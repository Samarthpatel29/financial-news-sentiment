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
from collections import defaultdict
from typing import NamedTuple

from sqlalchemy.orm import Session
from sqlalchemy import desc

from config.settings import TICKER_WINDOW_HOURS
from src.storage.models import SentimentResult, TickerSentiment
from src.sentiment.ticker_extractor import str_to_tickers

log = logging.getLogger(__name__)


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
    })

    for r in rows:
        tickers = str_to_tickers(r.tickers)
        if not tickers:
            continue

        # weight for this article
        w = abs(r.sentiment_score) * (r.trust_weight or 1.0) * (r.time_weight or 1.0)

        for ticker in tickers:
            b = buckets[ticker]
            b["scores"].append(r.sentiment_score)
            b["weights"].append(w)
            b["trust_vals"].append(r.trust_weight or 1.0)
            b["sources"].append(r.source)
            b["urls"].append(r.url or "")
            b["headlines"].append((r.rank_score or 0.0, r.title))

    if not buckets:
        log.info("Aggregator: no ticker mentions found in window")
        return 0

    # ── Upsert TickerSentiment ─────────────────────────────────────────────────
    updated = 0
    for ticker, b in buckets.items():
        scores   = b["scores"]
        weights  = b["weights"]
        total_w  = sum(weights)

        if total_w > 0:
            composite = sum(s * w for s, w in zip(scores, weights)) / total_w
        else:
            composite = sum(scores) / len(scores)  # plain mean fallback

        n        = len(scores)
        bullish  = sum(1 for s in scores if s >  0.05)
        bearish  = sum(1 for s in scores if s < -0.05)
        neutral  = n - bullish - bearish
        avg_trust = sum(b["trust_vals"]) / len(b["trust_vals"])

        # Best headline = highest rank_score
        best_rank, best_title = max(b["headlines"], key=lambda x: x[0])
        best_idx = next(i for i, h in enumerate(b["headlines"]) if h[1] == best_title)
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
                last_updated    = datetime.datetime.utcnow(),
            ))
        updated += 1

    db.commit()
    log.info("Aggregator: upserted %d tickers", updated)
    return updated
