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
from __future__ import annotations
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
from src.storage.models import SentimentResult, TickerSentiment
from src.sentiment.ticker_extractor import str_to_tickers

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
