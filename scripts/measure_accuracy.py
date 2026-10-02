#!/usr/bin/env python3
"""
Re-measure the prediction track record straight from the database.

    python scripts/measure_accuracy.py

Prints a Markdown block you can paste into docs/PREDICTION_TRACK_RECORD.md, so
the published numbers can always be re-derived instead of trusted. Every figure
comes from the `signal_history` table: each row is one prediction, logged with
the price at the time and graded later against the real price.
"""
from __future__ import annotations
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "sentiment.db")


def pct(ok: int, n: int) -> str:
    return f"{100 * ok / n:.1f}%" if n else "n/a"


def main() -> None:
    if not os.path.exists(DB):
        sys.exit(f"No database at {DB}. Run the app first so predictions accumulate.")
    cur = sqlite3.connect(DB).cursor()

    def one(sql, args=()):
        cur.execute(sql, args)
        return cur.fetchone()

    lo, hi = one("select min(date(created_at)), max(date(created_at)) from signal_history")
    snaps = one("select count(*) from signal_history")[0]
    tickers = one("select count(distinct ticker) from signal_history")[0]

    print(f"**Window:** {lo} → {hi}  ·  **{snaps:,} snapshots** across **{tickers} tickers**\n")

    for label, col in (("7-day horizon", "correct"), ("30-day horizon", "correct_30d")):
        graded = one(f"select count(*) from signal_history where {col} is not null")[0]
        n, ok = one(f"select count(*), sum({col}) from signal_history "
                    f"where {col} is not null and signal in ('BUY','SELL')")
        print(f"### {label} — {graded:,} graded\n")
        print("| Call | Count | Correct |")
        print("|---|---|---|")
        for sig in ("BUY", "SELL", "HOLD"):
            a, b = one(f"select count(*), sum({col}) from signal_history "
                       f"where {col} is not null and signal=?", (sig,))
            if a:
                print(f"| {sig} | {a:,} | {pct(b, a)} |")
        print(f"| **Directional (Buy+Sell)** | **{n:,}** | **{pct(ok, n)}** |\n")

    print("### 7-day directional accuracy by month\n")
    print("| Month | Calls | Correct |")
    print("|---|---|---|")
    cur.execute("""select strftime('%Y-%m', created_at), count(*), sum(correct)
                   from signal_history
                   where correct is not null and signal in ('BUY','SELL')
                   group by 1 order by 1""")
    for month, n, ok in cur.fetchall():
        print(f"| {month} | {n:,} | {pct(ok, n)} |")


if __name__ == "__main__":
    main()
