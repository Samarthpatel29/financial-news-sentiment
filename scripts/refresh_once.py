#!/usr/bin/env python3
"""
One-shot pipeline refresh for CI (GitHub Actions).

Runs a full cycle and regenerates the static snapshot, so a scheduled cloud job
can keep the deployed Vercel site fresh 24/7 — no one has to run start.sh:

  1. news cycle         collect RSS + StockTwits -> FinBERT/LLM score -> aggregate
  2. fundamentals cycle SEC filings -> price/analyst blend -> BUY/SELL/HOLD
  3. export_static      write public/*.json + index.html for the deploy

Designed to run from the repo root. The SQLite corpus (data/sentiment.db) is
persisted between runs by the workflow's cache so the 7-day news window
accumulates instead of starting empty each time.
"""
from __future__ import annotations
import logging
import os
import sys

os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)   # export_static writes ./public relative to cwd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("refresh")


def main() -> None:
    from src.storage import init_db
    from src.pipeline import SentimentCrew
    from src.pipeline import run_fundamentals_cycle

    log.info("1/3  news cycle (collect → score → aggregate) …")
    SentimentCrew().run_cycle()

    log.info("2/3  fundamentals cycle (filings → price/analyst blend → signals) …")
    db = init_db()
    try:
        run_fundamentals_cycle(db)
    finally:
        db.close()

    log.info("3/3  exporting static snapshot → public/ …")
    import export_static
    export_static.main()

    log.info("refresh complete")


if __name__ == "__main__":
    main()
