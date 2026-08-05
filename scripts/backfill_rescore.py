#!/usr/bin/env python3
"""
One-time backfill after the 2026-07-21 scoring audit.

The bugs that were fixed in code had already written 80k rows of bad output to
data/sentiment.db, and the aggregator recomputes composites from those stored
values — so fixing the code alone left the dashboard showing the old, wrong
numbers. This rewrites the stored corpus with the corrected logic:

  1. tickers          — re-extracted with word-boundary matching (removes ~2.5k
                        false attributions like META from "Rheinmetall")
  2. sentiment_score  — FinBERT P(positive) - P(negative) instead of the
                        label-map that collapsed every neutral article to 0.0
  3. vader_compound   — rescored with the finance lexicon
  4. trust_weight     — recomputed from the corrected SOURCE_TRUST table
  5. message_density  — renormalised to (0, 1]
  6. rank_score       — recomputed as the undecayed base
                        (|sentiment| x density x trust). Time decay is applied
                        live by the readers; storing it here decayed twice.

Safe to re-run. Back up data/sentiment.db first.

    .venv/bin/python scripts/backfill_rescore.py [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func

from config.settings import (
    SOURCE_TRUST, DEFAULT_TRUST_WEIGHT, TIME_DECAY_HALFLIFE_HOURS,
)
from src.sentiment import FinBERTScorer
from src.sentiment import extract_tickers, tickers_to_str
from src.sentiment import score as vader_score
from src.storage import SentimentResult, init_db

BATCH = 256


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only process N rows")
    ap.add_argument("--dry-run", action="store_true", help="report, do not write")
    args = ap.parse_args()

    db = init_db()
    finbert = FinBERTScorer()

    total = db.query(func.count(SentimentResult.id)).scalar() or 0
    max_density = db.query(func.max(SentimentResult.message_density)).scalar() or 1.0
    if max_density <= 0:
        max_density = 1.0

    print(f"rows={total}  max_density={max_density:.1f}  "
          f"{'DRY RUN' if args.dry_run else 'WRITING'}")

    processed = changed_tickers = flipped_sign = 0
    t0 = time.time()
    offset = 0

    while True:
        rows = (db.query(SentimentResult)
                  .order_by(SentimentResult.id)
                  .offset(offset).limit(BATCH).all())
        if not rows:
            break

        texts = [r.title or "" for r in rows]
        fb_results = finbert.score_batch(texts)

        for row, fb, text in zip(rows, fb_results, texts):
            old_tickers = row.tickers or ""
            old_score = row.sentiment_score or 0.0

            new_tickers = tickers_to_str(extract_tickers(text))
            vader_compound = vader_score(text).compound
            new_score = fb.score if fb is not None else vader_compound

            trust = SOURCE_TRUST.get(row.source, DEFAULT_TRUST_WEIGHT)
            density = min(1.0, (row.message_density or 1.0) / max_density)

            if not args.dry_run:
                row.tickers = new_tickers
                row.sentiment_score = new_score
                row.vader_compound = vader_compound
                row.finbert_label = fb.label if fb else None
                row.finbert_score = fb.score if fb else None
                row.finbert_conf = fb.confidence if fb else None
                row.trust_weight = trust
                row.message_density = density
                row.rank_score = abs(new_score) * density * trust

            if new_tickers != old_tickers:
                changed_tickers += 1
            if old_score * new_score < 0:
                flipped_sign += 1
            processed += 1

        if not args.dry_run:
            db.commit()

        offset += BATCH
        rate = processed / max(time.time() - t0, 1e-6)
        eta = (total - processed) / rate / 60 if rate else 0
        print(f"  {processed}/{total}  ({rate:.0f}/s, ETA {eta:.1f} min)  "
              f"tickers_changed={changed_tickers}  sign_flips={flipped_sign}",
              flush=True)

        if args.limit and processed >= args.limit:
            break

    print(f"\ndone in {(time.time()-t0)/60:.1f} min — "
          f"{processed} rows, {changed_tickers} ticker changes, "
          f"{flipped_sign} sentiment sign flips")

    if not args.dry_run:
        from src.pipeline import aggregate_tickers
        n = aggregate_tickers(db)
        print(f"re-aggregated {n} tickers")

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
