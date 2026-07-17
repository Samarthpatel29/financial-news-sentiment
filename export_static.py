#!/usr/bin/env python3
"""
Export the dashboard's current data to a static site for Vercel.

The heavy lifting (FinBERT scoring, SEC filing analysis, price history) runs
here on your machine — Vercel just serves the results. That keeps the public
site free: no PyTorch (1.4 GB) in the cloud, no database, no schedulers.

    python export_static.py

Produces ./public/
    index.html          the dashboard, in static mode
    data/signals.json   AI signals (BUY/SELL/HOLD + reports + price)
    data/news.json      ranked articles, linked to tickers
    data/tickers.json   per-ticker sentiment (feeds the Sector Map)
    data/stats.json     header stats + narrative + market status
    data/candles/*.json daily OHLC per signal ticker
    data/price/*.json   all-time price stats per signal ticker
    api/chat.py         serverless chatbot (Groq only — no ML deps)

Then: vercel deploy (or push to GitHub with Vercel connected).
"""
from __future__ import annotations
import datetime
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

OUT = "public"
DATA = os.path.join(OUT, "data")


def _write(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, separators=(",", ":"))


def main() -> None:
    from src.dashboard.app import (
        _make_session, _ranked_rows, _ranked_tickers, _get_stats,
        _fundamental_rows, _signal_accuracy, _get_narrative,
    )
    from src.utils.market_hours import market_status
    from src.collectors.price_history import get_price_stats, get_candles

    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(DATA, exist_ok=True)

    db = _make_session()
    try:
        print("exporting news + tickers + stats …")
        news    = _ranked_rows(db)
        tickers = _ranked_tickers(db)
        stats   = _get_stats(db)
        print("exporting signals …")
        signals = _fundamental_rows(db)
        accuracy = _signal_accuracy(db)
    finally:
        db.close()

    generated = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    _write(f"{DATA}/news.json",    {"data": news})
    _write(f"{DATA}/tickers.json", {"data": tickers})
    _write(f"{DATA}/signals.json", {"data": signals, "accuracy": accuracy})
    _write(f"{DATA}/stats.json", {
        **stats,
        "narrative": _get_narrative(),
        "market": market_status(),
        "generated": generated,
    })

    # Per-ticker price + candles for every signal (so the charts work offline)
    syms = [s["ticker"] for s in signals]
    print(f"exporting price history + candles for {len(syms)} tickers …")
    ok = 0
    for i, sym in enumerate(syms, 1):
        safe = re.sub(r"[^A-Za-z0-9._-]", "", sym)
        try:
            p = get_price_stats(sym)
            if p:
                _write(f"{DATA}/price/{safe}.json", p)
            c = get_candles(sym)
            if c:
                _write(f"{DATA}/candles/{safe}.json", {"ticker": sym, "candles": c})
            if p or c:
                ok += 1
        except Exception as exc:
            print(f"  ! {sym}: {exc}")
        if i % 10 == 0:
            print(f"  {i}/{len(syms)}")
    print(f"price/candles exported for {ok}/{len(syms)} tickers")

    # The page itself, rendered once with the data inlined
    print("rendering index.html …")
    from src.dashboard.app import app
    with app.test_request_context("/"):
        from flask import render_template
        from config.settings import DASHBOARD_REFRESH, DASHBOARD_TOP_N
        html = render_template(
            "index.html",
            rows=news, stats=stats, tickers=tickers,
            narrative=_get_narrative(),
            refresh=DASHBOARD_REFRESH, top_n=DASHBOARD_TOP_N,
        )
    # Static mode: no SSE, fetch JSON files instead of the Flask API
    html = html.replace(
        "<head>",
        f'<head>\n<script>window.STATIC_MODE=true;window.GENERATED_AT="{generated}";</script>',
        1,
    )
    with open(f"{OUT}/index.html", "w") as f:
        f.write(html)

    # Serverless chatbot — the ONLY server-side piece (no ML deps, fits easily)
    os.makedirs(f"{OUT}/../api", exist_ok=True)
    print("\n✅ export complete →", OUT)
    print(f"   {len(news)} articles · {len(signals)} signals · {len(tickers)} tickers")
    print(f"   generated {generated}")
    print("\nNext:  vercel deploy --prod")


if __name__ == "__main__":
    main()
