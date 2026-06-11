#!/usr/bin/env python3
"""
Entry point: starts the APScheduler pipeline loop and the Flask dashboard
in separate threads so both run together.

Usage:
    python run.py [--no-dashboard] [--once]
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
import threading

# Disable CrewAI's interactive trace prompt — it blocks the pipeline
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "1")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("run")

# Make src/ importable
sys.path.insert(0, os.path.dirname(__file__))

from config.settings import PIPELINE_INTERVAL, DATABASE_URL
from src.pipeline.crew import SentimentCrew
from src.storage.models import init_db
from src.utils.market_hours import market_status, pipeline_interval_seconds


def run_pipeline_loop(crew: SentimentCrew, once: bool = False):
    import datetime as dt
    from apscheduler.schedulers.blocking import BlockingScheduler

    if once:
        log.info("Running single pipeline cycle …")
        crew.run_cycle()
        return

    scheduler = BlockingScheduler()
    _current_interval = [None]  # mutable ref so inner func can read it

    def smart_cycle():
        ms = market_status()
        ideal = pipeline_interval_seconds()

        # Re-schedule if market status changed the ideal interval
        if _current_interval[0] != ideal:
            _current_interval[0] = ideal
            job = scheduler.get_job("sentiment_pipeline")
            if job:
                job.reschedule("interval", seconds=ideal)
            log.info("Pipeline interval → %ds  [%s]", ideal, ms["status"])

        crew.run_cycle()

    interval = pipeline_interval_seconds()
    _current_interval[0] = interval
    ms = market_status()
    log.info("Pipeline starting — %s — interval %ds", ms["label"], interval)

    scheduler.add_job(
        smart_cycle,
        "interval",
        seconds=interval,
        id="sentiment_pipeline",
        max_instances=1,
        coalesce=True,
        next_run_time=dt.datetime.now(),
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Pipeline scheduler stopped")


def run_dashboard():
    from src.dashboard.app import app
    port = int(os.getenv("DASHBOARD_PORT", 5000))
    log.info("Dashboard starting on http://localhost:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)


def main():
    parser = argparse.ArgumentParser(description="Financial News Sentiment Pipeline")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Run pipeline only, no Flask dashboard")
    parser.add_argument("--once", action="store_true",
                        help="Run one pipeline cycle then exit")
    args = parser.parse_args()

    # Ensure DB schema exists
    init_db()

    crew = SentimentCrew()

    if args.once:
        run_pipeline_loop(crew, once=True)
        return

    if not args.no_dashboard:
        dash_thread = threading.Thread(target=run_dashboard, daemon=True)
        dash_thread.start()

    run_pipeline_loop(crew)


if __name__ == "__main__":
    main()
