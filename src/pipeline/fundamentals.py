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

    m1 = clamp((r1 or 0) / 40.0)                       # +40% in 1y  → +1
    m5 = clamp((r5 or 0) / 150.0)                      # +150% in 5y → +1
    near = clamp(1.0 + (from_ath or -100) / 30.0)      # at ATH → +1, 30% below → 0
    return round(0.4 * m1 + 0.4 * m5 + 0.2 * near, 4)


def _continuation_label(score: float) -> str:
    if score > 0.35:  return "Strong Uptrend"
    if score > 0.10:  return "Building"
    if score < -0.10: return "Weak"
    return "Mixed"


def _aggregate(db: Session) -> None:
    """
    The prediction: which stocks look likely to IMPROVE, from
      - the company's report history for ALL TIME (10-K/10-Q trajectory + level)
      - financial news for the week (7-day composite)
    All scoring is local (FinBERT) — zero external AI calls required.
    Price stats are attached for display only, not the prediction.
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

        # ── All-time report signal ─────────────────────────────────────────────
        trajectory, level = _report_trajectory(fs)
        reports_signal = 0.6 * trajectory + 0.4 * level

        # ── Weekly news signal ─────────────────────────────────────────────────
        news = ts.composite_score or 0.0

        # ── Prediction: reports (all-time) 60% + news (week) 40% ──────────────
        pred = 0.6 * reports_signal + 0.4 * news
        ts.continuation_score = round(pred, 4)
        ts.continuation_label = _continuation_label(pred)

        # Price record attached for display/cross-checking only
        pstats = get_price_stats(ticker)
        if pstats:
            ts.price_score      = _price_score(pstats)
            ts.price_return_1y  = pstats.get("return_1y")
            ts.price_return_5y  = pstats.get("return_5y")
            ts.pct_from_ath     = pstats.get("pct_from_ath")
            ts.price_volatility = pstats.get("volatility")
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


def _score_signals(db: Session) -> None:
    """Grade signals that are ≥7 days old against what the price actually did."""
    from src.collectors.price_history import get_price_stats
    from src.storage.models import SignalHistory

    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    pending = (db.query(SignalHistory)
               .filter(SignalHistory.correct.is_(None),
                       SignalHistory.created_at <= cutoff,
                       SignalHistory.price_at_signal.isnot(None))
               .limit(60).all())
    for s in pending:
        pstats = get_price_stats(s.ticker)
        if not pstats:
            continue
        s.price_after = pstats["latest"]
        s.pct_change = round((s.price_after / s.price_at_signal - 1.0) * 100, 2) \
            if s.price_at_signal else None
        if s.pct_change is None:
            continue
        if s.signal == "BUY":
            s.correct = 1 if s.pct_change > 1.0 else 0
        elif s.signal == "SELL":
            s.correct = 1 if s.pct_change < -1.0 else 0
        else:  # HOLD
            s.correct = 1 if abs(s.pct_change) < 3.0 else 0
    db.commit()
