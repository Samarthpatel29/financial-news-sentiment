"""
Long-term fundamentals engine — Phases C & D.

Runs on a slow cadence (filings change a few times a day, not every minute):

  1. pick the tickers we already track (top of TickerSentiment)
  2. EdgarCollector -> recent 10-K / 10-Q / 8-K filings (last 7 days)
  3. skip filings already stored (dedup by accession)
  4. for each new filing:  extract section -> FinBERT score
                           -> Groq plain-English summary + verdict
                           -> fundamental_score, store Filing row
  5. aggregate per ticker over the 7-day window -> TickerSentiment.fundamental_*

100% free: SEC EDGAR + local FinBERT + Groq free tier. Groq is called only on
*new* filings (a handful per day) and the summary is cached in the DB.

See docs/FUNDAMENTALS_PLAN.md.
"""
from __future__ import annotations
import datetime
import logging

from sqlalchemy import desc
from sqlalchemy.orm import Session

from config.settings import (
    GROQ_API_KEY, CREW_LLM_MODEL,
    USE_GROQ_SUMMARIES, REPORT_HISTORY_MAX_FILINGS,
)
from src.collectors.edgar_collector import EdgarCollector, LOOKBACK_DAYS
from src.collectors.edgar_extractor import extract_section
from src.sentiment import SentimentScorer
from src.storage.models import Filing, TickerSentiment

log = logging.getLogger(__name__)

_GROQ_MODEL = CREW_LLM_MODEL.split("/", 1)[-1]

# Form-type weight in the fundamental score (10-K richest, 8-K lightest)
_FORM_WEIGHT = {"10-K": 1.0, "10-K/A": 1.0, "10-Q": 0.9, "10-Q/A": 0.9,
                "8-K": 0.8, "8-K/A": 0.8}

# Groq's long-term verdict mapped to a numeric signal
_VERDICT_SCORE = {"Improving": 1.0, "Stable": 0.0, "Deteriorating": -1.0}


def _blend(finbert_score: float | None, verdict: str) -> float:
    """
    Combine FinBERT (good on substantive prose) with Groq's verdict (which
    actually reads the filing). On short 8-Ks FinBERT often sees only cover-page
    boilerplate and returns 0, so the verdict carries most of the signal.
    """
    v = _VERDICT_SCORE.get(verdict)
    fb = finbert_score if finbert_score is not None else None
    if v is not None and fb is not None:
        return 0.7 * v + 0.3 * fb
    if v is not None:
        return v
    return fb or 0.0

_SUMMARY_SYSTEM = (
    "You are a long-term equity research assistant. Given an excerpt from a "
    "company's SEC filing, write a 2-3 sentence plain-English summary of what it "
    "means for a LONG-TERM investor, then on a final line output exactly one of: "
    "VERDICT: Improving | VERDICT: Stable | VERDICT: Deteriorating. "
    "Be objective, do not give buy/sell advice."
)

_MAX_TICKERS_PER_CYCLE = 40   # keep each cycle polite + within free limits


def _groq_summary(ticker: str, form_type: str, text: str) -> tuple[str, str]:
    """Return (summary, verdict) from Groq; ('','') if unavailable/disabled."""
    if not USE_GROQ_SUMMARIES:
        return "", ""   # 0-API mode: FinBERT alone drives the verdict
    if not GROQ_API_KEY or GROQ_API_KEY.startswith("PASTE_"):
        return "", ""
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)
        prompt = (f"Ticker {ticker}, form {form_type}. Filing excerpt:\n\n"
                  f"{text[:4000]}")
        resp = client.chat.completions.create(
            model=_GROQ_MODEL,
            messages=[{"role": "system", "content": _SUMMARY_SYSTEM},
                      {"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=220,
        )
        out = resp.choices[0].message.content.strip()
    except Exception as exc:
        log.warning("Groq filing summary failed [%s]: %s", ticker, exc)
        return "", ""

    verdict = ""
    summary = out
    m = out.rfind("VERDICT:")
    if m != -1:
        tail = out[m + len("VERDICT:"):].strip()
        for v in ("Improving", "Stable", "Deteriorating"):
            if tail.lower().startswith(v.lower()):
                verdict = v
                break
        summary = out[:m].strip()
    return _clean_summary(summary), verdict


def _clean_summary(s: str) -> str:
    """Drop the model's chatty preamble (e.g. 'Here's a 2-3 sentence summary:')."""
    s = s.strip()
    low = s.lower()
    if low.startswith("here") or low.startswith("sure"):
        # cut everything up to and including the first ':' on the opening line
        nl = s.find("\n")
        colon = s.find(":")
        if colon != -1 and (nl == -1 or colon < nl):
            s = s[colon + 1:].strip()
    return s


def _tracked_tickers(db: Session) -> list[str]:
    rows = (db.query(TickerSentiment.ticker)
            .order_by(desc(TickerSentiment.article_count))
            .limit(_MAX_TICKERS_PER_CYCLE)
            .all())
    return [r[0] for r in rows]


def run_fundamentals_cycle(db: Session) -> int:
    """Fetch, score, and store new filings; returns count of new filings."""
    tickers = _tracked_tickers(db)
    if not tickers:
        log.info("Fundamentals: no tracked tickers yet — skipping")
        return 0

    filings = EdgarCollector(lookback_days=LOOKBACK_DAYS).collect(tickers)
    if not filings:
        log.info("Fundamentals: no recent filings")
        _aggregate(db)
        return 0

    existing = {a[0] for a in db.query(Filing.accession).all()}
    new = [f for f in filings if f.accession not in existing]
    log.info("Fundamentals: %d filings, %d new", len(filings), len(new))

    scorer = SentimentScorer()
    stored = 0
    for f in new:
        text = extract_section(f.url, f.section_kind)
        finbert_score = None
        if text:
            try:
                res = scorer._finbert.score_batch([text[:512]])[0]
                finbert_score = res.score if res else 0.0
            except Exception:
                finbert_score = 0.0

        summary, verdict = _groq_summary(f.ticker, f.form_type, text) if text else ("", "")

        fw = _FORM_WEIGHT.get(f.form_type, 0.8)
        fundamental = round(_blend(finbert_score, verdict) * fw, 4)

        db.add(Filing(
            cik=f.cik, ticker=f.ticker, form_type=f.form_type,
            section_kind=f.section_kind, filed_at=f.filed_at,
            accession=f.accession, title=f.title, url=f.url,
            finbert_score=finbert_score, fundamental_score=fundamental,
            llm_summary=summary, llm_verdict=verdict,
        ))
        stored += 1
    db.commit()

    # Deepen the all-time report history a few tickers at a time (local scoring)
    build_report_history(db)

    _aggregate(db)
    return stored


def build_report_history(db: Session, tickers_per_cycle: int = 5) -> int:
    """
    Backfill the ALL-TIME report history (10-K / 10-Q going back years) for a
    few tickers per cycle, scored locally with FinBERT — zero external AI calls.
    Spreading the work over cycles keeps each run fast and polite to SEC.
    """
    collector = EdgarCollector()
    scorer = SentimentScorer()

    # Tickers we track by news volume that don't have deep history yet
    candidates = _tracked_tickers(db)
    done = 0
    for ticker in candidates:
        if done >= tickers_per_cycle:
            break
        n_hist = (db.query(Filing)
                  .filter(Filing.ticker == ticker,
                          Filing.form_type.in_(("10-K", "10-Q")))
                  .count())
        if n_hist >= 4:      # already has meaningful history
            continue

        history = collector.collect_history(ticker, REPORT_HISTORY_MAX_FILINGS)
        if not history:
            continue
        existing = {a[0] for a in db.query(Filing.accession)
                    .filter(Filing.ticker == ticker).all()}
        stored = 0
        for f in history:
            if f.accession in existing:
                continue
            text = extract_section(f.url, f.section_kind)
            if not text:
                continue
            try:
                res = scorer._finbert.score_batch([text[:512]])[0]
                fb = res.score if res else 0.0
            except Exception:
                fb = 0.0
            fw = _FORM_WEIGHT.get(f.form_type, 0.9)
            db.add(Filing(
                cik=f.cik, ticker=f.ticker, form_type=f.form_type,
                section_kind=f.section_kind, filed_at=f.filed_at,
                accession=f.accession, title=f.title, url=f.url,
                finbert_score=fb, fundamental_score=round(fb * fw, 4),
                llm_summary="", llm_verdict="",
            ))
            stored += 1
        if stored:
            db.commit()
            log.info("Report history: %s +%d filings", ticker, stored)
            done += 1
    return done


def _report_trajectory(fs: list[Filing]) -> tuple[float, float]:
    """
    (trajectory, recent_level) from a company's all-time report history.
    trajectory > 0 means its reports have been reading better over time.
    """
    scored = sorted(
        [f for f in fs if f.fundamental_score is not None and f.filed_at],
        key=lambda f: f.filed_at,
    )
    if len(scored) < 2:
        lvl = scored[0].fundamental_score if scored else 0.0
        return 0.0, lvl
    half = len(scored) // 2
    older  = [f.fundamental_score for f in scored[:half]]
    recent = [f.fundamental_score for f in scored[half:]]
    older_avg  = sum(older) / len(older)
    recent_avg = sum(recent) / len(recent)
    traj = max(-1.0, min(1.0, recent_avg - older_avg))
    return round(traj, 4), round(recent_avg, 4)


def _price_score(stats: dict) -> float:
    """
    Long-term price trend as a -1..1 signal for "likely to keep going up".
    Rewards positive 1-yr and 5-yr returns and sitting near the all-time high.
    """
    def clamp(x): return max(-1.0, min(1.0, x))
    r1 = stats.get("return_1y")
    r5 = stats.get("return_5y")
    from_ath = stats.get("pct_from_ath")   # 0 at peak, negative below

    # Only include the components we actually have, and renormalize over them.
    # NOTE: `from_ath` must be checked for None explicitly — 0.0 means "sitting
    # at the all-time high" (very bullish), but `from_ath or -100` treated that
    # (and any missing value) as −100%, forcing near = −1 and dragging a healthy
    # stock's score down for no reason.
    parts = []
    if r1 is not None:
        parts.append((clamp(r1 / 40.0), 0.4))               # +40% in 1y  → +1
    if r5 is not None:
        parts.append((clamp(r5 / 150.0), 0.4))              # +150% in 5y → +1
    if from_ath is not None:
        parts.append((clamp(1.0 + from_ath / 30.0), 0.2))   # at ATH → +1, 30% below → 0
    if not parts:
        return 0.0
    num = sum(v * w for v, w in parts)
    den = sum(w for _, w in parts)
    return round(num / den, 4)


def _reports_signal(fs: list[Filing]) -> float:
    """
    Company-report component. Prefer the Groq verdicts (Improving/Stable/
    Deteriorating) because FinBERT reads dry filing text as neutral and
    flatlines. Newer filings weighted more; falls back to the FinBERT
    trajectory when no verdicts exist yet.
    """
    V = {"Improving": 1.0, "Stable": 0.0, "Deteriorating": -1.0}
    verds = [(f.filed_at, V[f.llm_verdict]) for f in fs
             if f.llm_verdict in V and f.filed_at]
    if verds:
        verds.sort(key=lambda x: x[0])
        n = len(verds)
        num = sum(v * ((i + 1) / n) for i, (_, v) in enumerate(verds))
        den = sum((i + 1) / n for i in range(n))
        return num / den if den else 0.0
    traj, level = _report_trajectory(fs)
    return 0.6 * traj + 0.4 * level


def _analyst_signal(recom: float | None) -> float | None:
    """Finviz analyst consensus 1..5 (1=Strong Buy, 5=Strong Sell) → +1..-1."""
    if recom is None:
        return None
    return max(-1.0, min(1.0, (3.0 - recom) / 2.0))


# Give up on Finviz for the rest of a cycle after repeated failures (it's
# blocking us) so aggregation never stalls; analysts just drop from the blend.
_finviz_fails = {"n": 0}


def _fetch_recom(ticker: str) -> float | None:
    if _finviz_fails["n"] >= 4:
        return None
    try:
        from src.collectors.finviz_verify import verify
        data = verify(ticker)
        if data and data.get("recom") is not None:
            _finviz_fails["n"] = 0
            return data["recom"]
        _finviz_fails["n"] += 1
    except Exception:
        _finviz_fails["n"] += 1
    return None


def _continuation_label(score: float) -> str:
    # Boundaries align with _signal_of (±0.12) so the word and the BUY/SELL/HOLD
    # badge never disagree (0.11 used to read "Building" but "HOLD"). Symmetric
    # so a strong downtrend is named as clearly as a strong uptrend.
    if score >  0.35:  return "Strong Uptrend"
    if score >  0.12:  return "Building"
    if score < -0.35:  return "Strong Downtrend"
    if score < -0.12:  return "Weak"
    return "Mixed"


def _aggregate(db: Session) -> None:
    """
    The prediction: which stocks look likely to IMPROVE, blended from four
    components (each -1..1, missing ones drop out and the rest renormalize):
      - news      (30%) — this week's financial-news sentiment (7-day composite)
      - momentum  (30%) — long-term price trend (1y/5y returns, distance from ATH)
      - analysts  (25%) — Finviz consensus recommendation
      - reports   (15%) — the company's SEC-filing trajectory (10-K/10-Q/8-K)
    Local scoring (FinBERT) needs zero external AI calls; the optional Groq LLM
    judge only sharpens per-headline nuance when a free key is present. Price
    momentum is part of the prediction (not display-only) so a stock that has
    actually risen isn't rated SELL off a single bad news week.
    """
    from src.collectors.price_history import get_price_stats

    week_ago = datetime.datetime.utcnow() - datetime.timedelta(days=LOOKBACK_DAYS)

    # Group ALL stored filings (full history) by ticker
    by_ticker: dict[str, list[Filing]] = {}
    for f in db.query(Filing).all():
        by_ticker.setdefault(f.ticker, []).append(f)

    for ticker, fs in by_ticker.items():
        ts = db.query(TickerSentiment).filter(TickerSentiment.ticker == ticker).first()
        if ts is None:
            continue   # only rank tickers we already track via news

        # This week's filings feed the 7-day fields (shown as "recent filings")
        recent_fs = [f for f in fs if f.filed_at and f.filed_at >= week_ago]
        recent_scores = [f.fundamental_score for f in recent_fs
                         if f.fundamental_score is not None]
        if recent_scores:
            avg7 = sum(recent_scores) / len(recent_scores)
            ts.fundamental_score   = round(avg7, 4)
            ts.fundamental_verdict = ("Improving" if avg7 > 0.15 else
                                      "Deteriorating" if avg7 < -0.15 else "Stable")
        ts.filing_count_7d = len(recent_fs) if recent_fs else (1 if fs else 0)
        ts.last_filing_at  = max((f.filed_at for f in fs if f.filed_at), default=None)

        # ── Four real components, each -1..1 (missing ones drop out) ───────────
        # 1) News (this week's financial-news sentiment)
        news = ts.composite_score
        # 2) Reports (company's SEC filings — Groq verdicts, which actually move,
        #    with a FinBERT-trajectory fallback)
        reports = _reports_signal(fs)
        # 3) Price momentum (is the stock actually trending up? — this is what was
        #    missing before, why a +70% stock could read SELL)
        pstats = get_price_stats(ticker)
        momentum = _price_score(pstats) if pstats else None
        if pstats:
            ts.price_score      = momentum
            ts.price_return_1y  = pstats.get("return_1y")
            ts.price_return_5y  = pstats.get("return_5y")
            ts.pct_from_ath     = pstats.get("pct_from_ath")
            ts.price_volatility = pstats.get("volatility")
        # 4) Analyst consensus (Finviz Recom 1..5 → -1..1) so the rating lines up
        #    with what Wall Street analysts say
        recom = _fetch_recom(ticker)
        analysts = _analyst_signal(recom)
        ts.analyst_recom  = recom
        ts.analyst_signal = analysts
        ts.reports_signal  = round(reports, 4)
        ts.momentum_signal = round(momentum, 4) if momentum is not None else None

        # Weighted blend, renormalized over the components we actually have.
        # News & momentum lead; analysts corroborate; reports (weakest signal,
        # FinBERT flatlines on filings) contributes least.
        parts = [(news, 0.30), (momentum, 0.30), (analysts, 0.25), (reports, 0.15)]
        num = sum(v * w for v, w in parts if v is not None)
        den = sum(w for v, w in parts if v is not None)
        pred = num / den if den else 0.0

        # Guardrail 1 — never emit a confident BUY/SELL from news text alone.
        # Price momentum and analyst consensus are the "hard" corroborators for
        # what the stock is actually doing. When BOTH are missing (free scrapers
        # failed), the blend silently renormalized onto news + reports and a
        # single bad news week produced SELL on a stock that had risen. Clamp
        # into the HOLD band so the rating stays honest instead of guessing.
        if momentum is None and analysts is None:
            pred = max(-0.12, min(0.12, pred))

        # Guardrail 2 — a stock in a clear multi-quarter uptrend must not read
        # SELL off one rough news week. Floor to (at worst) HOLD when momentum
        # is strongly positive. This is exactly the "+70% stock shown SELL" bug.
        if momentum is not None and momentum >= 0.5:
            pred = max(pred, -0.12)

        ts.continuation_score = round(pred, 4)
        ts.continuation_label = _continuation_label(pred)
    db.commit()

    _record_signals(db)
    _score_signals(db)


# ── Honest self-scoring: log today's signals, grade them a week later ──────────
def _signal_of(score: float) -> str:
    return "BUY" if score > 0.12 else "SELL" if score < -0.12 else "HOLD"


def _record_signals(db: Session) -> None:
    """Snapshot each ticker's current signal once per day."""
    from src.collectors.price_history import get_price_stats
    from src.storage.models import SignalHistory

    today = datetime.datetime.utcnow().date()
    rows = db.query(TickerSentiment).filter(TickerSentiment.filing_count_7d > 0).all()
    for ts in rows:
        already = (db.query(SignalHistory)
                   .filter(SignalHistory.ticker == ts.ticker)
                   .order_by(SignalHistory.created_at.desc())
                   .first())
        if already and already.created_at and already.created_at.date() == today:
            continue
        pstats = get_price_stats(ts.ticker)
        db.add(SignalHistory(
            ticker=ts.ticker,
            signal=_signal_of(ts.continuation_score or 0.0),
            score=ts.continuation_score or 0.0,
            price_at_signal=(pstats or {}).get("latest"),
        ))
    db.commit()


def _grade(signal: str, pct: float, up: float, hold_band: float) -> int:
    """Was the call right? Thresholds scale with the horizon (bigger for monthly)."""
    if signal == "BUY":
        return 1 if pct > up else 0
    if signal == "SELL":
        return 1 if pct < -up else 0
    return 1 if abs(pct) < hold_band else 0     # HOLD


def _score_signals(db: Session) -> None:
    """
    Grade past signals against what the price actually did, on two long-term
    horizons: weekly (≥7 days old) and monthly (≥30 days old). Monthly uses
    slightly wider thresholds because prices move more over a month.
    """
    from src.collectors.price_history import get_price_stats
    from src.storage.models import SignalHistory

    now = datetime.datetime.utcnow()
    week_cut  = now - datetime.timedelta(days=7)
    month_cut = now - datetime.timedelta(days=30)

    # Weekly grade — rows ≥7d old not yet weekly-scored.
    for s in (db.query(SignalHistory)
              .filter(SignalHistory.correct.is_(None),
                      SignalHistory.created_at <= week_cut,
                      SignalHistory.price_at_signal.isnot(None))
              .limit(60).all()):
        pstats = get_price_stats(s.ticker)
        if not pstats or not s.price_at_signal:
            continue
        s.price_after = pstats["latest"]
        s.pct_change = round((s.price_after / s.price_at_signal - 1.0) * 100, 2)
        s.correct = _grade(s.signal, s.pct_change, up=1.0, hold_band=3.0)

    # Monthly grade — rows ≥30d old not yet monthly-scored (the meaningful
    # long-term check). Wider thresholds: BUY >2%, SELL <-2%, HOLD |pct|<6%.
    for s in (db.query(SignalHistory)
              .filter(SignalHistory.correct_30d.is_(None),
                      SignalHistory.created_at <= month_cut,
                      SignalHistory.price_at_signal.isnot(None))
              .limit(60).all()):
        pstats = get_price_stats(s.ticker)
        if not pstats or not s.price_at_signal:
            continue
        s.price_after_30d = pstats["latest"]
        s.pct_change_30d = round((s.price_after_30d / s.price_at_signal - 1.0) * 100, 2)
        s.correct_30d = _grade(s.signal, s.pct_change_30d, up=2.0, hold_band=6.0)

    db.commit()
