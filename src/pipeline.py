"""pipeline — merged from 3 modules for a simpler layout."""
from __future__ import annotations

# ======================================================================
# from pipeline/aggregator.py
# ======================================================================

"""
Per-ticker sentiment aggregator.

After each pipeline cycle this module queries the last TICKER_WINDOW_HOURS of
SentimentResults, groups by extracted ticker symbol, and upserts a
TickerSentiment row for each ticker.

Weighted average formula:
    weight_i = |sentiment_score_i| × trust_weight_i × time_weight_i
    composite = Σ(sentiment_score_i × weight_i) / Σ(weight_i)

If all weights are 0 (all articles neutral with score≈0) the simple mean is
used as a fallback.
"""
import datetime
import logging
import math
from collections import defaultdict
from typing import NamedTuple

from sqlalchemy.orm import Session
from sqlalchemy import desc

from config.settings import (
    TICKER_WINDOW_HOURS, MIN_ARTICLES_PER_TICKER, TIME_DECAY_HALFLIFE_HOURS,
    SOCIAL_SOURCES,
)
from src.storage import SentimentResult, TickerSentiment
from src.sentiment import str_to_tickers

log = logging.getLogger(__name__)

_LN2 = math.log(2)


def _live_time_weight(row: SentimentResult, now: datetime.datetime) -> float:
    """Exponential age decay evaluated at *now* (see aggregate_tickers)."""
    stamp = row.published or row.scored_at
    if stamp is None:
        return 1.0
    if stamp.tzinfo is not None:
        stamp = stamp.replace(tzinfo=None)
    hours_old = max(0.0, (now - stamp).total_seconds() / 3600)
    return math.exp(-_LN2 / TIME_DECAY_HALFLIFE_HOURS * hours_old)


class _TickerBucket(NamedTuple):
    scores:       list[float]
    weights:      list[float]
    trust_vals:   list[float]
    sources:      list[str]
    urls:         list[str]
    headlines:    list[tuple[float, str]]   # (rank_score, title)


def aggregate_tickers(db: Session) -> int:
    """
    Recompute TickerSentiment from the last TICKER_WINDOW_HOURS of data.
    Returns the number of tickers updated.
    """
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=TICKER_WINDOW_HOURS)

    rows: list[SentimentResult] = (
        db.query(SentimentResult)
        .filter(SentimentResult.scored_at >= cutoff)
        .all()
    )

    if not rows:
        log.info("Aggregator: no rows in window — skipping")
        return 0

    # ── Bucket articles by ticker ──────────────────────────────────────────────
    buckets: dict[str, dict] = defaultdict(lambda: {
        "scores": [], "weights": [], "trust_vals": [],
        "sources": [], "urls": [], "headlines": [],
        "social_scores": [], "social_weights": [],
    })

    now = datetime.datetime.utcnow()
    for r in rows:
        tickers = str_to_tickers(r.tickers)
        if not tickers:
            continue

        # Retail chatter is bucketed separately — see SOCIAL_SOURCES.
        if r.source in SOCIAL_SOURCES:
            sw = abs(r.sentiment_score) ** 0.5 * (r.trust_weight or 1.0) * _live_time_weight(r, now)
            for ticker in tickers:
                buckets[ticker]["social_scores"].append(r.sentiment_score)
                buckets[ticker]["social_weights"].append(sw)
            continue

        # Time decay is recomputed against *now*, not read from the stored
        # column. The stored value is a snapshot from when the article was
        # scored, so a 7-day-old headline kept the 1.0 weight it had when it
        # was fresh and never actually decayed inside the aggregation window.
        #
        # Confidence weight uses sqrt(|sentiment|), not |sentiment|. Linear
        # magnitude let one extreme headline (score ≈ -0.9, weight 0.9) outvote
        # ten mildly-positive ones (score ≈ +0.1, weight 0.1 each), flipping the
        # composite sign away from what most of the coverage actually said. sqrt
        # compresses that 9:1 dominance to ~3:1 so the majority carries.
        w = abs(r.sentiment_score) ** 0.5 * (r.trust_weight or 1.0) * _live_time_weight(r, now)

        for ticker in tickers:
            b = buckets[ticker]
            b["scores"].append(r.sentiment_score)
            b["weights"].append(w)
            b["trust_vals"].append(r.trust_weight or 1.0)
            b["sources"].append(r.source)
            b["urls"].append(r.url or "")
            # Decay is applied here too, so "top headline" means the most
            # important *recent* story. rank_score is stored undecayed, so
            # ranking on it raw surfaced a 5-day-old "Micron Plunges" over
            # today's "Micron Surges 12%" — a headline that contradicted the
            # very score it was supposed to illustrate.
            b["headlines"].append(
                ((r.rank_score or 0.0) * _live_time_weight(r, now), r.title)
            )

    if not buckets:
        log.info("Aggregator: no ticker mentions found in window")
        return 0

    # ── Upsert TickerSentiment ─────────────────────────────────────────────────
    updated = 0
    fresh: set[str] = set()
    for ticker, b in buckets.items():
        scores   = b["scores"]
        weights  = b["weights"]
        total_w  = sum(weights)

        # Require a minimum sample before publishing a score. A single article
        # can produce a near-±1.0 composite, which then outranks tickers backed
        # by hundreds of articles.
        if len(scores) < MIN_ARTICLES_PER_TICKER:
            continue
        fresh.add(ticker)

        if total_w > 0:
            composite = sum(s * w for s, w in zip(scores, weights)) / total_w
        else:
            composite = sum(scores) / len(scores)  # plain mean fallback

        n        = len(scores)
        bullish  = sum(1 for s in scores if s >  0.05)
        bearish  = sum(1 for s in scores if s < -0.05)
        neutral  = n - bullish - bearish
        avg_trust = sum(b["trust_vals"]) / len(b["trust_vals"])

        # Social chatter, aggregated the same way but reported separately
        s_scores  = b["social_scores"]
        s_weights = b["social_weights"]
        s_total_w = sum(s_weights)
        if s_scores:
            social = (sum(s * w for s, w in zip(s_scores, s_weights)) / s_total_w
                      if s_total_w > 0 else sum(s_scores) / len(s_scores))
        else:
            social = 0.0

        # Best headline = highest rank_score. Index is taken straight from the
        # max() rather than looked up by title, which mis-attributed the source
        # and URL whenever two articles shared a headline.
        best_idx, (best_rank, best_title) = max(
            enumerate(b["headlines"]), key=lambda pair: pair[1][0]
        )
        best_source = b["sources"][best_idx]
        best_url    = b["urls"][best_idx]

        # Upsert
        existing = (
            db.query(TickerSentiment)
            .filter(TickerSentiment.ticker == ticker)
            .first()
        )
        if existing:
            existing.composite_score = round(composite, 4)
            existing.article_count   = n
            existing.bullish_count   = bullish
            existing.bearish_count   = bearish
            existing.neutral_count   = neutral
            existing.avg_trust       = round(avg_trust, 3)
            existing.top_headline    = best_title
            existing.top_source      = best_source
            existing.top_url         = best_url
            existing.social_score    = round(social, 4)
            existing.social_count    = len(s_scores)
            existing.last_updated    = datetime.datetime.utcnow()
        else:
            db.add(TickerSentiment(
                ticker          = ticker,
                composite_score = round(composite, 4),
                article_count   = n,
                bullish_count   = bullish,
                bearish_count   = bearish,
                neutral_count   = neutral,
                avg_trust       = round(avg_trust, 3),
                top_headline    = best_title,
                top_source      = best_source,
                top_url         = best_url,
                social_score    = round(social, 4),
                social_count    = len(s_scores),
                last_updated    = datetime.datetime.utcnow(),
            ))
        updated += 1

    # ── Drop tickers that fell out of the window ──────────────────────────────
    # The aggregator only ever upserted, so a ticker that stopped being in the
    # news kept its last score forever and was still served as if current.
    dropped = 0
    # If this cycle produced nothing (feeds down, network error), keep whatever
    # is already there rather than wiping the board.
    stale = (
        db.query(TickerSentiment)
        .filter(~TickerSentiment.ticker.in_(fresh))
        .all()
        if fresh else []
    )
    for row in stale:
        # Keep rows that still carry a live fundamentals/filing signal, but zero
        # out the news half so nothing stale is presented as current news.
        if (row.filing_count_7d or 0) > 0:
            row.article_count = 0
            row.composite_score = 0.0
            row.bullish_count = row.bearish_count = row.neutral_count = 0
            row.social_score = 0.0
            row.social_count = 0
        else:
            db.delete(row)
        dropped += 1

    db.commit()
    log.info("Aggregator: upserted %d tickers, cleared %d stale", updated, dropped)
    return updated


# ======================================================================
# from pipeline/fundamentals.py
# ======================================================================

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
import datetime
import logging

from sqlalchemy import desc
from sqlalchemy.orm import Session

from config.settings import (
    GROQ_API_KEY, CREW_LLM_MODEL,
    USE_GROQ_SUMMARIES, REPORT_HISTORY_MAX_FILINGS,
    SIGNAL_BUY_THRESHOLD, SIGNAL_SELL_THRESHOLD,
)
from src.collectors import EdgarCollector, LOOKBACK_DAYS
from src.collectors import extract_section
from src.sentiment import SentimentScorer
from src.storage import Filing, TickerSentiment

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
        from src.collectors import verify
        data = verify(ticker)
        if data and data.get("recom") is not None:
            _finviz_fails["n"] = 0
            return data["recom"]
        _finviz_fails["n"] += 1
    except Exception:
        _finviz_fails["n"] += 1
    return None


def _continuation_label(score: float) -> str:
    # Boundaries align with _signal_of (asymmetric: BUY bar higher than SELL) so
    # the word and the BUY/SELL/HOLD badge never disagree.
    if score >  0.35:                    return "Strong Uptrend"
    if score >  SIGNAL_BUY_THRESHOLD:    return "Building"
    if score < -0.35:                    return "Strong Downtrend"
    if score < -SIGNAL_SELL_THRESHOLD:   return "Weak"
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
    from src.collectors import get_price_stats

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
    return ("BUY"  if score >  SIGNAL_BUY_THRESHOLD  else
            "SELL" if score < -SIGNAL_SELL_THRESHOLD else "HOLD")


def _record_signals(db: Session) -> None:
    """Snapshot each ticker's current signal once per day."""
    from src.collectors import get_price_stats
    from src.storage import SignalHistory

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
            # ingredient signals — logged for later evidence-based re-weighting
            comp_news=ts.composite_score,
            comp_momentum=ts.momentum_signal,
            comp_analysts=ts.analyst_signal,
            comp_reports=ts.reports_signal,
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
    from src.collectors import get_price_stats
    from src.storage import SignalHistory

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


# ======================================================================
# from pipeline/crew.py
# ======================================================================

import datetime
import logging
import os
import re
import time
import concurrent.futures
from typing import List

from config.settings import GROQ_API_KEY, CREW_LLM_MODEL, E2E_DEADLINE, PIPELINE_INTERVAL
from src.collectors import RSSCollector, ScraperCollector, BrokerCollector, StockTwitsCollector
from src.sentiment import SentimentScorer
from src.storage import Article, SentimentResult, init_db

log = logging.getLogger(__name__)

# How far back to look when de-duplicating by headline text
_TITLE_DEDUP_HOURS = 48

_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE    = re.compile(r"\s+")


def _title_key(title: str) -> str:
    """
    Normalised headline used for cross-source de-duplication.

    The same story arrives via Google News, Yahoo and the publisher's own feed
    under three different URLs, so URL-only dedup let it through three times —
    inflating message_density and every affected ticker's article_count.
    """
    return _WS_RE.sub(" ", _PUNCT_RE.sub("", (title or "").lower())).strip()


def _make_crew():
    """
    Build a CrewAI crew that uses Groq's free tier (llama-3.1-8b-instant).
    Returns None gracefully if crewai or groq are not installed / key missing.
    """
    if not GROQ_API_KEY or GROQ_API_KEY == "PASTE_YOUR_GROQ_KEY_HERE":
        log.info("GROQ_API_KEY not set — narrative summaries disabled")
        return None
    try:
        from crewai import Agent, Crew, Task, LLM
    except ImportError:
        log.warning("crewai not installed — narrative summaries disabled")
        return None

    llm = LLM(
        model=CREW_LLM_MODEL,       # "groq/llama-3.1-8b-instant"
        api_key=GROQ_API_KEY,
    )

    analyst = Agent(
        role="Financial News Analyst",
        goal=(
            "Synthesize the top-ranked financial news items into a concise "
            "market narrative. Highlight the strongest bullish and bearish signals."
        ),
        backstory=(
            "You are a CFA-level analyst who distils real-time news into "
            "actionable market intelligence in under 80 words."
        ),
        llm=llm,
        verbose=False,
    )

    summarize_task = Task(
        description=(
            "Given these ranked news items (title | source | sentiment | rank), "
            "write a market summary under 80 words:\n\n{ranked_items}"
        ),
        expected_output="A market narrative under 80 words.",
        agent=analyst,
    )

    return Crew(agents=[analyst], tasks=[summarize_task], verbose=False)


class SentimentCrew:
    """
    Runs the full pipeline every PIPELINE_INTERVAL seconds:
      collect  →  deduplicate  →  score  →  persist  →  (optional) Groq narrative
    """

    def __init__(self):
        self._rss        = RSSCollector()
        self._scraper    = ScraperCollector()
        self._broker     = BrokerCollector()
        self._stocktwits = StockTwitsCollector()
        self._scorer     = SentimentScorer()
        self._db         = init_db()
        self._crew       = _make_crew()

    def run_cycle(self) -> List[SentimentResult]:
        t0 = time.monotonic()
        log.info("── Pipeline cycle started ──")

        # 1. Collect from all sources in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            rss_f     = pool.submit(self._rss.collect)
            scraper_f = pool.submit(self._scraper.collect)
            broker_f  = pool.submit(self._broker.collect)
            st_f      = pool.submit(self._stocktwits.collect)
            rss_arts     = rss_f.result()
            scraper_arts = scraper_f.result()
            broker_arts  = broker_f.result()
            st_arts      = st_f.result()

        all_articles = rss_arts + scraper_arts + broker_arts + st_arts
        log.info("Collected %d articles  (%.1fs)", len(all_articles), time.monotonic() - t0)

        if not all_articles:
            log.warning("No articles — skipping cycle")
            return []

        # 2. Deduplicate by URL *and* headline within this batch
        seen: set[str] = set()
        seen_titles: set[str] = set()
        unique = []
        for a in all_articles:
            tkey = _title_key(a.title)
            if not a.url or a.url in seen:
                continue
            if tkey and tkey in seen_titles:
                continue
            seen.add(a.url)
            if tkey:
                seen_titles.add(tkey)
            unique.append(a)
        log.info("After batch dedup: %d unique articles", len(unique))

        # 3. Filter out URLs already in the database (cross-cycle dedup)
        existing_urls: set[str] = {
            row[0] for row in
            self._db.query(SentimentResult.url)
            .filter(SentimentResult.url.in_([a.url for a in unique]))
            .all()
        }
        new_articles = [a for a in unique if a.url not in existing_urls]

        # 3a. Drop stories already stored under a different URL (same headline
        #     re-syndicated by another feed) within the recent window.
        if new_articles:
            since = datetime.datetime.utcnow() - datetime.timedelta(hours=_TITLE_DEDUP_HOURS)
            recent_titles = {
                _title_key(t)
                for (t,) in self._db.query(SentimentResult.title)
                                    .filter(SentimentResult.scored_at >= since)
                                    .all()
            }
            recent_titles.discard("")
            before = len(new_articles)
            new_articles = [a for a in new_articles
                            if _title_key(a.title) not in recent_titles]
            if before != len(new_articles):
                log.info("Dropped %d re-syndicated duplicates", before - len(new_articles))

        log.info("New articles not yet in DB: %d", len(new_articles))

        # 3b. Backfill images for existing rows that have none —
        #     feeds re-serve the same articles, so the fresh fetch often has
        #     an image the older DB row is missing.
        fresh_imgs = {a.url: a.image_url for a in unique if a.image_url}
        if existing_urls and fresh_imgs:
            rows_missing_img = (
                self._db.query(SentimentResult)
                .filter(SentimentResult.url.in_(list(existing_urls)))
                .filter((SentimentResult.image_url == "") | (SentimentResult.image_url.is_(None)))
                .all()
            )
            filled = 0
            for row in rows_missing_img:
                img = fresh_imgs.get(row.url)
                if img:
                    row.image_url = img
                    filled += 1
            if filled:
                self._db.commit()
                log.info("Backfilled images on %d existing articles", filled)

        if not new_articles:
            # Still re-aggregate: composites carry live time decay and the
            # stale-ticker sweep, both of which must keep moving even on a
            # quiet cycle. Returning early here froze the board whenever the
            # feeds had nothing new.
            log.info("No new articles this cycle — re-aggregating only")
            aggregate_tickers(self._db)
            return []

        # 4. Score sentiment
        results = self._scorer.score_articles(new_articles, window_articles=all_articles)
        log.info("Scored %d articles  (%.1fs)", len(results), time.monotonic() - t0)

        # 5. Persist to SQLite.
        #    The articles table was never written, so every SentimentResult
        #    carried article_id = 0 and the article bodies were discarded after
        #    scoring. Store the article first, then point the score at its id.
        article_rows = [
            Article(
                source     = a.source,
                title      = a.title,
                url        = a.url,
                published  = a.published,
                body       = getattr(a, "body", "") or "",
            )
            for a in new_articles
        ]
        try:
            self._db.add_all(article_rows)
            self._db.flush()          # assigns primary keys without committing
            for result, row in zip(results, article_rows):
                result.article_id = row.id
        except Exception as exc:
            # Never let article bookkeeping cost us the sentiment scores
            self._db.rollback()
            log.warning("Article persistence failed (scores still saved): %s", exc)

        self._db.add_all(results)
        self._db.commit()

        # 5b. Per-ticker aggregation (fast — pure DB read/upsert)
        n_tickers = aggregate_tickers(self._db)
        log.info("Ticker aggregation: %d tickers updated  (%.1fs)", n_tickers, time.monotonic() - t0)

        # 6. Optional Groq narrative (only if budget allows)
        elapsed     = time.monotonic() - t0
        budget_left = E2E_DEADLINE - elapsed - 10
        if self._crew and budget_left > 15:
            top = sorted(results, key=lambda r: r.rank_score, reverse=True)[:10]
            ranked_str = "\n".join(
                f"{r.title[:70]} | {r.source} | {r.sentiment_score:.2f} | {r.rank_score:.2f}"
                for r in top
            )
            try:
                narrative = str(self._crew.kickoff(inputs={"ranked_items": ranked_str}))
                log.info("Groq narrative: %s", narrative[:200])
                os.makedirs("data", exist_ok=True)
                with open("data/narrative.txt", "w") as f:
                    f.write(narrative)
            except Exception as exc:
                log.warning("Groq narrative failed: %s", exc)

        total = time.monotonic() - t0
        log.info("Cycle done in %.1fs  (budget: %ds)", total, E2E_DEADLINE)
        if total > E2E_DEADLINE:
            log.error("E2E deadline exceeded: %.1fs > %ds", total, E2E_DEADLINE)

        return results

