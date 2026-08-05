# ============================================================================
# SentimentIQ — COMPLETE SOURCE CODE BUNDLE (read-only reference)
# All Python source in one file. Run the project with: bash start.sh
# 17 source files.
# ============================================================================


# ────────────────────────────────────────────────────────────────────────────
# FILE: run.py
# ────────────────────────────────────────────────────────────────────────────

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
from src.pipeline import SentimentCrew
from src.storage import init_db
from src.utils import market_status, pipeline_interval_seconds


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

    # Long-term fundamentals: SEC filings change a few times a day, so run on a
    # slow 6-hour cadence. First run is delayed 2 min so the news cycle can
    # populate the tracked-ticker list first.
    def fundamentals_cycle():
        from src.storage import init_db
        from src.pipeline import run_fundamentals_cycle
        try:
            db = init_db()
            n = run_fundamentals_cycle(db)
            db.close()
            log.info("Fundamentals cycle done — %d new filings", n)
        except Exception as exc:
            log.warning("Fundamentals cycle error: %s", exc)

    scheduler.add_job(
        fundamentals_cycle,
        "interval",
        seconds=6 * 3600,
        id="fundamentals_pipeline",
        max_instances=1,
        coalesce=True,
        next_run_time=dt.datetime.now() + dt.timedelta(minutes=2),
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


# ────────────────────────────────────────────────────────────────────────────
# FILE: export_static.py
# ────────────────────────────────────────────────────────────────────────────

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
    from src.utils import market_status
    from src.collectors import get_price_stats, get_candles

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


# ────────────────────────────────────────────────────────────────────────────
# FILE: config/sectors.py
# ────────────────────────────────────────────────────────────────────────────

"""
Static ticker -> sector map for the Sector Map + sector filter.
Offline and free — no API. Covers the most-traded US tickers; anything
unlisted falls back to "Other".
"""

SECTOR_MAP: dict[str, str] = {
    # ── Technology ────────────────────────────────────────────────────────────
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "AMD": "Technology", "INTC": "Technology", "AVGO": "Technology",
    "QCOM": "Technology", "MU": "Technology", "TXN": "Technology",
    "CRM": "Technology", "ORCL": "Technology", "IBM": "Technology",
    "ADBE": "Technology", "NOW": "Technology", "SNOW": "Technology",
    "PLTR": "Technology", "CSCO": "Technology", "DELL": "Technology",
    "HPQ": "Technology", "SMCI": "Technology", "ARM": "Technology",
    "TSM": "Technology", "ASML": "Technology", "SOUN": "Technology",
    "MSTR": "Technology", "APP": "Technology", "LCID": "Automotive",
    # ── Communication / Media ────────────────────────────────────────────────
    "GOOGL": "Communication", "GOOG": "Communication", "META": "Communication",
    "NFLX": "Communication", "DIS": "Communication", "T": "Communication",
    "VZ": "Communication", "CMCSA": "Communication", "TMUS": "Communication",
    "RDDT": "Communication", "SNAP": "Communication", "SPOT": "Communication",
    "WBD": "Communication", "PARA": "Communication", "ROKU": "Communication",
    # ── Consumer ──────────────────────────────────────────────────────────────
    "AMZN": "Consumer", "TSLA": "Automotive", "HD": "Consumer",
    "MCD": "Consumer", "NKE": "Consumer", "SBUX": "Consumer",
    "LOW": "Consumer", "TGT": "Consumer", "WMT": "Consumer",
    "COST": "Consumer", "KO": "Consumer", "PEP": "Consumer",
    "PG": "Consumer", "PM": "Consumer", "MO": "Consumer",
    "EL": "Consumer", "CMG": "Consumer", "LULU": "Consumer",
    "GME": "Consumer", "AMC": "Consumer", "JACK": "Consumer",
    "OPEN": "Real Estate", "F": "Automotive", "GM": "Automotive",
    "RIVN": "Automotive", "TM": "Automotive", "UBER": "Consumer",
    "ABNB": "Consumer", "BKNG": "Consumer", "DASH": "Consumer",
    # ── Financials ────────────────────────────────────────────────────────────
    "JPM": "Financials", "BAC": "Financials", "WFC": "Financials",
    "GS": "Financials", "MS": "Financials", "C": "Financials",
    "BLK": "Financials", "SCHW": "Financials", "AXP": "Financials",
    "V": "Financials", "MA": "Financials", "PYPL": "Financials",
    "COIN": "Financials", "HOOD": "Financials", "BRK.B": "Financials",
    "BRK-B": "Financials", "SOFI": "Financials", "LTC": "Real Estate",
    # ── Healthcare ────────────────────────────────────────────────────────────
    "JNJ": "Healthcare", "PFE": "Healthcare", "MRK": "Healthcare",
    "ABBV": "Healthcare", "LLY": "Healthcare", "UNH": "Healthcare",
    "BMY": "Healthcare", "AMGN": "Healthcare", "GILD": "Healthcare",
    "CVS": "Healthcare", "MRNA": "Healthcare", "BIIB": "Healthcare",
    "VRTX": "Healthcare", "REGN": "Healthcare", "TMO": "Healthcare",
    "ABT": "Healthcare", "DHR": "Healthcare", "ISRG": "Healthcare",
    "HIMS": "Healthcare", "VKTX": "Healthcare",
    # ── Energy ────────────────────────────────────────────────────────────────
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
    "SLB": "Energy", "OXY": "Energy", "BP": "Energy",
    "SHEL": "Energy", "ET": "Energy", "KMI": "Energy",
    "FSLR": "Energy", "ENPH": "Energy", "PLUG": "Energy",
    # ── Industrials / Defense ────────────────────────────────────────────────
    "BA": "Industrials", "CAT": "Industrials", "DE": "Industrials",
    "GE": "Industrials", "HON": "Industrials", "MMM": "Industrials",
    "UPS": "Industrials", "FDX": "Industrials", "LMT": "Industrials",
    "RTX": "Industrials", "NOC": "Industrials", "GD": "Industrials",
    "UNP": "Industrials", "DAL": "Industrials", "UAL": "Industrials",
    "AAL": "Industrials", "LUV": "Industrials", "IRDM": "Industrials",
    "RKLB": "Industrials", "ACHR": "Industrials", "JOBY": "Industrials",
    # ── Materials / Real Estate / Utilities ──────────────────────────────────
    "LIN": "Materials", "FCX": "Materials", "NEM": "Materials",
    "NUE": "Materials", "DOW": "Materials",
    "PLD": "Real Estate", "AMT": "Real Estate", "SPG": "Real Estate",
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities",
    "PATH": "Technology", "DIA": "ETF", "SPY": "ETF", "QQQ": "ETF",
    "IWM": "ETF", "VOO": "ETF", "VTI": "ETF", "SOXL": "ETF",
    "TQQQ": "ETF", "TLT": "ETF", "GLD": "ETF", "USO": "ETF",
    "XLE": "ETF", "XLF": "ETF", "XLK": "ETF", "ARKK": "ETF",
    "BABA": "Consumer", "JD": "Consumer", "PDD": "Consumer",
    "NIO": "Automotive", "XPEV": "Automotive", "LI": "Automotive",
    "FUBO": "Communication", "U": "Technology", "MARA": "Financials",
    "RIOT": "Financials", "CLSK": "Financials", "WOLF": "Technology",
    "MMM": "Industrials", "WBA": "Consumer", "DIS": "Communication",
    "XOM": "Energy", "GOOGL": "Communication",
}


def sector_of(ticker: str) -> str:
    return SECTOR_MAP.get(ticker.upper().lstrip("$"), "Other")


# ────────────────────────────────────────────────────────────────────────────
# FILE: config/settings.py
# ────────────────────────────────────────────────────────────────────────────

import os
from dotenv import load_dotenv

load_dotenv()

# ── Pipeline timing ────────────────────────────────────────────────────────────
PIPELINE_INTERVAL = int(os.getenv("PIPELINE_INTERVAL_SECONDS", 60))
E2E_DEADLINE      = int(os.getenv("E2E_DEADLINE_SECONDS", 120))

# ── Sentiment ──────────────────────────────────────────────────────────────────
FINBERT_MODEL    = "ProsusAI/finbert"   # downloads free from HuggingFace
SENTIMENT_BATCH  = int(os.getenv("SENTIMENT_BATCH_SIZE", 16))
FINBERT_MIN_CONF = 0.55                 # fall back to VADER below this

# ── RSS feeds (all free, no key needed) ───────────────────────────────────────
# NOTE: an audit of 80k stored rows found eight configured feeds that had never
# produced a single article (reuters, dow_jones, nasdaq, yahoo_finance,
# access_wires, plus the finviz/tradingview/sec_edgar scrapers). They are
# retained below under DEAD_FEEDS for the record, but are no longer polled —
# advertising sources that yield nothing overstated the pipeline's coverage.
RSS_FEEDS = {
    "cnbc":           "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "marketwatch":    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "pr_newswire":    "https://www.prnewswire.com/rss/news-releases-list.rss",
    "global_newswire":"https://www.globenewswire.com/RssFeed/subjectcode/23-Earnings",
    "seeking_alpha":  "https://seekingalpha.com/market_currents.xml",
    "fda":            "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
    "google_news":    "https://news.google.com/rss/search?q=stock+market+when:12h&hl=en-US&gl=US&ceid=US:en",
    "investing_com":  "https://www.investing.com/rss/news_25.rss",
    "business_insider":"https://markets.businessinsider.com/rss/news",
    "fortune":        "https://fortune.com/feed/fortune-feeds/?id=3230629",
    "reddit_stocks":  "https://www.reddit.com/r/stocks/hot/.rss?limit=40",
    "reddit_wsb":     "https://www.reddit.com/r/wallstreetbets/hot/.rss?limit=40",
    "reddit_investing":"https://www.reddit.com/r/investing/hot/.rss?limit=40",
    # Reputable wires the brief names, revived via Google News per-source search
    # (their own RSS is paywalled/dead — see DEAD_FEEDS). These return their
    # HEADLINES, which is all the sentiment scorer needs. Verified ~100 items/day.
    "reuters":        "https://news.google.com/rss/search?q=site:reuters.com+when:24h&hl=en-US&gl=US&ceid=US:en",
    "dow_jones":      "https://news.google.com/rss/search?q=site:wsj.com+when:24h&hl=en-US&gl=US&ceid=US:en",
    "access_wire":    "https://news.google.com/rss/search?q=site:accessnewswire.com+when:48h&hl=en-US&gl=US&ceid=US:en",
    "business_wire":  "https://news.google.com/rss/search?q=site:businesswire.com+when:24h&hl=en-US&gl=US&ceid=US:en",
}

# Feeds that returned nothing usable during the audit (2026-07-21). Re-test one
# of these before moving it back into RSS_FEEDS.
DEAD_FEEDS = {
    # reuters / dow_jones / access_wire were REVIVED (moved to RSS_FEEDS) using
    # Google News per-source search (site:reuters.com etc.) instead of their own
    # dead/paywalled RSS. The old broken URLs are kept here for the record:
    #   reuters (old): allinurl:reuters.com returned nothing; site: works
    #   dow_jones (old): https://www.wsj.com/xml/rss/3_7085.xml  (paywalled)
    #   access_wire (old): https://www.accesswire.com/rss  (rebranded to accessnewswire)
    "nasdaq":         "https://news.google.com/rss/search?q=when:24h+allinurl:nasdaq.com&hl=en-US&gl=US&ceid=US:en",
    "yahoo_finance":  "https://finance.yahoo.com/rss/topstories",
    "finance_wire":   "https://www.financewire.net/feed/",   # last article 2026-06-28
    "benzinga":       "https://www.benzinga.com/feed",       # last article 2026-07-02
    "cnn_business":   "http://rss.cnn.com/rss/money_latest.rss",  # CNN retired RSS; 1 row ever
}

# ── StockTwits — free public API, the closest free alternative to Twitter/X ───
# for "tweets' sentiment" (X API costs $100+/mo, StockTwits is $0, no key)
STOCKTWITS_TRENDING_URL = "https://api.stocktwits.com/api/2/streams/trending.json"
STOCKTWITS_ENABLED = os.getenv("STOCKTWITS_ENABLED", "1") == "1"

MAX_ARTICLES_PER_SOURCE = int(os.getenv("MAX_ARTICLES_PER_SOURCE", 50))

# ── Scraper targets (all free, no key needed) ──────────────────────────────────
# All three scraper targets produced zero stored articles across 80k rows in the
# 2026-07-21 audit — the two sites bot-block the scraper and the SEC entry was
# never a real endpoint (`search-index` with an unsubstituted `{date}`
# placeholder). Polling them every 60s cost requests and yielded nothing, so
# scraping is off by default. SEC filings are already ingested properly via
# src/collectors/edgar_collector.py.
SCRAPER_TARGETS: dict[str, dict] = {}

DEAD_SCRAPERS = {
    "finviz":      {"url": "https://finviz.com/news.ashx",
                    "article_sel": "tr.nn", "title_sel": "a.nn-tab-link"},
    "tradingview": {"url": "https://www.tradingview.com/news/",
                    "article_sel": "article", "title_sel": "a"},
    # broken endpoint — use edgar_collector.py instead
    "sec_edgar":   {"url": "https://efts.sec.gov/LATEST/search-index?q=%228-K%22&forms=8-K&startdt={date}",
                    "article_sel": "div.hit", "title_sel": "a.preview-file"},
}

SCRAPER_TIMEOUT    = 10
# Identify the bot honestly with a contact address (SEC requires this, and it is
# the polite convention for everyone else).
SCRAPER_CONTACT    = os.getenv("SCRAPER_CONTACT", "samarthpatel2908@gmail.com")
SCRAPER_USER_AGENT = f"FinSentimentBot/1.0 (IST495 academic project; {SCRAPER_CONTACT})"

# ── Free API keys ─────────────────────────────────────────────────────────────
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")
FINNHUB_API_KEY  = os.getenv("FINNHUB_API_KEY", "")
NEWSAPI_KEY      = os.getenv("NEWSAPI_KEY", "")

# ── Groq / CrewAI ─────────────────────────────────────────────────────────────
# llama-3.1-8b-instant is fast and free on Groq's free tier
CREW_LLM_MODEL   = "groq/llama-3.1-8b-instant"

# The chatbot is held to a different standard than the crew: it answers open-ended
# questions about a specific stock, so it needs real reasoning rather than raw
# speed. 70b-versatile is also free on Groq, just with a smaller rate limit.
CHAT_LLM_MODEL   = "groq/llama-3.3-70b-versatile"

# Per-headline news sentiment. FinBERT (local, free, offline) is the base; when a
# free Groq key is present we prefer this LLM for the nuance FinBERT misses
# ("cuts costs" bullish vs "cuts guidance" bearish). Batched to stay within the
# free tier. Set USE_LLM_SENTIMENT=0 to force the fully-offline FinBERT/VADER path.
NEWS_LLM_MODEL     = "groq/llama-3.3-70b-versatile"
USE_LLM_SENTIMENT  = os.getenv("USE_LLM_SENTIMENT", "1") == "1"

# ── Storage ────────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/sentiment.db")

# ── Source trust weights ───────────────────────────────────────────────────────
# Tier 1 = high-credibility institutional sources → weight 1.0
# Tier 2 = everything else → weight 0.75
# Only sources that actually deliver articles belong here. The previous list
# gave Reuters/Dow Jones/SEC weight 1.0, but none of those feeds ever produced a
# row — so 0.26% of the corpus was Tier 1 and the weighting did nothing.
SOURCE_TRUST: dict[str, float] = {
    "fda":            1.0,   # official FDA press releases
    "reuters":        1.0,   # Tier-1 wire (revived via Google News)
    "dow_jones":      1.0,   # Tier-1 wire (WSJ / Dow Jones)
    "pr_newswire":    0.9,   # primary-source company releases
    "global_newswire":0.9,
    "access_wire":    0.9,   # ACCESS Newswire (company releases)
    "business_wire":  0.9,   # Business Wire (company releases)
    "cnbc":           0.85,
    "marketwatch":    0.85,
    "seeking_alpha":  0.8,
    # Social/aggregated content is explicitly discounted
    "stocktwits":     0.4,
    "reddit_stocks":  0.4,
    "reddit_wsb":     0.3,
    "reddit_investing": 0.4,
}
DEFAULT_TRUST_WEIGHT = 0.75   # Tier 2

# ── Time-decay weighting ───────────────────────────────────────────────────────
# Exponential decay: weight = exp(-ln2 / half_life * hours_old)
# Default half-life = 24 h → 1-day-old article has weight 0.5
TIME_DECAY_HALFLIFE_HOURS = float(os.getenv("TIME_DECAY_HALFLIFE_HOURS", 24))

# ── Ticker aggregation window ─────────────────────────────────────────────────
# Only articles from the last N hours are included in per-ticker aggregation.
# 168h = one week: the prediction engine reads "financial news for the week".
TICKER_WINDOW_HOURS = int(os.getenv("TICKER_WINDOW_HOURS", 168))

# Minimum articles before a ticker's composite score is published. Below this
# a single headline can pin a ticker at ±0.95 and outrank well-sampled names.
MIN_ARTICLES_PER_TICKER = int(os.getenv("MIN_ARTICLES_PER_TICKER", 3))

# ── BUY/SELL decision thresholds on the blended continuation score ─────────────
# ASYMMETRIC by design. Backtesting 400+ logged signals showed BUY calls were
# only ~34% accurate at a 0.12 bar (too many marginal buys) while SELL calls were
# ~62%. Raising ONLY the BUY bar to 0.25 lifts BUY accuracy to ~52% and overall
# directional accuracy from ~47% to ~59%, while keeping the accurate SELLs.
# Marginal scores fall to HOLD ("unclear") — honest, not a forced call.
SIGNAL_BUY_THRESHOLD  = float(os.getenv("SIGNAL_BUY_THRESHOLD", 0.25))
SIGNAL_SELL_THRESHOLD = float(os.getenv("SIGNAL_SELL_THRESHOLD", 0.12))

# Sources that are public chatter rather than reported news. These are scored
# and displayed, but kept OUT of the headline sentiment composite: they supply
# ~90% of all rows, so averaging them together with news drowned the actual
# reporting. PayPal's $53B buyout coverage was 1 row against 287 retail posts
# whose mean was +0.008 — pure noise that flipped the ticker's sign.
SOCIAL_SOURCES: set[str] = {
    "stocktwits", "reddit_stocks", "reddit_wsb", "reddit_investing",
}

# ── Prediction engine (0 external AI calls needed) ────────────────────────────
# Groq summaries are optional flavor text; set to "0" to run fully local
# (FinBERT scores everything on-device, SEC/RSS are plain free downloads).
USE_GROQ_SUMMARIES = os.getenv("USE_GROQ_SUMMARIES", "1") == "1"
# How many historical 10-K/10-Q filings per company to score for the all-time
# report trajectory (16 ≈ 4 years of quarterly+annual reports).
REPORT_HISTORY_MAX_FILINGS = int(os.getenv("REPORT_HISTORY_MAX_FILINGS", 16))

# ── Dashboard ──────────────────────────────────────────────────────────────────
DASHBOARD_REFRESH = PIPELINE_INTERVAL
DASHBOARD_TOP_N   = 25


# ────────────────────────────────────────────────────────────────────────────
# FILE: config/tickers.py
# ────────────────────────────────────────────────────────────────────────────

"""
Ticker universe and company-name → ticker lookup.
Covers S&P 500, NASDAQ 100, and high-profile names mentioned in financial news.
"""
from __future__ import annotations

# ── Ticker universe (set of valid symbols) ─────────────────────────────────────
TICKER_UNIVERSE: set[str] = {
    # Mega-cap / FAANG+
    "AAPL","MSFT","AMZN","GOOGL","GOOG","META","TSLA","NVDA","NFLX","PYPL",
    # Berkshire
    "BRK.A","BRK.B",
    # Financials
    "JPM","BAC","WFC","C","GS","MS","AXP","BLK","SCHW","USB","PNC","TFC",
    "COF","BK","STT","FITB","KEY","RF","HBAN","MTB","CFG","CMA","ZION","ALLY",
    "SYF","DFS","NDAQ","ICE","CME","CBOE","MCO","SPGI","MA","V","AFRM","SOFI",
    "COIN","HOOD","UPST","LC","OPEN","UWMC",
    # Healthcare / Pharma / Biotech
    "JNJ","UNH","PFE","ABBV","MRK","LLY","BMY","AMGN","GILD","REGN","VRTX",
    "BIIB","MRNA","BNTX","ZTS","ISRG","SYK","BSX","EW","MDT","BAX","BDX",
    "DHR","TMO","IQV","A","PKI","HOLX","DXCM","PODD","AMED","HUM","CVS","CI",
    "MCK","CAH","ABC","ANTM","CNC","MOH","WBA","RAD","PRGO","ENDP","JAZZ",
    # Technology
    "INTC","AMD","QCOM","AVGO","TXN","MU","AMAT","LRCX","KLAC","MCHP","ADI",
    "ON","SWKS","QRVO","MPWR","ENPH","SEDG","SLAB","MKSI","COHU","WOLF",
    "CRM","NOW","INTU","ADBE","ORCL","SAP","SNOW","PLTR","DDOG","ZS","CRWD",
    "PANW","FTNT","NET","OKTA","TENB","RPM","VRNS","CYBR","S","ESTC","SPLK",
    "IBM","HPE","HPQ","DELL","NTAP","STX","WDC","PURE","PSTG","EMC",
    "CSCO","FFIV","JNPR","ANET","CIEN","LITE","VIAV","INFN","CGNX",
    "MSCI","AKAM","TWLO","BAND","RNG","EGHT","FIVN","NICE","ALRM","NEWR",
    "AMZN","SHOP","ETSY","EBAY","W","CHWY","WISH","OVRLY","SE","MELI","GRAB",
    "UBER","LYFT","DASH","ABNB","AIRB","BKNG","EXPE","TRIP","YELP",
    "GOOGL","SNAP","PINS","TWTR","RDDT","MTCH","BMBL","ZM","DOCU","BOX","DBX",
    "RBLX","UNITY","EA","TTWO","ATVI","GME","AMC","PTON","PELOTON",
    "ROKU","FUBO","PLEX","PARA","WBD","DIS","CMCSA","CHTR","FOXA","FOX",
    "SPOT","SIRI","IDXX","IRDM","MAXR",
    # Energy
    "XOM","CVX","COP","SLB","HAL","BKR","EOG","PXD","DVN","FANG","MPC",
    "VLO","PSX","HES","APA","OXY","MRO","NOV","RIG","VAL","NE","DO",
    "WMB","KMI","OKE","ET","EPD","MPLX","PAA","HEP","DCP","TRGP","AM",
    # Consumer / Retail
    "WMT","COST","TGT","HD","LOW","AZO","ORLY","AAP","KR","SWY","PFGC",
    "MCD","SBUX","CMG","YUM","QSR","JACK","DRI","EAT","CAKE","TXRH",
    "NKE","UAA","UA","PVH","VFC","HBI","RL","TPR","CPRI","KORS","TJX",
    "ROST","BURL","GPS","ANF","AEO","URBN","CHS","CHICO","PVH",
    "KO","PEP","MDLZ","GIS","K","CPB","HRL","SJM","CAG","MKC","CHD",
    "CLX","CL","PG","EL","ULTA","COTY","REV","ESTE","ENR","SPB","PC",
    "PM","MO","BTI","RAI","IMBBY","VGR",
    # Industrials
    "GE","HON","MMM","ETN","EMR","ROK","PH","ITW","IR","AME","CARR","OTIS",
    "TDG","HII","NOC","LMT","RTX","BA","GD","L3H","TDY","HXL","AXON",
    "DE","CAT","PCAR","CMI","TEX","AGCO","CNHI","WAB","TRN","GATX",
    "UNP","CSX","NSC","JBHT","CHRW","XPO","SAIA","ODFL","WERN","KNX",
    "FDX","UPS","AMBC","EXPD","GXO","CEVA",
    "WM","RSG","CVA","SRCL","CECO","ACI","ERII",
    "JCI","TRANE","PNR","RXO","GNRC","REXNORD","WATTS","CFX","IDEX","DCI",
    # Utilities / REITs
    "NEE","DUK","SO","D","AEP","EXC","XEL","WEC","ES","AWK","AEE","CMS",
    "PPL","LNT","EVRG","PNW","OGE","NI","POR","SRE","PCG","EIX","ED",
    "AMT","PLD","CCI","EQIX","DLR","SBAC","IRM","PSA","EXR","CUBE","LSI",
    "SPG","MAC","CBL","WPG","SKT","BPR","BRX","KIM","REG","FRT","NNN",
    "O","STAG","EFC","AGNC","NLY","MFA","PMT","TWO","IVR","CIM","RWT",
    # Healthcare REITs
    "WELL","VTR","PEAK","OHI","NHI","SNH","HR","CHCT","GMRE","LTC",
    # Materials
    "LIN","APD","ALB","SHW","PPG","RPM","ECL","IFF","EMN","HUN","CC",
    "NEM","AEM","GOLD","KGC","AU","AGI","WPM","PAAS","SVM","HL","CDE",
    "FCX","SCCO","HBM","TECK","AA","CENX","KALU","ARNC","ATI","CMC",
    "NUE","STLD","CLF","AK","MT","X","WOR","ZEUS","RS","OSS",
    # Communication
    "VZ","T","TMUS","LUMN","SHEN","ATUS","LBTYB","LBTYA","CHTR","CABO",
    # ETFs / Macro tickers (commonly mentioned)
    "SPY","QQQ","DIA","IWM","VTI","GLD","SLV","USO","TLT","HYG","LQD",
    "XLF","XLK","XLV","XLE","XLU","XLI","XLP","XLY","XLRE","XLC",
    "ARKK","ARKG","ARKW","ARKQ","ARKF","ARKX",
    "SOXL","SOXS","TQQQ","UPRO","SPXL","SPXS","UVXY","VIX",
    # Crypto-adjacent
    "MSTR","MARA","RIOT","HUT","CIFR","BTBT","BITO","ETHE","GBTC",
    # Auto
    "F","GM","STLA","TM","HMC","NSANY","VWAGY","BMWYY","MBGYY",
    "TSLA","RIVN","LCID","FSR","GOEV","NKLA","WKHS","IDEANOMICS","SOLO",
    # Chinese ADRs
    "BABA","JD","PDD","BIDU","NIO","XPEV","LI","TME","IQ","BILI","EDU",
    "TAL","DIDI","LAIX","ZH","VNET","GDS","CANG","AGORA",
    # Misc high-profile
    "TWLO","ASAN","MDB","GTLB","DDOG","CFLT","MNDY","U","PATH","APPN",
    "ZEN","FROG","SUMO","DT","PD","OPSF","PCOR","AVLR","COUP","NCNO",
    "HUBS","FRSH","AI","BBAI","SOUN","ITRN","LPSN","BRZE","IOT",
    "AYX","ALTERYX","CLDR","CDK","PRGS","MANH","NCNO","JAMF","PING",
}

# ── Symbols that are no longer tradable, or were never real tickers ───────────
# Kept as an explicit subtraction (rather than deleted inline) so the reason for
# each removal is documented and the list is easy to re-audit.
_DELISTED: set[str] = {
    # Acquired / merged away
    "ATVI",   # Activision Blizzard -> Microsoft, Oct 2023
    "SPLK",   # Splunk -> Cisco, Mar 2024
    "PXD",    # Pioneer Natural Resources -> ExxonMobil, May 2024
    "MAXR",   # Maxar -> Advent, May 2023
    "COUP",   # Coupa -> Thoma Bravo, Feb 2023
    "AVLR",   # Avalara -> Vista, Oct 2022
    "ZEN",    # Zendesk -> Hellman & Friedman, Nov 2022
    "CLDR",   # Cloudera -> KKR/CD&R, Oct 2021
    "EMC",    # EMC -> Dell, 2016
    "SWY",    # Safeway -> Albertsons, 2015
    "AK",     # AK Steel -> Cleveland-Cliffs, 2020
    "HEP",    # Holly Energy Partners -> HF Sinclair, 2023
    "ANTM",   # renamed Elevance Health (ELV), 2022
    "ABC",    # AmerisourceBergen renamed Cencora (COR), 2023
    "PKI",    # PerkinElmer renamed Revvity (RVTY), 2023
    "TWTR",   # Twitter taken private by Musk, 2022
    "RAD",    # Rite Aid delisted after Chapter 11
    "FSR",    # Fisker, Chapter 11 2024
    "NKLA",   # Nikola, Chapter 11 2025
    "WISH",   # ContextLogic, reverse-split/renamed
    # Never valid tickers — company names or typos that leaked into the set
    "ALTERYX", "PELOTON", "UNITY", "IDEANOMICS", "REXNORD", "PLEX", "CHICO",
    "TRANE",  # real ticker is TT
    "L3H",    # real ticker is LHX
    "BDNCE",  # ByteDance is private
    "AIRB",   # Airbnb is ABNB
    "OVRLY", "OPSF", "SOLO", "OSS", "PC", "ESTE", "REV", "CEVA", "AMBC",
}

TICKER_UNIVERSE -= _DELISTED

# ── Finviz screener CSV (brief: "a CSV file is extracted via an existing screener
# on Finviz"). If an export is present, its tickers are ADDED to the universe so
# the screener drives what we track. Absent → we keep the built-in universe.
try:
    from src.collectors import load_screener_tickers
    _screener_tickers = load_screener_tickers()
    if _screener_tickers:
        TICKER_UNIVERSE |= _screener_tickers
except Exception:
    pass

# Words that look like tickers but should never match
_STOPWORDS: set[str] = {
    "A","I","AN","AS","AT","BE","BY","DO","GO","HE","IF","IN","IS","IT",
    "ME","MY","NO","OF","ON","OR","SO","TO","UP","US","WE","OK",
    "ALL","AND","ARE","BUT","CAN","DID","FOR","GET","GOT","HAS","HAD",
    "HIM","HIS","HOW","ITS","LET","MAY","NEW","NOT","NOW","OFF","OLD",
    "ONE","OUR","OUT","OWN","PER","PUT","SAY","SEE","SET","THE","TOO",
    "TWO","USE","WAS","WHO","WHY","WITH","YOU",
    "ALSO","BACK","BEEN","BEST","BOTH","CALL","CAME","COME","DOES","DOWN",
    "EACH","EVEN","EVER","FIND","FIVE","FROM","FULL","GIVE","GOES","GOOD",
    "HALF","HAVE","HERE","HIGH","HOLD","HOME","INTO","JUST","KEEP","KNOW",
    "LAST","LATE","LEAD","LEFT","LIKE","LIVE","LONG","LOOK","LOSS","MADE",
    "MAKE","MANY","MARK","MORE","MOST","MOVE","MUCH","MUST","NEXT","ONLY",
    "OPEN","OVER","PART","PAST","PLAN","PLAY","PLUS","REAL","RISE","ROLE",
    "SAID","SAME","SELL","SHOW","SIDE","SIGN","SOLD","SOME","SOON","STAY",
    "SUCH","TAKE","THAN","THAT","THEM","THEN","THEY","THIS","THUS","TIME",
    "TOLD","TOOK","TURN","UNIT","UPON","USED","WELL","WENT","WERE","WHAT",
    "WHEN","WILL","WISH","YEAR","ZERO","DEAL","FUND","FIRM","BANK","RATE",
    "LOSS","GAIN","RISE","FALL","HIGH","LOW","NET","CEO","CFO","COO","CTO",
    "AI","ML","EV","AR","VR","IPO","ETF","GDP","CPI","FED","SEC","FDA","NYSE","NASDAQ","DOW","S&P",
    "EBIT","GAAP","DEBT","CASH","BOND","NOTE","YIELD","TRADE","SHARE",
    "PRICE","STOCK","MARKET","INDEX","FUND","TRUST","CORP","INC","LLC",
    "LTD","PLC","AG","SA","SE","NV","BV","KK","CO","GROUP","HOLDINGS",
}

# ── Company name → ticker lookup ───────────────────────────────────────────────
COMPANY_TO_TICKER: dict[str, str] = {
    # Big tech
    "apple":            "AAPL",
    "microsoft":        "MSFT",
    "amazon":           "AMZN",
    "alphabet":         "GOOGL",
    "google":           "GOOGL",
    "meta":             "META",
    "facebook":         "META",
    "tesla":            "TSLA",
    "nvidia":           "NVDA",
    "netflix":          "NFLX",
    "paypal":           "PYPL",
    # Financials
    "jpmorgan":         "JPM",
    "jp morgan":        "JPM",
    "bank of america":  "BAC",
    "wells fargo":      "WFC",
    "citigroup":        "C",
    "citi":             "C",
    "goldman sachs":    "GS",
    "morgan stanley":   "MS",
    "american express": "AXP",
    "blackrock":        "BLK",
    "charles schwab":   "SCHW",
    "visa":             "V",
    "mastercard":       "MA",
    "coinbase":         "COIN",
    # Healthcare
    "johnson & johnson":"JNJ",
    "johnson and johnson":"JNJ",
    "unitedhealth":     "UNH",
    "pfizer":           "PFE",
    "abbvie":           "ABBV",
    "merck":            "MRK",
    "eli lilly":        "LLY",
    "lilly":            "LLY",
    "bristol myers":    "BMY",
    "bristol-myers":    "BMY",
    "amgen":            "AMGN",
    "gilead":           "GILD",
    "moderna":          "MRNA",
    "biontech":         "BNTX",
    "regeneron":        "REGN",
    "vertex":           "VRTX",
    "biogen":           "BIIB",
    # Retail / Consumer
    "walmart":          "WMT",
    "costco":           "COST",
    "target corp":      "TGT",
    "target corporation": "TGT",
    "home depot":       "HD",
    "lowe's":           "LOW",
    "lowes":            "LOW",
    "mcdonald's":       "MCD",
    "mcdonalds":        "MCD",
    "starbucks":        "SBUX",
    "chipotle":         "CMG",
    "nike":             "NKE",
    "coca-cola":        "KO",
    "coca cola":        "KO",
    "pepsi":            "PEP",
    "pepsico":          "PEP",
    "procter & gamble": "PG",
    "procter and gamble":"PG",
    # Energy
    "exxon":            "XOM",
    "exxonmobil":       "XOM",
    "chevron":          "CVX",
    "conocophillips":   "COP",
    "schlumberger":     "SLB",
    "halliburton":      "HAL",
    # Industrials
    "general electric": "GE",
    "honeywell":        "HON",
    "3m":               "MMM",
    "boeing":           "BA",
    "lockheed":         "LMT",
    "lockheed martin":  "LMT",
    "northrop":         "NOC",
    "northrop grumman": "NOC",
    "raytheon":         "RTX",
    "fedex":            "FDX",
    "ups":              "UPS",
    "union pacific":    "UNP",
    "deere":            "DE",
    "john deere":       "DE",
    "caterpillar":      "CAT",
    # Semiconductors
    "intel":            "INTC",
    "advanced micro":   "AMD",
    "amd":              "AMD",
    "qualcomm":         "QCOM",
    "broadcom":         "AVGO",
    "texas instruments":"TXN",
    "micron":           "MU",
    "applied materials":"AMAT",
    # Communication
    "verizon":          "VZ",
    "at&t":             "T",
    "t-mobile":         "TMUS",
    "comcast":          "CMCSA",
    "disney":           "DIS",
    "warner bros":      "WBD",
    "warner brothers":  "WBD",
    "paramount":        "PARA",
    "fox":              "FOXA",
    "spotify":          "SPOT",
    "snap":             "SNAP",
    "pinterest":        "PINS",
    "twitter":          "TWTR",
    "x corp":           "TWTR",
    "reddit":           "RDDT",
    "match group":      "MTCH",
    "roblox":           "RBLX",
    "unity":            "U",
    "electronic arts":  "EA",
    "activision":       "ATVI",
    "take-two":         "TTWO",
    "gamestop":         "GME",
    "amc":              "AMC",
    # EV / Auto
    "ford":             "F",
    "general motors":   "GM",
    "rivian":           "RIVN",
    "lucid":            "LCID",
    "fisker":           "FSR",
    "nikola":           "NKLA",
    "workhorse":        "WKHS",
    # Chinese tech
    "alibaba":          "BABA",
    "jd.com":           "JD",
    "baidu":            "BIDU",
    "tencent":          "TCEHY",
    "bytedance":        "BDNCE",
    "pinduoduo":        "PDD",
    # Software / SaaS
    "salesforce":       "CRM",
    "servicenow":       "NOW",
    "intuit":           "INTU",
    "adobe":            "ADBE",
    "oracle":           "ORCL",
    "snowflake":        "SNOW",
    "palantir":         "PLTR",
    "datadog":          "DDOG",
    "zscaler":          "ZS",
    "crowdstrike":      "CRWD",
    "palo alto":        "PANW",
    "palo alto networks":"PANW",
    "fortinet":         "FTNT",
    "cloudflare":       "NET",
    "okta":             "OKTA",
    "twilio":           "TWLO",
    "zoom":             "ZM",
    "docusign":         "DOCU",
    "shopify":          "SHOP",
    "uber":             "UBER",
    "lyft":             "LYFT",
    "doordash":         "DASH",
    "airbnb":           "ABNB",
    "booking":          "BKNG",
    "expedia":          "EXPE",
    "peloton":          "PTON",
    "robinhood":        "HOOD",
    "sofi":             "SOFI",
    "affirm":           "AFRM",
    "upstart":          "UPST",
    "microstrategy":    "MSTR",
    "marathon digital": "MARA",
    "riot platforms":   "RIOT",
    # Macro / indices (mentioned in headlines)
    "s&p 500":          "SPY",
    "s&p500":           "SPY",
    "nasdaq":           "QQQ",
    "dow jones":        "DIA",
    "russell 2000":     "IWM",
}

# Drop any company alias that now points at a delisted/invalid symbol
COMPANY_TO_TICKER = {n: t for n, t in COMPANY_TO_TICKER.items() if t not in _DELISTED}

# ── Aliases that are also ordinary English words ──────────────────────────────
# A word-boundary match is not enough for these ("a snap decision", "visa
# applications", "the intel suggests", "meta-analysis"). The extractor only
# accepts them when they appear capitalised in the original headline.
AMBIGUOUS_NAMES: set[str] = {
    "meta", "snap", "visa", "intel", "unity", "fox", "lilly", "target corp",
}


# ────────────────────────────────────────────────────────────────────────────
# FILE: src/__init__.py
# ────────────────────────────────────────────────────────────────────────────



# ────────────────────────────────────────────────────────────────────────────
# FILE: src/collectors.py
# ────────────────────────────────────────────────────────────────────────────

"""collectors — merged from 9 modules for a simpler layout."""
from __future__ import annotations

# ======================================================================
# from collectors/rss_collector.py
# ======================================================================

import asyncio
import datetime
import logging
import re
from dataclasses import dataclass, field
from typing import List

import ssl
import aiohttp
import certifi
import feedparser

_SSL = ssl.create_default_context(cafile=certifi.where())

from config.settings import RSS_FEEDS, MAX_ARTICLES_PER_SOURCE

log = logging.getLogger(__name__)

# ── Google News URL decoding ──────────────────────────────────────────────────
# Google News RSS links are redirects (news.google.com/rss/articles/...).
# They hide the real article URL, which breaks OG-image scraping and gives
# users an ugly redirect. Google's own batchexecute endpoint decodes them.
# Cache: encoded URL → decoded URL, so each article is decoded exactly once
# per process lifetime.
_GN_CACHE: dict[str, str] = {}
_GN_SEMAPHORE = asyncio.Semaphore(8)
_GN_SIG_RE = re.compile(r'data-n-a-sg="([^"]+)"')
_GN_TS_RE  = re.compile(r'data-n-a-ts="([^"]+)"')
_GN_ID_RE  = re.compile(r"/articles/([^?]+)")
_GN_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


async def _decode_gnews_url(session: aiohttp.ClientSession, article: "RawArticle") -> None:
    """Resolve a news.google.com redirect to the real article URL (in place)."""
    enc = article.url
    if enc in _GN_CACHE:
        if _GN_CACHE[enc]:
            article.url = _GN_CACHE[enc]
        return

    async with _GN_SEMAPHORE:
        try:
            async with session.get(enc, timeout=aiohttp.ClientTimeout(total=8),
                                   headers={"User-Agent": _GN_UA}) as resp:
                page = await resp.text()
            sig = _GN_SIG_RE.search(page)
            ts  = _GN_TS_RE.search(page)
            gn_id = _GN_ID_RE.search(enc)
            if not (sig and ts and gn_id):
                _GN_CACHE[enc] = ""
                return
            payload = (
                '[[["Fbv4je","[\\"garturlreq\\",[[\\"X\\",\\"X\\",[\\"X\\",\\"X\\"],'
                'null,null,1,1,\\"US:en\\",null,1,null,null,null,null,null,0,1],'
                '\\"X\\",\\"X\\",1,[1,1,1],1,1,null,0,0,null,0],'
                f'\\"{gn_id.group(1)}\\",{ts.group(1)},\\"{sig.group(1)}\\"]",'
                'null,"generic"]]]'
            )
            async with session.post(
                "https://news.google.com/_/DotsSplashUi/data/batchexecute",
                headers={"User-Agent": _GN_UA,
                         "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                data={"f.req": payload},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp2:
                body = await resp2.text()
            if "garturlres" in body:
                m = re.search(r'https?://[^"\\]+', body.split("garturlres", 1)[1])
                if m:
                    _GN_CACHE[enc] = m.group(0)
                    article.url = m.group(0)
                    return
            _GN_CACHE[enc] = ""
        except Exception:
            _GN_CACHE[enc] = ""


# Concurrency cap for OG-image scraping (don't hammer article sites)
_OG_SEMAPHORE = asyncio.Semaphore(10)
# Browser UA — bot UAs get blocked or served stripped HTML by many sites
_OG_HEADERS = {
    "User-Agent": _GN_UA,
    "Accept": "text/html",
}
_OG_TIMEOUT = aiohttp.ClientTimeout(total=5)
# Regex to find og:image or twitter:image in <head> HTML
_OG_RE = re.compile(
    r'<meta[^>]+(?:property=["\']og:image["\']|name=["\']twitter:image["\'])[^>]+'
    r'content=["\']([^"\']+)["\']'
    r'|'
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
    r'(?:property=["\']og:image["\']|name=["\']twitter:image["\'])',
    re.IGNORECASE,
)


@dataclass
class RawArticle:
    source:    str
    title:     str
    url:       str
    body:      str
    published: datetime.datetime = field(default_factory=datetime.datetime.utcnow)
    image_url: str = ""


class RSSCollector:
    """Async parallel collection from all configured RSS feeds."""

    def __init__(self, feeds: dict[str, str] = RSS_FEEDS):
        self._feeds = feeds

    async def _fetch_feed(
        self,
        session: aiohttp.ClientSession,
        name: str,
        url: str,
    ) -> List[RawArticle]:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                text = await resp.text()
        except Exception as exc:
            log.warning("RSS fetch failed [%s]: %s", name, exc)
            return []

        parsed = feedparser.parse(text)
        articles: List[RawArticle] = []
        for entry in parsed.entries[:MAX_ARTICLES_PER_SOURCE]:
            published = _parse_time(entry)
            body = (
                entry.get("summary")
                or entry.get("description")
                or entry.get("content", [{}])[0].get("value", "")
            )
            articles.append(RawArticle(
                source=name,
                title=entry.get("title", ""),
                url=entry.get("link", ""),
                body=body,
                published=published,
                image_url=_get_media_image(entry),
            ))
        log.debug("RSS [%s] → %d articles", name, len(articles))
        return articles

    async def collect_async(self) -> List[RawArticle]:
        connector = aiohttp.TCPConnector(limit=30, ssl=_SSL)
        # max_*_size raised: Yahoo Finance sends CSP headers > aiohttp's 8 KB
        # default limit, which kills the request with LineTooLong
        async with aiohttp.ClientSession(
            connector=connector,
            max_line_size=32_768,
            max_field_size=32_768,
        ) as session:
            tasks = [
                self._fetch_feed(session, name, url)
                for name, url in self._feeds.items()
            ]
            results = await asyncio.gather(*tasks)
            articles = [a for batch in results for a in batch]

            # Resolve Google News redirect URLs to real article URLs first,
            # so dedup keys on the real URL and OG scraping hits the article
            gnews = [a for a in articles if "news.google.com/rss/articles" in a.url]
            if gnews:
                await asyncio.gather(*[_decode_gnews_url(session, a) for a in gnews],
                                     return_exceptions=True)
                decoded = sum(1 for a in gnews if "news.google.com" not in a.url)
                if decoded:
                    log.info("Google News decoder resolved %d/%d URLs", decoded, len(gnews))

            # Enrich articles that have no image by scraping OG tags
            no_img = [a for a in articles if not a.image_url and a.url.startswith("http")]
            if no_img:
                og_tasks = [_fetch_og_image(session, a) for a in no_img]
                await asyncio.gather(*og_tasks, return_exceptions=True)
                enriched = sum(1 for a in no_img if a.image_url)
                if enriched:
                    log.info("OG scraper enriched %d/%d articles with images", enriched, len(no_img))

        return articles

    def collect(self) -> List[RawArticle]:
        return asyncio.run(self.collect_async())


async def _fetch_og_image(session: aiohttp.ClientSession, article: "RawArticle") -> None:
    """Fetch the article page and extract og:image / twitter:image into article.image_url."""
    async with _OG_SEMAPHORE:
        try:
            async with session.get(
                article.url, timeout=_OG_TIMEOUT,
                headers=_OG_HEADERS, allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    return
                # Read first 120 KB — heavy pages (Yahoo Finance) bury
                # og:image past 60 KB of inlined scripts
                chunk = await resp.content.read(120_000)
                text = chunk.decode("utf-8", errors="ignore")
        except Exception:
            return

    m = _OG_RE.search(text)
    if m:
        img_url = (m.group(1) or m.group(2) or "").strip()
        if img_url.startswith("http"):
            article.image_url = img_url


def _get_media_image(entry) -> str:
    """Extract the best available image URL from an RSS entry."""
    # media:thumbnail (most common in news RSS)
    thumbs = getattr(entry, "media_thumbnail", None)
    if thumbs and isinstance(thumbs, list) and thumbs[0].get("url"):
        return thumbs[0]["url"]

    # media:content with image type
    content = getattr(entry, "media_content", None)
    if content and isinstance(content, list):
        for m in content:
            url = m.get("url", "")
            if url and (m.get("medium") == "image" or
                        any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"))):
                return url

    # enclosures (podcasts/images)
    for enc in getattr(entry, "enclosures", []):
        if enc.get("type", "").startswith("image/"):
            return enc.get("href") or enc.get("url", "")

    # img tag buried in summary HTML
    summary = entry.get("summary", "") or entry.get("description", "")
    if summary and "<img" in summary:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(summary, "html.parser")
            img = soup.find("img")
            if img and img.get("src", "").startswith("http"):
                return img["src"]
        except Exception:
            pass

    return ""


def _parse_time(entry) -> datetime.datetime:
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            return datetime.datetime(*entry.published_parsed[:6])
        except Exception:
            pass
    return datetime.datetime.utcnow()


# ======================================================================
# from collectors/scraper_collector.py
# ======================================================================

import asyncio
import datetime
import logging
from typing import List

import aiohttp
import feedparser
from bs4 import BeautifulSoup

from config.settings import SCRAPER_TARGETS, SCRAPER_TIMEOUT, SCRAPER_USER_AGENT

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": SCRAPER_USER_AGENT}


def _get_og_image(soup) -> str:
    """Extract Open Graph or Twitter card image from a BeautifulSoup page."""
    for attr, key in [("property", "og:image"), ("name", "twitter:image"),
                      ("property", "og:image:url"), ("itemprop", "image")]:
        tag = soup.find("meta", {attr: key})
        if tag and tag.get("content", "").startswith("http"):
            return tag["content"]
    return ""


class ScraperCollector:
    """HTML scraper for TradingView, FinViz, SEC EDGAR, and FDA."""

    async def _scrape_generic(
        self,
        session: aiohttp.ClientSession,
        name: str,
        cfg: dict,
    ) -> List[RawArticle]:
        # FDA exposes RSS — delegate to feedparser
        if "rss" in cfg:
            try:
                async with session.get(
                    cfg["rss"],
                    headers=HEADERS,
                    timeout=aiohttp.ClientTimeout(total=SCRAPER_TIMEOUT),
                ) as resp:
                    text = await resp.text()
                parsed = feedparser.parse(text)
                return [
                    RawArticle(
                        source=name,
                        title=e.get("title", ""),
                        url=e.get("link", ""),
                        body=e.get("summary", ""),
                        published=datetime.datetime.utcnow(),
                    )
                    for e in parsed.entries[:50]
                ]
            except Exception as exc:
                log.warning("Scraper RSS [%s]: %s", name, exc)
                return []

        url = cfg["url"].format(date=datetime.date.today().isoformat())
        try:
            async with session.get(
                url,
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=SCRAPER_TIMEOUT),
            ) as resp:
                html = await resp.text()
        except Exception as exc:
            log.warning("Scraper fetch [%s]: %s", name, exc)
            return []

        soup = BeautifulSoup(html, "lxml")
        page_og_image = _get_og_image(soup)   # fallback: page-level OG image
        articles: List[RawArticle] = []
        for row in soup.select(cfg["article_sel"])[:50]:
            title_tag = row.select_one(cfg["title_sel"])
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            href  = title_tag.get("href", "")
            if href and not href.startswith("http"):
                from urllib.parse import urlparse, urljoin
                href = urljoin(url, href)
            # row-level image first, then page og, then empty
            row_img = ""
            img_tag = row.find("img")
            if img_tag:
                row_img = img_tag.get("src", "") or img_tag.get("data-src", "")
                if row_img and not row_img.startswith("http"):
                    row_img = ""
            articles.append(RawArticle(
                source=name,
                title=title,
                url=href,
                body="",
                published=datetime.datetime.utcnow(),
                image_url=row_img or page_og_image,
            ))
        log.debug("Scraper [%s] → %d articles", name, len(articles))
        return articles

    async def collect_async(self) -> List[RawArticle]:
        connector = aiohttp.TCPConnector(limit=10, ssl=_SSL)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = [
                self._scrape_generic(session, name, cfg)
                for name, cfg in SCRAPER_TARGETS.items()
            ]
            results = await asyncio.gather(*tasks)
        return [a for batch in results for a in batch]

    def collect(self) -> List[RawArticle]:
        return asyncio.run(self.collect_async())


# ======================================================================
# from collectors/broker_collector.py
# ======================================================================

"""
Free-tier news collectors replacing the broker API feeds.

  FinnhubCollector  — finnhub.io    (60 calls/min free, no credit card)
  NewsAPICollector  — newsapi.org   (100 req/day free developer plan)

Both are commonly used in student/academic finance projects.
"""
import datetime
import logging
from typing import List

import certifi
import requests

from config.settings import FINNHUB_API_KEY, NEWSAPI_KEY

log = logging.getLogger(__name__)


class FinnhubCollector:
    """
    Pulls general market news from Finnhub's free tier.
    Sign up at finnhub.io — the API key is shown right on your dashboard.
    """

    BASE_URL = "https://finnhub.io/api/v1"

    def collect(self) -> List[RawArticle]:
        if not FINNHUB_API_KEY or FINNHUB_API_KEY == "PASTE_YOUR_FINNHUB_KEY_HERE":
            log.info("Finnhub key not set — skipping (add FINNHUB_API_KEY to .env)")
            return []

        articles: List[RawArticle] = []

        # General market news (category: general, forex, crypto, merger)
        for category in ("general", "merger"):
            try:
                resp = requests.get(
                    f"{self.BASE_URL}/news",
                    params={"category": category, "token": FINNHUB_API_KEY},
                    timeout=10,
                    verify=certifi.where(),
                )
                resp.raise_for_status()
                for item in resp.json()[:30]:
                    articles.append(RawArticle(
                        source=f"finnhub_{category}",
                        title=item.get("headline", ""),
                        url=item.get("url", ""),
                        body=item.get("summary", ""),
                        published=datetime.datetime.utcfromtimestamp(
                            item.get("datetime", 0) or 0
                        ),
                    ))
            except Exception as exc:
                log.warning("Finnhub [%s] error: %s", category, exc)

        log.debug("Finnhub → %d articles", len(articles))
        return articles


class NewsAPICollector:
    """
    Pulls financial headlines from NewsAPI's free developer plan.
    Sign up at newsapi.org — you get 100 requests/day free.
    """

    BASE_URL = "https://newsapi.org/v2"

    QUERIES = [
        "stock market",
        "earnings report",
        "federal reserve",
        "IPO merger acquisition",
    ]

    def collect(self) -> List[RawArticle]:
        if not NEWSAPI_KEY or NEWSAPI_KEY == "PASTE_YOUR_NEWSAPI_KEY_HERE":
            log.info("NewsAPI key not set — skipping (add NEWSAPI_KEY to .env)")
            return []

        articles: List[RawArticle] = []
        seen: set[str] = set()

        for query in self.QUERIES:
            try:
                resp = requests.get(
                    f"{self.BASE_URL}/everything",
                    verify=certifi.where(),
                    params={
                        "q":        query,
                        "language": "en",
                        "sortBy":   "publishedAt",
                        "pageSize": 10,
                        "apiKey":   NEWSAPI_KEY,
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                for item in resp.json().get("articles", []):
                    url = item.get("url", "")
                    if url in seen:
                        continue
                    seen.add(url)
                    articles.append(RawArticle(
                        source="newsapi",
                        title=item.get("title", "") or "",
                        url=url,
                        body=item.get("description", "") or "",
                        published=_parse_newsapi_date(item.get("publishedAt")),
                    ))
            except Exception as exc:
                log.warning("NewsAPI [%s] error: %s", query, exc)

        log.debug("NewsAPI → %d articles", len(articles))
        return articles


def _parse_newsapi_date(s: str | None) -> datetime.datetime:
    if not s:
        return datetime.datetime.utcnow()
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return datetime.datetime.utcnow()


class BrokerCollector:
    """Wraps all free-tier API collectors under one interface."""

    def __init__(self):
        self._finnhub  = FinnhubCollector()
        self._newsapi  = NewsAPICollector()

    def collect(self, tickers: list[str] | None = None) -> List[RawArticle]:
        articles = []
        articles.extend(self._finnhub.collect())
        articles.extend(self._newsapi.collect())
        return articles


# ======================================================================
# from collectors/stocktwits_collector.py
# ======================================================================

"""
StockTwits trending-stream collector.

Free public API, no key required — the zero-cost alternative to the Twitter/X
API for "tweets' sentiment". Each trending message becomes a RawArticle whose
body is the message text; cashtags ($AAPL) flow straight into the existing
3-pass ticker extractor.

Rate limit: 200 req/hr unauthenticated. We make exactly 1 request per pipeline
cycle (max 60/hr), so we stay well under it.
"""
import datetime
import json
import logging
import subprocess
from typing import List

from config.settings import STOCKTWITS_TRENDING_URL, STOCKTWITS_ENABLED

log = logging.getLogger(__name__)

# Cloudflare blocks Python's TLS fingerprint (requests/aiohttp get 403)
# but curl's fingerprint passes — so we shell out to curl.
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


class StockTwitsCollector:
    """Fetch trending messages from StockTwits' free public API."""

    def collect(self) -> List[RawArticle]:
        if not STOCKTWITS_ENABLED:
            return []
        try:
            out = subprocess.run(
                ["curl", "-s", "--max-time", "10", "-H", f"User-Agent: {_UA}",
                 STOCKTWITS_TRENDING_URL],
                capture_output=True, text=True, timeout=15,
            )
            messages = json.loads(out.stdout).get("messages", [])
        except Exception as exc:
            log.warning("StockTwits fetch failed: %s", exc)
            return []

        articles: List[RawArticle] = []
        for m in messages:
            body = m.get("body", "")
            if not body:
                continue
            symbols = [s.get("symbol", "") for s in m.get("symbols", [])]
            # Prefix cashtags so the $TICKER extraction pass catches them
            cashtags = " ".join(f"${s}" for s in symbols if s)
            user = (m.get("user") or {}).get("username", "user")

            created = m.get("created_at", "")
            try:
                published = datetime.datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, TypeError):
                published = datetime.datetime.utcnow()

            articles.append(RawArticle(
                source="stocktwits",
                title=f"@{user}: {body[:120]}",
                url=f"https://stocktwits.com/{user}/message/{m.get('id','')}",
                body=f"{cashtags} {body}".strip(),
                published=published,
                image_url=(m.get("entities") or {}).get("chart", {}).get("url", "") or "",
            ))

        log.info("StockTwits → %d trending messages", len(articles))
        return articles


# ======================================================================
# from collectors/edgar_collector.py
# ======================================================================

"""
SEC EDGAR filings collector — Phase A of the long-term fundamentals engine.

Pulls each tracked ticker's recent official filings (10-K annual, 10-Q earnings,
8-K contract/event) from the **free** SEC EDGAR APIs (no key required) and
returns them as RawFiling records for storage + scoring in later phases.

Free endpoints used (SEC asks for a descriptive User-Agent and <=10 req/sec):
  - ticker->CIK map:  https://www.sec.gov/files/company_tickers.json
  - filing history:   https://data.sec.gov/submissions/CIK##########.json
  - the document:     https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}

See docs/FUNDAMENTALS_PLAN.md for the full design.
"""
import datetime
import logging
import time
from dataclasses import dataclass, field
from typing import Iterable

import requests

log = logging.getLogger(__name__)

# SEC requires a real, descriptive User-Agent (their fair-access policy).
_HEADERS = {
    "User-Agent": "SentimentIQ Research (academic project; shp5246@psu.edu)",
    "Accept-Encoding": "gzip, deflate",
}

_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"

# Form type -> our long-term section category
_FORM_KIND = {
    "10-K":  "annual",
    "10-K/A":"annual",
    "10-Q":  "earnings",
    "10-Q/A":"earnings",
    "8-K":   "contract",   # 8-Ks include material agreements / earnings releases
    "8-K/A": "contract",
}

# Rolling window for "long-term, within 1 week" signal
LOOKBACK_DAYS = 7


@dataclass
class RawFiling:
    cik:          str
    ticker:       str
    form_type:    str
    section_kind: str
    filed_at:     datetime.datetime
    accession:    str
    url:          str
    title:        str = ""


class EdgarCollector:
    """Fetches recent 10-K / 10-Q / 8-K filings for a set of tickers."""

    def __init__(self, lookback_days: int = LOOKBACK_DAYS):
        self.lookback_days = lookback_days
        self._cik_map: dict[str, str] | None = None     # TICKER -> 10-digit CIK
        self._map_loaded_at: float = 0.0

    # ── ticker -> CIK map (cached ~24h) ────────────────────────────────────────
    def _load_cik_map(self) -> dict[str, str]:
        if self._cik_map is not None and (time.time() - self._map_loaded_at) < 86400:
            return self._cik_map
        try:
            resp = requests.get(_TICKER_MAP_URL, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("EDGAR ticker map fetch failed: %s", exc)
            return self._cik_map or {}

        # data is {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
        mapping: dict[str, str] = {}
        for row in data.values():
            tic = str(row.get("ticker", "")).upper()
            cik = str(row.get("cik_str", "")).zfill(10)
            if tic:
                mapping[tic] = cik
        self._cik_map = mapping
        self._map_loaded_at = time.time()
        log.info("EDGAR ticker->CIK map loaded (%d companies)", len(mapping))
        return mapping

    # ── recent filings for one ticker ──────────────────────────────────────────
    def _filings_for_ticker(self, ticker: str, cik: str,
                            cutoff: datetime.datetime) -> list[RawFiling]:
        url = _SUBMISSIONS_URL.format(cik10=cik)
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
            if resp.status_code != 200:
                return []
            recent = resp.json().get("filings", {}).get("recent", {})
        except Exception as exc:
            log.debug("EDGAR submissions failed [%s]: %s", ticker, exc)
            return []

        forms      = recent.get("form", [])
        dates      = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primaries  = recent.get("primaryDocument", [])
        titles     = recent.get("primaryDocDescription", [])

        out: list[RawFiling] = []
        cik_int = str(int(cik))   # Archives path uses the un-padded CIK
        for i, form in enumerate(forms):
            if form not in _FORM_KIND:
                continue
            try:
                filed = datetime.datetime.strptime(dates[i], "%Y-%m-%d")
            except (ValueError, IndexError):
                continue
            if filed < cutoff:
                continue
            acc = accessions[i]
            acc_nodash = acc.replace("-", "")
            doc = primaries[i] if i < len(primaries) else ""
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"
                if doc else
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
            )
            out.append(RawFiling(
                cik=cik, ticker=ticker, form_type=form,
                section_kind=_FORM_KIND[form], filed_at=filed,
                accession=acc, url=doc_url,
                title=(titles[i] if i < len(titles) else "") or form,
            ))
        return out

    # ── all-time report history (10-K / 10-Q) for one ticker ──────────────────
    def collect_history(self, ticker: str, max_filings: int = 16) -> list[RawFiling]:
        """
        The company's report history going back years — 10-K annual reports and
        10-Q earnings reports, newest first, capped at max_filings. Same free
        submissions JSON as collect(); no extra API, no key.
        """
        cik_map = self._load_cik_map()
        tic = str(ticker).upper().lstrip("$")
        cik = cik_map.get(tic)
        if not cik:
            return []
        url = _SUBMISSIONS_URL.format(cik10=cik)
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
            if resp.status_code != 200:
                return []
            recent = resp.json().get("filings", {}).get("recent", {})
        except Exception as exc:
            log.debug("EDGAR history failed [%s]: %s", tic, exc)
            return []

        forms      = recent.get("form", [])
        dates      = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primaries  = recent.get("primaryDocument", [])
        titles     = recent.get("primaryDocDescription", [])

        out: list[RawFiling] = []
        cik_int = str(int(cik))
        for i, form in enumerate(forms):
            if form not in ("10-K", "10-Q"):
                continue
            try:
                filed = datetime.datetime.strptime(dates[i], "%Y-%m-%d")
            except (ValueError, IndexError):
                continue
            acc = accessions[i]
            doc = primaries[i] if i < len(primaries) else ""
            if not doc:
                continue
            out.append(RawFiling(
                cik=cik, ticker=tic, form_type=form,
                section_kind=_FORM_KIND[form], filed_at=filed,
                accession=acc,
                url=f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc.replace('-','')}/{doc}",
                title=(titles[i] if i < len(titles) else "") or form,
            ))
            if len(out) >= max_filings:
                break
        return out

    # ── public API ─────────────────────────────────────────────────────────────
    def collect(self, tickers: Iterable[str]) -> list[RawFiling]:
        cik_map = self._load_cik_map()
        if not cik_map:
            return []
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=self.lookback_days)

        results: list[RawFiling] = []
        seen_ciks: set[str] = set()
        for raw_tic in tickers:
            tic = str(raw_tic).upper().lstrip("$")
            cik = cik_map.get(tic)
            if not cik or cik in seen_ciks:
                continue
            seen_ciks.add(cik)
            results.extend(self._filings_for_ticker(tic, cik, cutoff))
            time.sleep(0.12)   # be polite: well under SEC's 10 req/sec limit

        log.info("EDGAR: %d filings in last %dd across %d tickers",
                 len(results), self.lookback_days, len(seen_ciks))
        return results


# ======================================================================
# from collectors/edgar_extractor.py
# ======================================================================

"""
SEC filing section extractor — Phase B of the long-term fundamentals engine.

Filings are huge (a 10-K can be 300+ pages), so we download the primary document
and pull only the high-signal sections per form type:

  10-K (annual)   -> Risk Factors (Item 1A) + MD&A (Item 7)
  10-Q (earnings) -> MD&A (Item 2) + results-of-operations text
  8-K  (contract) -> Item 1.01 material agreement / Item 2.02 results / body

Everything is capped to a few thousand characters before scoring so FinBERT and
Groq stay fast and within the free tier. Pure stdlib + BeautifulSoup (free).
"""
import logging
import re

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_HEADERS_EXT = {
    "User-Agent": "SentimentIQ Research (academic project; shp5246@psu.edu)",
    "Accept-Encoding": "gzip, deflate",
}

MAX_CHARS = 6000   # cap fed to scoring per filing

# Anchor phrases that mark the start of high-signal sections, by section kind.
_ANCHORS = {
    "annual": [
        "risk factors",
        "management's discussion and analysis",
        "management’s discussion and analysis",
    ],
    "earnings": [
        "management's discussion and analysis",
        "management’s discussion and analysis",
        "results of operations",
    ],
    "contract": [
        "item 1.01",
        "entry into a material definitive agreement",
        "item 2.02",
        "results of operations and financial condition",
    ],
}


def _clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "table"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _slice_around_anchors(text: str, kind: str) -> str:
    """Return text starting at the first matching section anchor, capped."""
    low = text.lower()
    best = None
    for anchor in _ANCHORS.get(kind, []):
        idx = low.find(anchor)
        # Skip a hit that's only in the table of contents (very early in doc)
        if idx != -1 and idx > 500:
            best = idx if best is None else min(best, idx)
    if best is not None:
        return text[best : best + MAX_CHARS]
    # Fallback: skip the cover page, take the first substantive chunk
    return text[500 : 500 + MAX_CHARS]


def extract_section(url: str, section_kind: str) -> str:
    """
    Download a filing's primary document and return the high-signal section text
    for scoring. Returns "" on any failure (caller skips gracefully).
    """
    try:
        resp = requests.get(url, headers=_HEADERS_EXT, timeout=20)
        if resp.status_code != 200 or not resp.text:
            return ""
    except Exception as exc:
        log.debug("Filing fetch failed [%s]: %s", url, exc)
        return ""

    text = _clean_text(resp.text)
    if len(text) < 200:
        return ""
    return _slice_around_anchors(text, section_kind)


# ======================================================================
# from collectors/finviz_screener.py
# ======================================================================

"""
Finviz screener CSV loader.

The brief: "A CSV file is extracted via an existing screener on Finviz." Finviz
(Elite) lets you export a screener's results as a CSV — a "Ticker" column plus
whatever fields the screener shows. Drop that export at data/finviz_screener.csv
(or point FINVIZ_SCREENER_CSV at it) and the tracked ticker universe picks it up
automatically. When no CSV is present we fall back to the built-in universe, so
nothing breaks before your Finviz credentials arrive.

Pure stdlib (csv) — free, no Finviz API or login needed to READ an export.
"""
import csv
import logging
import os

log = logging.getLogger(__name__)

# Where the Finviz screener export is expected. Override with the env var.
DEFAULT_CSV = os.getenv("FINVIZ_SCREENER_CSV", "data/finviz_screener.csv")

# Finviz's export header is "Ticker"; accept a couple of common variants.
_TICKER_COLS = {"ticker", "symbol", "tickers"}


def _looks_like_ticker(t: str) -> bool:
    # 1–6 chars, letters/digits with an optional . or - (e.g. BRK.B, RDS-A)
    return bool(t) and len(t) <= 6 and t.replace(".", "").replace("-", "").isalnum()


def load_screener_tickers(path: str | None = None) -> set[str]:
    """
    Return the set of tickers from a Finviz screener CSV export.
    Empty set when the file is missing or unreadable (caller falls back).
    """
    path = path or DEFAULT_CSV
    if not path or not os.path.isfile(path):
        return set()

    out: set[str] = set()
    try:
        # utf-8-sig strips the BOM Finviz sometimes prepends.
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            col = next((name for name in (reader.fieldnames or [])
                        if name and name.strip().lower() in _TICKER_COLS), None)
            if col is None:
                log.warning("Finviz CSV %s has no Ticker/Symbol column (%s)",
                            path, reader.fieldnames)
                return set()
            for row in reader:
                t = (row.get(col) or "").strip().upper()
                if _looks_like_ticker(t):
                    out.add(t)
    except Exception as exc:  # noqa: BLE001 — never break startup on a bad file
        log.warning("Finviz screener CSV load failed (%s): %s", path, exc)
        return set()

    log.info("Finviz screener CSV: loaded %d tickers from %s", len(out), path)
    return out


# ======================================================================
# from collectors/finviz_verify.py
# ======================================================================

"""
Finviz cross-check — verify a prediction against an independent source.

Fetches the free Finviz quote page for a ticker and pulls the data points a
user can check our BUY/SELL/HOLD signal against:
  - analyst recommendation (Finviz "Recom", 1=Strong Buy … 5=Strong Sell)
  - analyst price target (+ implied upside vs current price)
  - actual recent performance (week / month / year / YTD)

We compare our signal to the analyst consensus and report AGREE / MIXED /
DISAGREE, so the prediction can be independently validated. Finviz is one of
the project's sanctioned sources; we fetch politely (real User-Agent, cached
~30 min, low volume) and degrade gracefully if the page is unavailable.
"""
import re
import time

import requests

_UA_FV = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_URL = "https://finviz.com/quote.ashx?t={t}"
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL_FV = 1800   # 30 min

_FIELDS = ["Price", "Recom", "Target Price",
           "Perf Week", "Perf Month", "Perf Year", "Perf YTD"]


def _grab(html: str, label: str) -> str | None:
    i = html.find(f">{label}</a>")
    if i == -1:
        i = html.find(f">{label}<")
    if i == -1:
        return None
    j = html.find("snapshot-td-content", i)
    if j == -1:
        return None
    chunk = html[j:html.find("</div>", j)]
    text = re.sub(r"<[^>]+>", "", chunk).replace('snapshot-td-content"', "").strip(' ">')
    return text or None


def _num(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace("%", "").replace("$", "").replace(",", ""))
    except ValueError:
        return None


def _recom_to_signal(recom: float | None) -> str | None:
    """Finviz Recom 1..5 → analyst consensus as BUY/HOLD/SELL."""
    if recom is None:
        return None
    if recom <= 2.5:
        return "BUY"
    if recom >= 3.5:
        return "SELL"
    return "HOLD"


def _agreement(ours: str, theirs: str | None) -> str:
    if theirs is None:
        return "UNKNOWN"
    if ours == theirs:
        return "AGREE"
    # one side neutral, the other directional
    if "HOLD" in (ours, theirs):
        return "MIXED"
    return "DISAGREE"   # BUY vs SELL


def verify(ticker: str, our_signal: str = "") -> dict | None:
    """
    Cross-check our signal against Finviz for one ticker.
    Returns a dict of the Finviz data + agreement verdict, or None on failure.
    """
    key = ticker.upper().lstrip("$")
    now = time.time()
    if key in _CACHE and (now - _CACHE[key][0]) < _TTL_FV:
        data = dict(_CACHE[key][1])
    else:
        try:
            resp = requests.get(_URL.format(t=key), headers={"User-Agent": _UA_FV},
                                timeout=15, allow_redirects=True)
            if resp.status_code != 200 or "snapshot-td-content" not in resp.text:
                return None
            html = resp.text
        except Exception:
            return None

        raw = {f: _grab(html, f) for f in _FIELDS}
        price  = _num(raw.get("Price"))
        recom  = _num(raw.get("Recom"))
        target = _num(raw.get("Target Price"))
        upside = round((target / price - 1.0) * 100, 1) if (target and price) else None
        data = {
            "ticker":         key,
            "price":          price,
            "recom":          recom,
            "recom_signal":   _recom_to_signal(recom),
            "target":         target,
            "target_upside":  upside,
            "perf_week":      raw.get("Perf Week"),
            "perf_month":     raw.get("Perf Month"),
            "perf_year":      raw.get("Perf Year"),
            "perf_ytd":       raw.get("Perf YTD"),
            "source_url":     f"https://finviz.com/quote.ashx?t={key}",
            "checked_at":     time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now)),
        }
        _CACHE[key] = (now, dict(data))

    if our_signal:
        data["our_signal"] = our_signal.upper()
        data["agreement"] = _agreement(our_signal.upper(), data.get("recom_signal"))
    return data


# ======================================================================
# from collectors/price_history.py
# ======================================================================

"""
All-time price history + reliability stats — free, no API key.

Source: Yahoo Finance via the `yfinance` library (free, handles Yahoo's session
cookie/crumb). We pull full monthly history (period="max") — light and fast,
and plenty of resolution for a long-term reliability view.

We turn that history into the "is this stock reliable?" stats a long-term
investor wants: all-time high/low, distance from the peak, 1-year / 5-year /
all-time returns, max drawdown, annualized volatility, plus a sparkline.

Results are cached in-memory for a few hours (history barely changes intraday
and we stay polite to Yahoo).
"""
import logging
import math
import time

log = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, dict]] = {}     # ticker -> (fetched_at, stats)
_NEG_CACHE: dict[str, float] = {}              # ticker -> when we last got nothing
_TTL = 6 * 3600                                # 6 hours
_NEG_TTL = 1800                                # don't re-hit a dead ticker for 30 min
_SPARK_POINTS = 64


def _downsample(closes: list[float], n: int = _SPARK_POINTS) -> list[float]:
    if len(closes) <= n:
        return [round(c, 2) for c in closes]
    step = len(closes) / n
    return [round(closes[min(int(i * step), len(closes) - 1)], 2) for i in range(n)]


def _fetch_closes(ticker: str) -> tuple[list[str], list[float]]:
    """Return (dates, monthly closes) oldest→newest, or ([],[]) on failure."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="max", interval="1mo", auto_adjust=True)
    except Exception as exc:
        log.debug("yfinance failed [%s]: %s", ticker, exc)
        return [], []
    if hist is None or hist.empty or "Close" not in hist:
        return [], []
    closes, dates = [], []
    for idx, val in hist["Close"].items():
        try:
            c = float(val)
        except (TypeError, ValueError):
            continue
        if c > 0 and not math.isnan(c):
            closes.append(c)
            dates.append(idx.date().isoformat())
    return dates, closes


_CANDLE_CACHE: dict[str, tuple[float, list]] = {}
_CANDLE_TTL = 1800   # 30 min — daily candles barely move intraday


def get_candles(ticker: str, days: int = 520) -> list[dict]:
    """
    Daily OHLC candles for the candlestick chart (free, via yfinance). Pulls ~2
    years so the chart can zoom across timeframes (1M/3M/6M/1Y/2Y) client-side.
    Returns [{d, o, h, l, c}, ...] oldest→newest, or [] on failure.
    """
    key = ticker.upper().lstrip("$")
    now = time.time()
    if key in _CANDLE_CACHE and (now - _CANDLE_CACHE[key][0]) < _CANDLE_TTL:
        return _CANDLE_CACHE[key][1]
    try:
        import yfinance as yf
        hist = yf.Ticker(key).history(period="2y", interval="1d", auto_adjust=True)
    except Exception as exc:
        log.debug("yfinance candles failed [%s]: %s", key, exc)
        return []
    if hist is None or hist.empty:
        return []
    out: list[dict] = []
    for idx, row in hist.iterrows():
        try:
            o, h, l, c = (float(row["Open"]), float(row["High"]),
                          float(row["Low"]), float(row["Close"]))
        except (KeyError, TypeError, ValueError):
            continue
        if any(map(math.isnan, (o, h, l, c))) or c <= 0:
            continue
        out.append({"d": idx.date().isoformat(),
                    "o": round(o, 2), "h": round(h, 2),
                    "l": round(l, 2), "c": round(c, 2)})
    out = out[-days:]
    _CANDLE_CACHE[key] = (now, out)
    return out


def get_price_stats(ticker: str) -> dict | None:
    """All-time reliability stats for a ticker, or None if no data."""
    key = ticker.upper().lstrip("$")
    now = time.time()
    if key in _CACHE and (now - _CACHE[key][0]) < _TTL:
        return _CACHE[key][1]
    if key in _NEG_CACHE and (now - _NEG_CACHE[key]) < _NEG_TTL:
        return None

    dates, closes = _fetch_closes(key)
    if len(closes) < 12:
        _NEG_CACHE[key] = now
        return None

    latest = closes[-1]
    ath    = max(closes)
    atl    = min(closes)

    def ret_over(months: int) -> float | None:
        if len(closes) <= months:
            return None
        past = closes[-months - 1]
        return (latest / past - 1.0) * 100 if past else None

    # max drawdown on monthly closes (largest peak-to-trough drop)
    peak = closes[0]; max_dd = 0.0
    for c in closes:
        peak = max(peak, c)
        if peak:
            max_dd = min(max_dd, (c / peak - 1.0))

    # annualized volatility from monthly returns (×√12)
    rets = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes)) if closes[i - 1]]
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
        vol = math.sqrt(var) * math.sqrt(12) * 100
    else:
        vol = 0.0

    stats = {
        "ticker":       key,
        "latest":       round(latest, 2),
        "latest_date":  dates[-1],
        "first_date":   dates[0],
        "years":        round(len(closes) / 12, 1),
        "ath":          round(ath, 2),
        "atl":          round(atl, 2),
        "pct_from_ath": round((latest / ath - 1.0) * 100, 1) if ath else 0.0,
        "return_1y":    None if ret_over(12) is None else round(ret_over(12), 1),
        "return_5y":    None if ret_over(60) is None else round(ret_over(60), 1),
        "return_all":   round((latest / closes[0] - 1.0) * 100, 1) if closes[0] else 0.0,
        "max_drawdown": round(max_dd * 100, 1),
        "volatility":   round(vol, 1),
        "spark":        _downsample(closes),
    }
    _CACHE[key] = (now, stats)
    return stats



# ────────────────────────────────────────────────────────────────────────────
# FILE: src/pipeline.py
# ────────────────────────────────────────────────────────────────────────────

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



# ────────────────────────────────────────────────────────────────────────────
# FILE: src/sentiment.py
# ────────────────────────────────────────────────────────────────────────────

"""sentiment — merged from 5 modules for a simpler layout."""
from __future__ import annotations

# ======================================================================
# from sentiment/finbert.py
# ======================================================================

import logging
from dataclasses import dataclass
from typing import List

from config.settings import FINBERT_MODEL, SENTIMENT_BATCH, FINBERT_MIN_CONF

log = logging.getLogger(__name__)


@dataclass
class FinBERTResult:
    label:      str    # positive | negative | neutral
    score:      float  # continuous polarity: P(positive) - P(negative), in [-1, 1]
    confidence: float  # raw softmax probability of the winning class


class FinBERTScorer:
    """Lazy-loaded FinBERT inference with batching."""

    _pipeline = None

    @classmethod
    def _load(cls):
        if cls._pipeline is None:
            from transformers import pipeline
            log.info("Loading FinBERT model %s …", FINBERT_MODEL)
            cls._pipeline = pipeline(
                "text-classification",
                model=FINBERT_MODEL,
                tokenizer=FINBERT_MODEL,
                top_k=None,          # return all three class scores
                truncation=True,
                max_length=512,
            )
            log.info("FinBERT loaded.")
        return cls._pipeline

    def score_batch(self, texts: List[str]) -> List[FinBERTResult | None]:
        """Score a batch; returns None for items that fall below confidence threshold."""
        pipe = self._load()
        results: List[FinBERTResult | None] = []
        for i in range(0, len(texts), SENTIMENT_BATCH):
            batch = texts[i : i + SENTIMENT_BATCH]
            try:
                outputs = pipe(batch)
            except Exception as exc:
                log.warning("FinBERT batch error: %s", exc)
                results.extend([None] * len(batch))
                continue
            for item_scores in outputs:
                best = max(item_scores, key=lambda x: x["score"])
                if best["score"] < FINBERT_MIN_CONF:
                    results.append(None)
                    continue
                # Continuous polarity rather than a hard +1/-1/0 label.
                # The label-map version collapsed every neutral article to
                # exactly 0.0, which zeroed its rank_score AND its aggregation
                # weight — silently discarding ~80% of the corpus and letting a
                # small confident minority set each ticker's composite.
                probs = {s["label"].lower(): s["score"] for s in item_scores}
                polarity = probs.get("positive", 0.0) - probs.get("negative", 0.0)
                results.append(FinBERTResult(
                    label=best["label"].lower(),
                    score=polarity,
                    confidence=best["score"],
                ))
        return results

    def score(self, text: str) -> FinBERTResult | None:
        return self.score_batch([text])[0]


# ======================================================================
# from sentiment/vader.py
# ======================================================================

from dataclasses import dataclass

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# VADER ships a general-purpose social-media lexicon that misreads financial
# language: "beat" is scored as violence (-), "surges", "downgrade" and
# "all-time high" are absent entirely. "Earnings beat expectations, stock surges
# to all-time high" scored exactly 0.0 (neutral) before this table was added.
# Valences are on VADER's -4..+4 scale.
_FINANCE_LEXICON: dict[str, float] = {
    # bullish
    "beat": 2.0, "beats": 2.0, "outperform": 2.4, "outperformed": 2.4,
    "surge": 2.8, "surges": 2.8, "surged": 2.8, "soar": 3.0, "soars": 3.0,
    "soared": 3.0, "rally": 2.2, "rallies": 2.2, "rallied": 2.2,
    "jump": 1.8, "jumps": 1.8, "jumped": 1.8, "climb": 1.4, "climbs": 1.4,
    "gain": 1.6, "gains": 1.6, "gained": 1.6, "upgrade": 2.4,
    "upgraded": 2.4, "upgrades": 2.4, "bullish": 2.6, "record": 1.6,
    "profit": 1.8, "profits": 1.8, "profitable": 2.0, "growth": 1.6,
    "dividend": 1.2, "buyback": 1.6, "expansion": 1.4, "guidance": 0.4,
    "raised": 1.6, "raises": 1.6, "topped": 1.8, "tops": 1.8,
    "breakout": 2.0, "momentum": 1.2, "recovery": 1.6, "rebound": 1.8,
    # bearish
    "miss": -2.0, "misses": -2.0, "missed": -2.0, "plunge": -3.0,
    "plunges": -3.0, "plunged": -3.0, "slump": -2.4, "slumps": -2.4,
    "tumble": -2.6, "tumbles": -2.6, "tumbled": -2.6, "crash": -3.2,
    "crashes": -3.2, "crashed": -3.2, "slide": -1.8, "slides": -1.8,
    "sink": -2.2, "sinks": -2.2, "sank": -2.2, "drop": -1.6, "drops": -1.6,
    "dropped": -1.6, "fell": -1.6, "falls": -1.6, "decline": -1.8,
    "declines": -1.8, "downgrade": -2.6, "downgraded": -2.6,
    "downgrades": -2.6, "bearish": -2.6, "loss": -2.0, "losses": -2.0,
    "layoff": -2.4, "layoffs": -2.4, "bankruptcy": -3.4, "default": -2.8,
    "lawsuit": -2.0, "probe": -1.8, "investigation": -1.8, "recall": -2.0,
    "selloff": -2.4, "sell-off": -2.4, "warning": -2.0, "warns": -2.0,
    "cuts": -1.4, "slashed": -2.2, "halted": -2.0, "delisted": -3.0,
    "shortfall": -2.2, "writedown": -2.4, "restructuring": -1.2,
}

_analyzer = SentimentIntensityAnalyzer()
_analyzer.lexicon.update(_FINANCE_LEXICON)


@dataclass
class VADERResult:
    compound: float   # [-1, 1]
    label:    str     # positive / negative / neutral


def score(text: str) -> VADERResult:
    scores  = _analyzer.polarity_scores(text)
    compound = scores["compound"]
    if compound >= 0.05:
        label = "positive"
    elif compound <= -0.05:
        label = "negative"
    else:
        label = "neutral"
    return VADERResult(compound=compound, label=label)


# ======================================================================
# from sentiment/ticker_extractor.py
# ======================================================================

"""
Ticker extractor: finds stock ticker symbols mentioned in article text.

Three pass strategy (ordered by precision):
  1. $TICKER  — explicit dollar-prefix (highest precision)
  2. ALL-CAPS words filtered against TICKER_UNIVERSE (medium precision)
  3. Company name substring match from COMPANY_TO_TICKER (catches "Apple", "Tesla" etc.)
"""
import re
import logging
from typing import Sequence

from config.tickers import (
    TICKER_UNIVERSE, COMPANY_TO_TICKER, AMBIGUOUS_NAMES, _STOPWORDS
)

log = logging.getLogger(__name__)

# Pre-compiled patterns
_DOLLAR_PAT  = re.compile(r'\$([A-Z]{1,5}(?:\.[A-B])?)')  # $AAPL, $BRK.B
_ALLCAPS_PAT = re.compile(r'\b([A-Z]{2,5})\b')            # standalone ALL-CAPS


def _name_pattern(name: str) -> re.Pattern:
    """
    Word-boundary matcher for a company name.

    Plain `name in text` matches inside unrelated words — "meta" inside
    "Rheinmetall", "ups" inside "groups", "intel" inside "intelligence",
    "unity" inside "opportunity". Anchoring with \\b removes those.
    \\b is only useful next to a word character, so it is added conditionally
    (a name like "at&t" ends in one, "s&p 500" does not start with a symbol).
    """
    left  = r'\b' if name[:1].isalnum() else ''
    right = r'\b' if name[-1:].isalnum() else ''
    return re.compile(left + re.escape(name) + right)


# (name, lowercase pattern, ticker, needs_capital), built once at import
_COMPANY_PATTERNS: list[tuple[str, re.Pattern, str, bool]] = [
    (name, _name_pattern(name), ticker, name in AMBIGUOUS_NAMES)
    for name, ticker in COMPANY_TO_TICKER.items()
]


def _company_matches(text: str, text_lower: str):
    """Yield tickers whose company name appears as a whole word in *text*."""
    for name, pat, ticker, needs_capital in _COMPANY_PATTERNS:
        # cheap substring prefilter first, then the authoritative boundary check
        if name not in text_lower:
            continue
        match = pat.search(text_lower)
        if not match:
            continue
        # Ambiguous aliases must be capitalised in the original headline to
        # count — "Snap beat estimates" yes, "a snap decision" no. Compare the
        # matched span back against the original-case text.
        if needs_capital:
            spans = [m.start() for m in pat.finditer(text_lower)]
            if not any(text[i:i + 1].isupper() for i in spans):
                continue
        yield ticker


def extract_tickers(text: str, *, max_tickers: int = 10) -> list[str]:
    """
    Return a sorted list of unique ticker symbols found in *text*.
    Limited to *max_tickers* to avoid noise from very long articles.
    """
    if not text:
        return []

    # Track which pass found each symbol so truncation can keep the most
    # reliable ones: $-prefix (1) > company name (2) > bare ALL-CAPS (3).
    precision: dict[str, int] = {}

    def _add(sym: str, rank: int) -> None:
        if rank < precision.get(sym, 99):
            precision[sym] = rank

    # ── Pass 1: $TICKER ────────────────────────────────────────────────────────
    # No stopword filter here: an explicit cashtag is unambiguous intent, and
    # several real symbols are also common words ($ON, $OPEN, $NOW, $ALL).
    for m in _DOLLAR_PAT.finditer(text):
        sym = m.group(1)
        if sym in TICKER_UNIVERSE:
            _add(sym, 1)

    # ── Pass 2: Company names (whole word only) ───────────────────────────────
    text_lower = text.lower()
    for ticker in _company_matches(text, text_lower):
        _add(ticker, 2)

    # ── Pass 3: ALL-CAPS words ─────────────────────────────────────────────────
    for m in _ALLCAPS_PAT.finditer(text):
        sym = m.group(1)
        if sym in TICKER_UNIVERSE and sym not in _STOPWORDS:
            _add(sym, 3)

    if len(precision) > max_tickers:
        # Drop the least reliable matches first, then break ties alphabetically
        keep = sorted(precision, key=lambda s: (precision[s], s))[:max_tickers]
        return sorted(keep)

    return sorted(precision)


def extract_primary_ticker(text: str) -> str | None:
    """
    Return the single most prominent ticker, or None.
    Priority: $-prefix > company name > all-caps.
    """
    if not text:
        return None

    # $-prefix first (explicit cashtag — no stopword filter, see extract_tickers)
    for m in _DOLLAR_PAT.finditer(text):
        sym = m.group(1)
        if sym in TICKER_UNIVERSE:
            return sym

    # Company name (whole word only)
    text_lower = text.lower()
    for ticker in _company_matches(text, text_lower):
        return ticker

    # All-caps fallback
    for m in _ALLCAPS_PAT.finditer(text):
        sym = m.group(1)
        if sym in TICKER_UNIVERSE and sym not in _STOPWORDS:
            return sym

    return None


def tickers_to_str(tickers: Sequence[str]) -> str:
    """Serialize ticker list to comma-separated string for DB storage."""
    return ",".join(tickers) if tickers else ""


def str_to_tickers(s: str | None) -> list[str]:
    """Deserialize comma-separated ticker string from DB."""
    if not s:
        return []
    return [t.strip() for t in s.split(",") if t.strip()]


# ======================================================================
# from sentiment/llm_judge.py
# ======================================================================

"""
LLM sentiment judge — free-tier Groq nuance layer over financial headlines.

Why this exists
---------------
FinBERT is a strong *free* financial-sentiment model (~89% accuracy) but it
classifies single sentences and misses context that flips a headline's meaning:
"Acme cuts costs" (bullish) vs "Acme cuts guidance" (bearish), "beats but warns",
"misses on revenue, raises buyback", etc. A general LLM reads that nuance.

This module asks Groq's free tier (llama-3.3-70b-versatile — no credit card,
14,400 req/day, fast enough for the real-time board) to score a BATCH of
headlines in one call. It returns a continuous score in [-1, 1] per headline,
in the SAME convention as FinBERT's `score` (P(pos) - P(neg)), so callers can
use it as a drop-in replacement.

It NEVER raises and NEVER blocks the pipeline: with no key, a missing `groq`
package, a rate-limit, or any error, it returns `None` for the affected
headlines and the caller falls back to FinBERT/VADER (fully offline).
"""
import json
import logging

from config.settings import GROQ_API_KEY, NEWS_LLM_MODEL, USE_LLM_SENTIMENT

log = logging.getLogger(__name__)

# Batch size: one Groq call scores this many headlines. ~20 short headlines is
# well under the free-tier per-minute token budget and keeps latency low.
_BATCH = 20

_SYSTEM = (
    "You are a financial-news sentiment analyst. For each numbered headline, "
    "judge its impact on the mentioned company's stock for a LONG-TERM investor. "
    "Read context carefully: 'cuts costs' is bullish, 'cuts guidance' is bearish; "
    "'beats but warns' is roughly neutral. Output ONLY a JSON array, one object "
    "per headline, in the same order, each: "
    '{"i": <number>, "score": <float -1..1>, "label": "positive"|"negative"|"neutral"}. '
    "score = +1 very bullish, 0 neutral, -1 very bearish. No prose, no code fences."
)


def _client():
    """Return a Groq client, or None when unavailable (no key / package)."""
    if not USE_LLM_SENTIMENT:
        return None
    try:
        from groq import Groq
    except ImportError:
        return None
    if not GROQ_API_KEY or GROQ_API_KEY.startswith("PASTE_"):
        return None
    return Groq(api_key=GROQ_API_KEY)


def is_available() -> bool:
    return _client() is not None


# Strip the "groq/" prefix the CrewAI config uses — the raw SDK wants the bare id.
_MODEL = NEWS_LLM_MODEL.split("/", 1)[-1]


def _label_of(score: float) -> str:
    return "positive" if score > 0.05 else "negative" if score < -0.05 else "neutral"


def _score_batch(client, headlines: list[str]) -> list[dict | None]:
    """Score one batch. Returns per-headline dict or None on any failure."""
    numbered = "\n".join(f"{i}. {h}" for i, h in enumerate(headlines))
    try:
        resp = client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": numbered},
            ],
            temperature=0.0,
            max_tokens=1500,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content.strip()
        data = json.loads(raw)
        # The model may wrap the array in a key (json_object mode); unwrap it.
        if isinstance(data, dict):
            arr = next((v for v in data.values() if isinstance(v, list)), None)
        else:
            arr = data
        if not isinstance(arr, list):
            return [None] * len(headlines)

        out: list[dict | None] = [None] * len(headlines)
        for obj in arr:
            if not isinstance(obj, dict):
                continue
            idx = obj.get("i")
            if not isinstance(idx, int) or not (0 <= idx < len(headlines)):
                continue
            try:
                score = max(-1.0, min(1.0, float(obj.get("score"))))
            except (TypeError, ValueError):
                continue
            label = obj.get("label")
            if label not in ("positive", "negative", "neutral"):
                label = _label_of(score)
            out[idx] = {"score": round(score, 4), "label": label}
        return out
    except Exception as exc:  # noqa: BLE001 — never break the pipeline
        log.warning("LLM judge batch failed (%s); falling back to FinBERT", exc)
        return [None] * len(headlines)


def score_headlines(headlines: list[str]) -> list[dict | None]:
    """
    Score each headline in [-1, 1] (FinBERT `score` convention).

    Returns a list the same length as `headlines`; each element is
    {"score": float, "label": str} or None (caller should fall back).
    Fully graceful: returns all-None when Groq is unavailable.
    """
    if not headlines:
        return []
    client = _client()
    if client is None:
        return [None] * len(headlines)

    results: list[dict | None] = []
    for start in range(0, len(headlines), _BATCH):
        batch = headlines[start:start + _BATCH]
        results.extend(_score_batch(client, batch))
    return results


# ======================================================================
# from sentiment/scorer.py
# ======================================================================

"""
SentimentScorer: FinBERT primary, VADER fallback.

Rank formula (v3):
    rank_score = |sentiment_score| × message_density × trust_weight

Time decay is NOT stored in rank_score — it is applied at read time by the
dashboard and the aggregator, so a score keeps decaying as the article ages
instead of being frozen at the moment it was scored. `time_weight` is still
recorded on the row for reference/analysis.

sentiment_score = FinBERT P(positive) - P(negative), continuous in [-1, 1]
                  (VADER compound when FinBERT is below FINBERT_MIN_CONF)

message_density = this source's share of the window, normalised to (0, 1] so
                  rank_score stays bounded and comparable across cycles

trust_weight = 1.0  for Tier-1 sources (Reuters, Dow Jones, SEC, FDA)
             = 0.75 for everything else

time_weight  = exp( -ln(2) / HALFLIFE_HOURS × hours_old )
               → article published NOW gets 1.0
               → 24-h-old article gets 0.5 (with default 24-h half-life)
               (recorded on the row; applied live by readers, not baked into
                rank_score — see above)
"""
import datetime
import logging
import math
from collections import Counter
from typing import List

from config.settings import (
    SOURCE_TRUST, DEFAULT_TRUST_WEIGHT, TIME_DECAY_HALFLIFE_HOURS
)
from src.collectors import RawArticle
from src.storage import SentimentResult
vader_score = score

log = logging.getLogger(__name__)

_LN2 = math.log(2)


def _trust_weight(source: str) -> float:
    return SOURCE_TRUST.get(source, DEFAULT_TRUST_WEIGHT)


def _time_weight(published: datetime.datetime | None) -> float:
    """Exponential decay based on article age."""
    if published is None:
        return 1.0
    now = datetime.datetime.utcnow()
    # Ensure naive comparison
    if published.tzinfo is not None:
        published = published.replace(tzinfo=None)
    hours_old = max(0.0, (now - published).total_seconds() / 3600)
    return math.exp(-_LN2 / TIME_DECAY_HALFLIFE_HOURS * hours_old)


class SentimentScorer:
    """
    FinBERT primary; VADER fallback when FinBERT confidence < FINBERT_MIN_CONF.
    rank_score = |sentiment_score| × density × trust_weight × time_weight
    """

    def __init__(self):
        self._finbert = FinBERTScorer()

    def score_articles(
        self,
        articles: List[RawArticle],
        window_articles: List[RawArticle] | None = None,
    ) -> List[SentimentResult]:
        if not articles:
            return []

        # Message density = share of the window contributed by this article's
        # source, normalised to (0, 1]. The raw count was unusable as a rank
        # factor: a source with 50k rows (StockTwits) gave every one of its
        # posts a 10-50x multiplier over a CNBC story, so rank_score measured
        # how chatty a source is rather than how important the news is. It was
        # also unbounded, making scores incomparable across cycles.
        all_articles = window_articles or articles
        source_counts = Counter(a.source for a in all_articles)
        max_count = max(source_counts.values()) if source_counts else 1

        texts = [f"{a.title}. {a.body}"[:512] for a in articles]
        finbert_results = self._finbert.score_batch(texts)

        # Free-tier Groq LLM judge over the headlines — reads nuance FinBERT
        # misses ("cuts costs" bullish vs "cuts guidance" bearish). Returns
        # None per item when Groq is unavailable, so FinBERT/VADER still drive
        # the fully-offline path. Attribution is title-based, so judge titles.
        llm_results = score_headlines([a.title for a in articles])

        results: List[SentimentResult] = []
        for article, fb, llm, text in zip(articles, finbert_results, llm_results, texts):
            density = source_counts[article.source] / max_count
            tw      = _trust_weight(article.source)
            dw      = _time_weight(article.published)

            # VADER is computed once for every article: it is stored as an
            # independent second opinion, and reused as the fallback below.
            vader_compound = vader_score(text).compound

            # FinBERT fields are always recorded for reference/display when the
            # model was confident, regardless of which engine drives the score.
            if fb is not None:
                finbert_label = fb.label
                finbert_score = fb.score
                finbert_conf  = fb.confidence
            else:
                finbert_label = finbert_score = finbert_conf = None

            # Sentiment priority: LLM judge (nuance) → FinBERT → VADER. Sign is
            # preserved; all three use the same [-1, 1] convention.
            if llm is not None:
                sentiment_score = llm["score"]
                if finbert_label is None:
                    finbert_label = llm["label"]   # give the UI a label to show
            elif fb is not None:
                # fb.score is already continuous polarity in [-1, 1]; it carries
                # the model's confidence in its magnitude, so multiplying by
                # fb.confidence again would double-count it.
                sentiment_score = fb.score
            else:
                # VADER fallback (FinBERT below FINBERT_MIN_CONF or errored)
                sentiment_score = vader_compound
                log.debug("VADER fallback for: %s", article.title[:60])

            # rank_score is the *undecayed* base. Time decay is applied when the
            # score is read (dashboard `_ranked_rows`, aggregator
            # `_live_time_weight`) so it keeps decaying as the article ages.
            # Baking dw in here meant the readers multiplied by decay a second
            # time — an article scored 12h after publication was decayed twice.
            rank_score = abs(sentiment_score) * density * tw

            # Extract tickers from title (fast, no body needed)
            tickers = extract_tickers(article.title)
            tickers_str = tickers_to_str(tickers)

            results.append(SentimentResult(
                article_id      = 0,
                source          = article.source,
                title           = article.title,
                url             = article.url,
                published       = article.published,
                finbert_label   = finbert_label,
                finbert_score   = finbert_score,
                finbert_conf    = finbert_conf,
                vader_compound  = vader_compound,
                sentiment_score = sentiment_score,
                message_density = density,
                trust_weight    = tw,
                time_weight     = dw,
                rank_score      = rank_score,
                tickers         = tickers_str,
                image_url       = getattr(article, "image_url", "") or "",
                scored_at       = datetime.datetime.utcnow(),
            ))
        return results



# ────────────────────────────────────────────────────────────────────────────
# FILE: src/storage.py
# ────────────────────────────────────────────────────────────────────────────

"""storage — merged from 1 modules for a simpler layout."""
from __future__ import annotations

# ======================================================================
# from storage/models.py
# ======================================================================

import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Text, create_engine
)
from sqlalchemy.orm import DeclarativeBase, Session

from config.settings import DATABASE_URL


class Base(DeclarativeBase):
    pass


class Article(Base):
    __tablename__ = "articles"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    source      = Column(String(64), nullable=False, index=True)
    title       = Column(Text, nullable=False)
    url         = Column(String(512), unique=True)
    published   = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    body        = Column(Text)
    fetched_at  = Column(DateTime, default=datetime.datetime.utcnow)


class SentimentResult(Base):
    __tablename__ = "sentiment_results"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    article_id    = Column(Integer, nullable=False, index=True)
    source        = Column(String(64), nullable=False, index=True)
    title         = Column(Text, nullable=False)
    url           = Column(String(512))
    published     = Column(DateTime, index=True)
    # FinBERT: positive / negative / neutral + confidence
    finbert_label = Column(String(16))
    finbert_score = Column(Float)
    finbert_conf  = Column(Float)
    # VADER compound [-1, 1]
    vader_compound = Column(Float)
    # Final blended score: positive=+1, negative=-1, neutral=0, scaled by conf
    sentiment_score = Column(Float, nullable=False)
    # Density score: how many articles from this source in the last window
    message_density = Column(Float, default=1.0)
    # Composite ranking key
    rank_score    = Column(Float, nullable=False)
    scored_at     = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    # Trust / time weighting (new)
    trust_weight  = Column(Float, default=1.0)
    time_weight   = Column(Float, default=1.0)
    # Comma-separated ticker symbols extracted from the article (e.g. "AAPL,MSFT")
    tickers       = Column(String(256), default="")
    # Article thumbnail image URL (from RSS media:thumbnail or OG image)
    image_url     = Column(String(1024), default="")


class TickerSentiment(Base):
    """Per-ticker aggregated sentiment, recomputed each pipeline cycle."""
    __tablename__ = "ticker_sentiment"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    ticker          = Column(String(10), unique=True, nullable=False, index=True)
    # Headline sentiment: reported news only, social chatter excluded
    composite_score = Column(Float, nullable=False)   # weighted-avg sentiment
    article_count   = Column(Integer, default=0)      # news articles only
    # Retail chatter (StockTwits/Reddit), tracked separately so it can be shown
    # without contaminating the news signal
    social_score    = Column(Float, default=0.0)
    social_count    = Column(Integer, default=0)
    bullish_count   = Column(Integer, default=0)
    bearish_count   = Column(Integer, default=0)
    neutral_count   = Column(Integer, default=0)
    avg_trust       = Column(Float, default=1.0)
    top_headline    = Column(Text)
    top_source      = Column(String(64))
    top_url         = Column(String(512))
    last_updated    = Column(DateTime, default=datetime.datetime.utcnow)
    # ── Long-term fundamentals (7-day SEC-filing signal) ──────────────────────
    fundamental_score   = Column(Float, default=0.0)
    fundamental_verdict = Column(String(16), default="")    # Improving|Stable|Deteriorating
    filing_count_7d     = Column(Integer, default=0)
    last_filing_at      = Column(DateTime)
    # ── Continuation signal (news + filings + all-time price record) ──────────
    price_score         = Column(Float, default=0.0)        # long-term price trend, -1..1
    price_return_1y     = Column(Float)                     # % (display)
    price_return_5y     = Column(Float)                     # % (display)
    pct_from_ath        = Column(Float)                     # % below all-time high (display)
    price_volatility    = Column(Float)                     # annualized %, for STABLE/VOLATILE tag
    continuation_score  = Column(Float, default=0.0)        # blended -1..1
    continuation_label  = Column(String(20), default="")    # Strong|Building|Mixed|Weak
    # ── Real market cross-checks now folded INTO the score (not just display) ──
    analyst_recom       = Column(Float)                     # Finviz consensus 1..5 (1=Strong Buy)
    analyst_signal      = Column(Float)                     # mapped to -1..1
    reports_signal      = Column(Float)                     # report component of the score
    momentum_signal     = Column(Float)                     # price-momentum component


class SignalHistory(Base):
    """
    Daily snapshot of each ticker's BUY/SELL/HOLD signal, scored against the
    actual price on TWO long-term horizons — weekly (7 days) and monthly
    (30 days) — which powers the honest "model accuracy" stat. This is a
    long-term investing tool, so both horizons matter (a monthly check is the
    more meaningful one; the weekly check just fills in sooner).
    """
    __tablename__ = "signal_history"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    ticker          = Column(String(10), index=True, nullable=False)
    signal          = Column(String(8), nullable=False)      # BUY | SELL | HOLD
    score           = Column(Float, default=0.0)             # continuation at signal time
    price_at_signal = Column(Float)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    # weekly outcome (filled ~7 days later)
    price_after     = Column(Float)
    pct_change      = Column(Float)
    correct         = Column(Integer)                        # 1 | 0 | NULL = not scored yet
    # monthly outcome (filled ~30 days later)
    price_after_30d = Column(Float)
    pct_change_30d  = Column(Float)
    correct_30d     = Column(Integer)                        # 1 | 0 | NULL = not scored yet
    # the four ingredient signals at prediction time — logged so we can later
    # train a meta-model / re-weight the blend on real outcomes (accuracy work)
    comp_news       = Column(Float)                          # weekly news sentiment
    comp_momentum   = Column(Float)                          # price momentum
    comp_analysts   = Column(Float)                          # analyst consensus
    comp_reports    = Column(Float)                          # SEC-filing trajectory


class Filing(Base):
    """
    A single SEC EDGAR filing (10-K annual, 10-Q earnings, 8-K contract/event)
    for one ticker, scored for the long-term fundamentals signal.
    """
    __tablename__ = "filings"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    cik               = Column(String(12), index=True)
    ticker            = Column(String(10), index=True)
    form_type         = Column(String(12), index=True)       # 10-K | 10-Q | 8-K
    section_kind      = Column(String(16))                   # earnings | annual | contract
    filed_at          = Column(DateTime, index=True)
    accession         = Column(String(24), unique=True, index=True)
    title             = Column(Text, default="")
    url               = Column(String(512))
    # Scoring (filled in later phases; nullable so Phase A can store raw filings)
    finbert_score     = Column(Float)
    fundamental_score = Column(Float)
    llm_summary       = Column(Text, default="")
    llm_verdict       = Column(String(16), default="")       # Improving|Stable|Deteriorating
    fetched_at        = Column(DateTime, default=datetime.datetime.utcnow)


def init_db() -> Session:
    import os
    from sqlalchemy import inspect, text

    os.makedirs("data", exist_ok=True)
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

    # Additive migration: add any new columns that don't exist yet
    inspector = inspect(engine)
    if inspector.has_table("sentiment_results"):
        existing = {c["name"] for c in inspector.get_columns("sentiment_results")}
        new_cols = {
            "trust_weight": "REAL DEFAULT 1.0",
            "time_weight":  "REAL DEFAULT 1.0",
            "tickers":      'TEXT DEFAULT ""',
            "image_url":    'TEXT DEFAULT ""',
        }
        with engine.connect() as conn:
            for col, typedef in new_cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE sentiment_results ADD COLUMN {col} {typedef}"))
            conn.commit()

    # Additive migration for long-term fundamentals columns on ticker_sentiment
    if inspector.has_table("ticker_sentiment"):
        existing = {c["name"] for c in inspector.get_columns("ticker_sentiment")}
        ts_cols = {
            "fundamental_score":   "REAL DEFAULT 0.0",
            "fundamental_verdict": 'TEXT DEFAULT ""',
            "filing_count_7d":     "INTEGER DEFAULT 0",
            "last_filing_at":      "TIMESTAMP",
            "price_score":         "REAL DEFAULT 0.0",
            "price_return_1y":     "REAL",
            "price_return_5y":     "REAL",
            "pct_from_ath":        "REAL",
            "price_volatility":    "REAL",
            "continuation_score":  "REAL DEFAULT 0.0",
            "continuation_label":  'TEXT DEFAULT ""',
            "social_score":        "REAL DEFAULT 0.0",
            "social_count":        "INTEGER DEFAULT 0",
            "analyst_recom":       "REAL",
            "analyst_signal":      "REAL",
            "reports_signal":      "REAL",
            "momentum_signal":     "REAL",
        }
        with engine.connect() as conn:
            for col, typedef in ts_cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE ticker_sentiment ADD COLUMN {col} {typedef}"))
            conn.commit()

    # Additive migration for the monthly (30-day) outcome on signal_history
    if inspector.has_table("signal_history"):
        existing = {c["name"] for c in inspector.get_columns("signal_history")}
        sh_cols = {
            "price_after_30d": "REAL",
            "pct_change_30d":  "REAL",
            "correct_30d":     "INTEGER",
            "comp_news":       "REAL",
            "comp_momentum":   "REAL",
            "comp_analysts":   "REAL",
            "comp_reports":    "REAL",
        }
        with engine.connect() as conn:
            for col, typedef in sh_cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE signal_history ADD COLUMN {col} {typedef}"))
            conn.commit()

    Base.metadata.create_all(engine)
    return Session(engine)



# ────────────────────────────────────────────────────────────────────────────
# FILE: src/utils.py
# ────────────────────────────────────────────────────────────────────────────

"""utils — merged from 1 modules for a simpler layout."""
from __future__ import annotations

# ======================================================================
# from utils/market_hours.py
# ======================================================================

"""
Market-hours helper for US equities (NYSE/NASDAQ).
All times in US/Eastern.
"""
import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Market open / close in ET
_OPEN  = datetime.time(9, 30)
_CLOSE = datetime.time(16, 0)
_PRE   = datetime.time(4, 0)
_POST  = datetime.time(20, 0)


def now_et() -> datetime.datetime:
    return datetime.datetime.now(tz=ET)


def market_status() -> dict:
    """
    Returns a dict with keys:
      status  : "OPEN" | "PRE-MARKET" | "AFTER-HOURS" | "CLOSED"
      label   : short display label
      color   : hex color hint for the UI
      is_open : bool — True only during regular trading hours
    """
    now = now_et()
    weekday = now.weekday()  # 0=Mon … 6=Sun
    t = now.time()

    if weekday >= 5:  # Sat or Sun
        return {"status": "CLOSED", "label": "Market Closed", "color": "#5a7a96", "is_open": False}

    if _OPEN <= t < _CLOSE:
        return {"status": "OPEN", "label": "Market Open", "color": "#00d47e", "is_open": True}

    if _PRE <= t < _OPEN:
        return {"status": "PRE-MARKET", "label": "Pre-Market", "color": "#f5a623", "is_open": False}

    # Everything else on a weekday (after close OR overnight) = after-hours
    # This is the prime window for overnight sentiment analysis
    return {"status": "AFTER-HOURS", "label": "After-Hours", "color": "#9b59b6", "is_open": False}


def pipeline_interval_seconds() -> int:
    """
    Recommended pipeline interval based on current market status:
      After-hours / Pre-market  → 60s  (overnight news is high-value)
      Market open               → 90s  (still active, slightly relaxed)
      Weekend / overnight       → 600s (market fully closed, no rush)
    """
    ms = market_status()
    if ms["status"] in ("AFTER-HOURS", "PRE-MARKET"):
        return 60
    if ms["status"] == "OPEN":
        return 90
    return 600  # CLOSED (weekend / overnight)



# ────────────────────────────────────────────────────────────────────────────
# FILE: src/dashboard/__init__.py
# ────────────────────────────────────────────────────────────────────────────



# ────────────────────────────────────────────────────────────────────────────
# FILE: src/dashboard/app.py
# ────────────────────────────────────────────────────────────────────────────

from __future__ import annotations
import datetime
import json
import logging
import os
import time

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from sqlalchemy import create_engine, desc, func
from sqlalchemy.orm import Session

from config.settings import (
    DATABASE_URL, DASHBOARD_REFRESH, DASHBOARD_TOP_N,
    TICKER_WINDOW_HOURS, MIN_ARTICLES_PER_TICKER, SOURCE_TRUST,
    SIGNAL_BUY_THRESHOLD, SIGNAL_SELL_THRESHOLD,
)
from config.sectors import sector_of as _sector_of
from src.storage import SentimentResult, TickerSentiment, Base
from src.utils import market_status

log = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates")
# Re-read index.html on every request so edits show up on browser refresh
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# Derived from the live trust table so the badge can't drift away from the
# weighting again (it used to name Reuters/Dow Jones, which deliver nothing).
TIER1 = {s for s, w in SOURCE_TRUST.items() if w >= 0.9}

# Domain used to build favicon URL for each source
SOURCE_DOMAINS = {
    "reuters":         "reuters.com",
    "dow_jones":       "wsj.com",
    "cnbc":            "cnbc.com",
    "marketwatch":     "marketwatch.com",
    "pr_newswire":     "prnewswire.com",
    "access_wires":    "accesswire.com",
    "finance_wire":    "financewire.net",
    "global_newswire": "globenewswire.com",
    "yahoo_finance":   "finance.yahoo.com",
    "seeking_alpha":   "seekingalpha.com",
    "fda":             "fda.gov",
    "sec_edgar":       "sec.gov",
    "tradingview":     "tradingview.com",
    "finviz":          "finviz.com",
    "finnhub_general": "finnhub.io",
    "finnhub_merger":  "finnhub.io",
    "newsapi":         "newsapi.org",
    "google_news":     "news.google.com",
    "nasdaq":          "nasdaq.com",
    "investing_com":   "investing.com",
    "benzinga":        "benzinga.com",
    "business_insider":"businessinsider.com",
    "cnn_business":    "cnn.com",
    "fortune":         "fortune.com",
    "reddit_stocks":   "reddit.com",
    "reddit_wsb":      "reddit.com",
    "reddit_investing":"reddit.com",
    "stocktwits":      "stocktwits.com",
}

SOURCE_LABELS = {
    "reuters": "Reuters", "dow_jones": "Dow Jones", "cnbc": "CNBC",
    "marketwatch": "MarketWatch", "pr_newswire": "PR Newswire",
    "access_wires": "ACCESS Wires", "access_wire": "ACCESS Newswire",
    "business_wire": "Business Wire", "finance_wire": "FinanceWire",
    "global_newswire": "GlobeNewswire", "yahoo_finance": "Yahoo Finance",
    "seeking_alpha": "Seeking Alpha", "fda": "FDA", "sec_edgar": "SEC EDGAR",
    "tradingview": "TradingView", "finviz": "FinViz",
    "finnhub_general": "Finnhub", "finnhub_merger": "Finnhub M&A",
    "newsapi": "NewsAPI",
    "google_news": "Google News", "nasdaq": "Nasdaq",
    "investing_com": "Investing.com", "benzinga": "Benzinga",
    "business_insider": "Business Insider", "cnn_business": "CNN Business",
    "fortune": "Fortune", "reddit_stocks": "r/stocks",
    "reddit_wsb": "r/wallstreetbets", "reddit_investing": "r/investing",
    "stocktwits": "StockTwits",
}


# One engine for the process. It used to be built inside _make_session(), so
# every single request created a new engine (and re-ran create_all), leaking a
# connection pool per call. SQLAlchemy engines are thread-safe and pool
# internally — sessions are the per-request part.
_engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
Base.metadata.create_all(_engine)


def _make_session() -> Session:
    return Session(_engine)


def _row_to_dict(r: SentimentResult, rank: int) -> dict:
    method = "FinBERT" if r.finbert_label else "VADER"
    source = r.source or ""
    domain = SOURCE_DOMAINS.get(source, "")
    favicon = f"https://www.google.com/s2/favicons?domain={domain}&sz=64" if domain else ""
    return {
        "rank":            rank,
        "id":              r.id,
        "source":          source,
        "source_label":    SOURCE_LABELS.get(source, source.title()),
        "source_domain":   domain,
        "source_favicon":  favicon,
        "tier":            1 if source in TIER1 else 2,
        "title":           r.title,
        "url":             r.url or "#",
        "image_url":       getattr(r, "image_url", "") or "",
        "sentiment_score": round(r.sentiment_score, 3),
        "message_density": int(r.message_density),
        "rank_score":      round(r.rank_score, 3),
        "trust_weight":    round(getattr(r, "trust_weight", 1.0) or 1.0, 2),
        "time_weight":     round(getattr(r, "time_weight",  1.0) or 1.0, 3),
        "tickers":         getattr(r, "tickers", "") or "",
        "method":          method,
        "label": r.finbert_label or (
            "positive" if r.sentiment_score > 0.05
            else "negative" if r.sentiment_score < -0.05
            else "neutral"
        ),
        "published": r.published.strftime("%H:%M:%S") if r.published else "—",
        "scored_at": r.scored_at.strftime("%H:%M:%S") if r.scored_at else "—",
    }


def _ranked_rows(db: Session) -> list[dict]:
    import datetime
    import math
    from config.settings import TICKER_WINDOW_HOURS, TIME_DECAY_HALFLIFE_HOURS

    # Only the freshness window — rank_score is frozen at scoring time, so
    # without this cutoff stale articles would dominate the board forever.
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=TICKER_WINDOW_HOURS)
    # Pull a big pool so that, after keeping only stock-linked articles, we still
    # fill the board. General market news ("Wall Street drifts…") is dropped.
    rows = (
        db.query(SentimentResult)
        .filter(SentimentResult.scored_at >= cutoff)
        .filter(SentimentResult.tickers.isnot(None), SentimentResult.tickers != "")
        .order_by(desc(SentimentResult.rank_score))
        .limit(DASHBOARD_TOP_N * 20)
        .all()
    )

    # Keep only articles about a stock actually listed on the site
    tracked = {t[0] for t in db.query(TickerSentiment.ticker).all()}
    if tracked:
        def _has_tracked(r: SentimentResult) -> bool:
            return any(t.strip() in tracked for t in (r.tickers or "").split(",") if t.strip())
        linked = [r for r in rows if _has_tracked(r)]
        rows = linked or rows   # fall back to any-ticker rows if none match

    # Re-apply time decay live so newer articles outrank equally-scored old ones
    now = datetime.datetime.utcnow()
    k = math.log(2) / TIME_DECAY_HALFLIFE_HOURS

    def _live_rank(r: SentimentResult) -> float:
        ref = r.published or r.scored_at or now
        hours_old = max((now - ref).total_seconds() / 3600, 0)
        return (r.rank_score or 0) * math.exp(-k * hours_old)

    rows = sorted(rows, key=_live_rank, reverse=True)[:DASHBOARD_TOP_N]
    return [_row_to_dict(r, i + 1) for i, r in enumerate(rows)]


def _get_stats(db: Session) -> dict:
    total = db.query(func.count(SentimentResult.id)).scalar() or 0
    pos   = db.query(func.count(SentimentResult.id))\
              .filter(SentimentResult.sentiment_score >  0.05).scalar() or 0
    neg   = db.query(func.count(SentimentResult.id))\
              .filter(SentimentResult.sentiment_score < -0.05).scalar() or 0
    neu   = total - pos - neg
    sources = db.query(SentimentResult.source).distinct().count()
    return {"total": total, "bullish": pos, "bearish": neg,
            "neutral": neu, "sources": sources}


def _ranked_tickers(db: Session) -> list[dict]:
    """
    Full tracked-stock board. Stocks with fresh news (>= MIN_ARTICLES_PER_TICKER
    in the last TICKER_WINDOW_HOURS) are ranked on top by
    |composite_score| × article_count × avg_trust, carrying their live sentiment.

    Stocks WITHOUT fresh news are still shown — as neutral "No recent news" —
    so the board never collapses to a handful when the market is closed
    (overnight / weekends, when few articles publish). We deliberately do NOT
    surface a quiet stock's last-known score: an old score served as current is
    misleading (the reason the aggregator stopped doing exactly that), so quiet
    rows read neutral until fresh coverage arrives.
    """
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=TICKER_WINDOW_HOURS)
    all_rows = db.query(TickerSentiment).all()
    if not all_rows:
        return []

    def _is_active(t: TickerSentiment) -> bool:
        return ((t.article_count or 0) >= MIN_ARTICLES_PER_TICKER
                and t.last_updated is not None
                and t.last_updated >= cutoff)

    def _ticker_rank(t: TickerSentiment) -> float:
        return abs(t.composite_score or 0.0) * (t.article_count or 0) * (t.avg_trust or 0.0)

    active = sorted((t for t in all_rows if _is_active(t)), key=_ticker_rank, reverse=True)
    quiet  = sorted((t for t in all_rows if not _is_active(t)), key=lambda t: t.ticker)
    rows_sorted = list(active) + list(quiet)

    out = []
    for i, t in enumerate(rows_sorted, 1):
        fresh = _is_active(t)
        score = (t.composite_score or 0.0) if fresh else 0.0
        if not fresh:
            label = "neutral"
        elif score > 0.05:
            label = "positive"
        elif score < -0.05:
            label = "negative"
        else:
            label = "neutral"
        out.append({
            "rank":           i,
            "ticker":         t.ticker,
            "composite_score": round(score, 4),
            "label":          label,
            # Lets the UI mute quiet stocks and label them "No recent news"
            "has_recent_news": fresh,
            "article_count":  t.article_count if fresh else 0,
            "bullish_count":  t.bullish_count if fresh else 0,
            "bearish_count":  t.bearish_count if fresh else 0,
            "neutral_count":  t.neutral_count if fresh else 0,
            "avg_trust":      round(t.avg_trust or 0.0, 2),
            # Retail chatter, reported alongside but never mixed into the
            # news composite above
            "social_score":   round(t.social_score or 0.0, 4) if fresh else 0.0,
            "social_count":   (t.social_count or 0) if fresh else 0,
            "top_headline":   (t.top_headline or "—") if fresh else "No recent news",
            "top_source":     SOURCE_LABELS.get(t.top_source, (t.top_source or "").title()) if fresh else "—",
            "top_url":        (t.top_url or "#") if fresh else "#",
            # Date included: a time-only stamp made a three-week-old row look
            # like it had refreshed minutes ago.
            "last_updated":   t.last_updated.strftime("%Y-%m-%d %H:%M") if t.last_updated else "—",
            "sector":         _sector_of(t.ticker),
        })
    return out


def _fundamental_rows(db: Session) -> list[dict]:
    """
    Long-term fundamentals screener: tickers ranked by their 7-day SEC-filing
    signal, each with the recent filings (form, verdict, Groq summary, link).
    """
    from src.storage import Filing
    rows = (db.query(TickerSentiment)
            .filter(TickerSentiment.filing_count_7d > 0)
            .all())
    if not rows:
        return []

    # Rank by the blended Continuation score (news + filings + price record)
    rows_sorted = sorted(rows, key=lambda t: (t.continuation_score or 0.0), reverse=True)
    out = []
    for i, t in enumerate(rows_sorted, 1):
        fs = (db.query(Filing)
              .filter(Filing.ticker == t.ticker)
              .order_by(Filing.filed_at.desc())
              .limit(6).all())
        filings = [{
            "form":      f.form_type,
            "kind":      f.section_kind,
            "filed":     f.filed_at.strftime("%b %d") if f.filed_at else "—",
            "verdict":   f.llm_verdict or "Stable",
            "score":     round(f.fundamental_score or 0.0, 3),
            "summary":   f.llm_summary or "",
            "url":       f.url or "#",
        } for f in fs]
        # Bull/bear strength from this week's news mix
        n_arts = max(1, (t.bullish_count or 0) + (t.bearish_count or 0) + (t.neutral_count or 0))
        bull_pct = round((t.bullish_count or 0) / n_arts * 100)
        bear_pct = round((t.bearish_count or 0) / n_arts * 100)

        # Confidence: how much evidence backs this prediction (data volume + agreement)
        n_filings = db.query(Filing).filter(Filing.ticker == t.ticker).count()
        evidence = min(40, n_filings * 3) + min(25, n_arts * 2)
        agreement = abs(t.continuation_score or 0.0) * 30
        confidence = min(95, round(35 + evidence * 0.6 + agreement))

        pred = t.continuation_score or 0.0
        signal = ("BUY" if pred > SIGNAL_BUY_THRESHOLD
                  else "SELL" if pred < -SIGNAL_SELL_THRESHOLD else "HOLD")

        # Event types driving this signal (from the filing kinds present)
        kinds = {f.section_kind for f in fs}
        event_types = sorted(k for k in kinds if k)

        # Volatility tag from the all-time price record
        vol = t.price_volatility
        vol_tag = ("VOLATILE" if vol and vol >= 45 else
                   "STABLE" if vol and vol < 25 else
                   "MODERATE" if vol else "")

        out.append({
            "rank":     i,
            "ticker":   t.ticker,
            "score":    round(t.fundamental_score or 0.0, 3),
            "verdict":  t.fundamental_verdict or "Stable",
            "count":    t.filing_count_7d,
            "filing_total": n_filings,
            "last":     t.last_filing_at.strftime("%b %d") if t.last_filing_at else "—",
            # ── Prediction signal ──────────────────────────────────────────────
            "continuation":       round(pred, 3),
            "continuation_label": t.continuation_label or "Mixed",
            "signal":             signal,
            "confidence":         confidence,
            "uncertainty":        100 - confidence,
            "sector":             _sector_of(t.ticker),
            "event_types":        event_types,
            "vol_tag":            vol_tag,
            "volatility":         vol,
            "bull_pct":           bull_pct,
            "bear_pct":           bear_pct,
            "article_count":      t.article_count or 0,
            "headline":           t.top_headline or "",
            "headline_source":    SOURCE_LABELS.get(t.top_source or "", t.top_source or ""),
            "headline_url":       t.top_url or "",
            "news_score":         round(t.composite_score or 0.0, 3),
            "price_score":        round(t.price_score or 0.0, 3),
            # ── The four components that now make up the rating ───────────────
            "comp_news":          None if t.composite_score is None else round(t.composite_score, 3),
            "comp_reports":       None if t.reports_signal is None else round(t.reports_signal, 3),
            "comp_momentum":      None if t.momentum_signal is None else round(t.momentum_signal, 3),
            "comp_analysts":      None if t.analyst_signal is None else round(t.analyst_signal, 3),
            "analyst_recom":      t.analyst_recom,
            "return_1y":          t.price_return_1y,
            "return_5y":          t.price_return_5y,
            "pct_from_ath":       t.pct_from_ath,
            "filings":            filings,
        })
    return out


def _signal_accuracy(db: Session) -> dict:
    """
    Honest self-score on two long-term horizons: weekly (7-day check) and
    monthly (30-day check). Each is the % of graded signals that proved right.
    The monthly window is wider (rows are ≥30d old, so a 30-day lookback would
    exclude them). Returns {"weekly": {...}, "monthly": {...}} plus a top-level
    "pct"/"n" mirroring weekly for backward compatibility.
    """
    from src.storage import SignalHistory
    import datetime as _dt
    now = _dt.datetime.utcnow()

    def _pct(field, since_days):
        cutoff = now - _dt.timedelta(days=since_days)
        rows = (db.query(SignalHistory)
                .filter(field.isnot(None), SignalHistory.created_at >= cutoff)
                .all())
        if not rows:
            return {"pct": None, "n": 0}
        correct = sum(1 for r in rows if getattr(r, field.key))
        return {"pct": round(correct / len(rows) * 100), "n": len(rows)}

    weekly  = _pct(SignalHistory.correct, 45)
    monthly = _pct(SignalHistory.correct_30d, 120)
    return {"weekly": weekly, "monthly": monthly, **weekly}


@app.route("/api/fundamentals")
def api_fundamentals():
    db = _make_session()
    try:
        return jsonify({
            "data": _fundamental_rows(db),
            "accuracy": _signal_accuracy(db),
        })
    finally:
        db.close()


@app.route("/api/ticker-events/<ticker>")
def api_ticker_events(ticker):
    """Chronological timeline: SEC filings + this week's news for one ticker."""
    from src.storage import Filing
    sym = ticker.upper().lstrip("$")
    db = _make_session()
    try:
        events = []
        for f in (db.query(Filing).filter(Filing.ticker == sym)
                  .order_by(desc(Filing.filed_at)).limit(20).all()):
            events.append({
                "kind":    "filing",
                "date":    f.filed_at.strftime("%Y-%m-%d") if f.filed_at else "",
                "title":   f"{f.form_type} — {f.section_kind or 'filing'}",
                "detail":  f.llm_summary or "",
                "score":   round(f.fundamental_score or 0.0, 3),
                "url":     f.url or "",
            })
        week_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        arts = (db.query(SentimentResult)
                .filter(SentimentResult.tickers.like(f"%{sym}%"),
                        SentimentResult.published >= week_ago)
                .order_by(desc(SentimentResult.rank_score))
                .limit(15).all())
        for a in arts:
            events.append({
                "kind":    "news",
                "date":    a.published.strftime("%Y-%m-%d") if a.published else "",
                "title":   a.title,
                "detail":  SOURCE_LABELS.get(a.source, a.source),
                "score":   round(a.sentiment_score or 0.0, 3),
                "url":     a.url or "",
            })
        events.sort(key=lambda e: e["date"], reverse=True)
        return jsonify({"ticker": sym, "events": events})
    finally:
        db.close()


def _get_narrative() -> str:
    try:
        with open("data/narrative.txt") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "Groq AI narrative will appear here after the first pipeline cycle."


@app.route("/")
def index():
    db      = _make_session()
    rows    = _ranked_rows(db)
    stats   = _get_stats(db)
    tickers = _ranked_tickers(db)
    db.close()
    return render_template(
        "index.html",
        rows=rows, stats=stats, tickers=tickers,
        narrative=_get_narrative(),
        refresh=DASHBOARD_REFRESH,
        top_n=DASHBOARD_TOP_N,
    )


@app.route("/api/ranked")
def api_ranked():
    db   = _make_session()
    rows = _ranked_rows(db)
    db.close()
    return {"data": rows, "count": len(rows)}


@app.route("/api/tickers")
def api_tickers():
    db      = _make_session()
    tickers = _ranked_tickers(db)
    db.close()
    return {"data": tickers, "count": len(tickers)}


@app.route("/api/stats")
def api_stats():
    db    = _make_session()
    stats = _get_stats(db)
    db.close()
    stats["narrative"] = _get_narrative()
    return stats


@app.route("/api/market-status")
def api_market_status():
    return jsonify(market_status())


@app.route("/api/price/<ticker>")
def api_price(ticker):
    """All-time price history + reliability stats (free, via yfinance)."""
    from src.collectors import get_price_stats
    stats = get_price_stats(ticker)
    if not stats:
        return jsonify({"error": "no price history", "ticker": ticker.upper()}), 404
    return jsonify(stats)


@app.route("/api/candles/<ticker>")
def api_candles(ticker):
    """Recent daily OHLC candles for the candlestick chart (free, via yfinance)."""
    from src.collectors import get_candles
    candles = get_candles(ticker)
    if not candles:
        return jsonify({"error": "no candles", "ticker": ticker.upper()}), 404
    return jsonify({"ticker": ticker.upper(), "candles": candles})


@app.route("/api/verify/<ticker>")
def api_verify(ticker):
    """Independently cross-check our signal against Finviz (analysts + performance)."""
    from src.collectors import verify
    our = request.args.get("signal", "")
    data = verify(ticker, our)
    if not data:
        return jsonify({"error": "finviz unavailable", "ticker": ticker.upper()}), 404
    return jsonify(data)


def _chat_context(db: Session) -> str:
    """Compact live snapshot to ground the chatbot in real dashboard data."""
    stats   = _get_stats(db)
    tickers = _ranked_tickers(db)[:6]
    rows    = _ranked_rows(db)[:5]
    ms      = market_status()

    mood = "neutral"
    if stats["total"]:
        if stats["bullish"] > stats["bearish"] * 1.2:
            mood = "mostly bullish (optimistic)"
        elif stats["bearish"] > stats["bullish"] * 1.2:
            mood = "mostly bearish (pessimistic)"

    lines = [
        f"Market status: {ms['label']}.",
        f"Overall mood: {mood}.",
        f"Articles analyzed: {stats['total']} "
        f"({stats['bullish']} bullish, {stats['bearish']} bearish, {stats['neutral']} neutral) "
        f"from {stats['sources']} sources.",
    ]
    if tickers:
        lines.append("Top tickers by sentiment right now: " + ", ".join(
            f"{t['ticker']} ({t['label']}, score {t['composite_score']})" for t in tickers
        ))
    if rows:
        lines.append("Top headlines right now:")
        for r in rows:
            lines.append(f"  - [{r['label']}] {r['title']} ({r['source_label']})")
    return "\n".join(lines)


def _stock_context(db: Session, ticker: str) -> str:
    """
    Everything the dashboard knows about ONE stock, as plain text, so the
    chatbot can answer specific questions ("why is JPM a buy?", "is NVDA
    risky?") from the same numbers the user sees on screen.
    """
    from src.storage import Filing
    sym = ticker.upper().lstrip("$")
    ts = db.query(TickerSentiment).filter(TickerSentiment.ticker == sym).first()
    if ts is None:
        return ""

    pred = ts.continuation_score or 0.0
    signal = ("BUY" if pred > SIGNAL_BUY_THRESHOLD
              else "SELL" if pred < -SIGNAL_SELL_THRESHOLD else "HOLD")
    n_arts = max(1, (ts.bullish_count or 0) + (ts.bearish_count or 0) + (ts.neutral_count or 0))
    n_filings = db.query(Filing).filter(Filing.ticker == sym).count()
    evidence = min(40, n_filings * 3) + min(25, n_arts * 2)
    confidence = min(95, round(35 + evidence * 0.6 + abs(pred) * 30))

    def _fmt(v):
        return "n/a" if v is None else f"{v:+.3f}"

    analyst_txt = "n/a"
    if ts.analyst_signal is not None:
        analyst_txt = f"{ts.analyst_signal:+.3f} (Finviz analyst consensus {ts.analyst_recom}/5, 1=Strong Buy … 5=Strong Sell)"

    L = [
        f"=== STOCK DATA FOR {sym} (this is what the dashboard shows) ===",
        f"Our rating: {signal} (overall score {pred:+.3f}, "
        f"{confidence}% confidence, {100 - confidence}% uncertainty).",
        f"Sector: {_sector_of(sym)}.",
        "The rating blends FOUR signals (each -1 bad to +1 good), weighted:",
        f"  1. News this week (30%): {_fmt(ts.composite_score)} "
        f"from {ts.article_count or 0} articles "
        f"({ts.bullish_count or 0} bullish, {ts.bearish_count or 0} bearish, "
        f"{ts.neutral_count or 0} neutral).",
        f"  2. Price momentum (30%): {_fmt(ts.momentum_signal)} "
        f"(1-year {ts.price_return_1y}%, 5-year {ts.price_return_5y}%).",
        f"  3. Wall-Street analyst view (25%): {analyst_txt}.",
        f"  4. SEC filings (15%): {_fmt(ts.reports_signal)} "
        f"— latest filings read as \"{ts.fundamental_verdict or 'Stable'}\".",
        "IMPORTANT: our BUY/SELL/HOLD is this blend of sentiment + market data, "
        "NOT a guaranteed price forecast. A stock can be up a lot yet get HOLD/SELL "
        "if its recent news is negative, and vice-versa. Explain it that way.",
    ]
    if ts.price_return_1y is not None or ts.price_return_5y is not None:
        L.append(
            f"Price record: 1-year {ts.price_return_1y}%, 5-year {ts.price_return_5y}%, "
            f"currently {ts.pct_from_ath}% from its all-time high, "
            f"volatility {ts.price_volatility}% a year "
            f"({'high — it swings a lot' if (ts.price_volatility or 0) >= 45 else 'relatively steady' if (ts.price_volatility or 0) < 25 else 'moderate'})."
        )
    if ts.top_headline:
        L.append(f"Top headline driving it: \"{ts.top_headline}\" "
                 f"({SOURCE_LABELS.get(ts.top_source or '', ts.top_source or '')}).")

    fs = (db.query(Filing).filter(Filing.ticker == sym)
          .order_by(desc(Filing.filed_at)).limit(3).all())
    if fs:
        L.append("Recent SEC filings we scored:")
        for f in fs:
            when = f.filed_at.strftime("%b %d, %Y") if f.filed_at else "—"
            L.append(f"  - {f.form_type} filed {when} (score {f.fundamental_score or 0:+.2f})"
                     + (f": {f.llm_summary[:220]}" if f.llm_summary else ""))

    week_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    arts = (db.query(SentimentResult)
            .filter(SentimentResult.tickers.like(f"%{sym}%"),
                    SentimentResult.published >= week_ago)
            .order_by(desc(SentimentResult.rank_score)).limit(5).all())
    if arts:
        L.append(f"Recent news about {sym}:")
        for a in arts:
            lbl = ("bullish" if (a.sentiment_score or 0) > 0.05
                   else "bearish" if (a.sentiment_score or 0) < -0.05 else "neutral")
            L.append(f"  - [{lbl}] {a.title} ({SOURCE_LABELS.get(a.source, a.source)})")
    return "\n".join(L)


def _tickers_in_question(text: str, db: Session) -> list[str]:
    """Which tracked stocks is the user asking about?"""
    from src.sentiment import extract_tickers
    found = extract_tickers(text or "", max_tickers=3)
    tracked = {t[0] for t in db.query(TickerSentiment.ticker).all()}
    return [t for t in found if t in tracked]


@app.route("/api/chat", methods=["POST"])
def api_chat():
    payload  = request.get_json(silent=True) or {}
    messages = payload.get("messages", [])
    mode     = payload.get("mode", "tutor")
    if mode not in ("tutor", "support"):
        mode = "tutor"
    if not isinstance(messages, list) or not messages:
        return jsonify({"reply": "Ask me anything about the dashboard or investing basics!"})

    # Sanitize to the fields the model needs
    clean = [
        {"role": m.get("role", "user"), "content": str(m.get("content", ""))[:2000]}
        for m in messages if m.get("content")
    ]

    # The stock the user currently has open, sent by the dashboard
    viewing = str(payload.get("ticker", "") or "").upper().lstrip("$")[:10]

    db = _make_session()
    try:
        context = _chat_context(db)

        # Which stock(s) is this question about? Look at the latest user turn,
        # and fall back to whatever they're viewing ("is this a good buy?").
        last_user = next((m["content"] for m in reversed(clean)
                          if m.get("role") == "user"), "")
        syms = _tickers_in_question(last_user, db)
        if not syms and viewing:
            syms = [viewing]
        # Also honour a stock named earlier in the conversation
        if not syms:
            for m in reversed(clean[:-1]):
                syms = _tickers_in_question(m.get("content", ""), db)
                if syms:
                    break

        blocks = [b for b in (_stock_context(db, s) for s in syms[:2]) if b]
        if blocks:
            context += ("\n\n" + "\n\n".join(blocks) +
                        "\n\nAnswer the user's question using these exact numbers. "
                        "Explain what they mean in plain English for a beginner. "
                        "You may explain what the data shows and why the rating is "
                        "what it is, but never tell them to buy or sell — remind them "
                        "it's for learning, not financial advice.")
    finally:
        db.close()

    reply = chat(clean, context=context, mode=mode)
    return jsonify({"reply": reply})


@app.route("/stream")
def stream():
    def event_gen():
        db = _make_session()
        # try/finally: without it the session (and its pooled connection) leaked
        # every time a browser tab closed and the generator was torn down.
        try:
            while True:
                try:
                    rows      = _ranked_rows(db)
                    tickers   = _ranked_tickers(db)
                    stats     = _get_stats(db)
                    narrative = _get_narrative()
                    db.expire_all()
                    payload = json.dumps({
                        "data": rows,
                        "tickers": tickers,
                        "stats": stats,
                        "narrative": narrative,
                        "market": market_status(),
                    })
                    yield f"data: {payload}\n\n"
                except Exception as exc:
                    log.warning("SSE error: %s", exc)
                    db.rollback()
                    yield f"data: {json.dumps({'error': str(exc)})}\n\n"
                time.sleep(DASHBOARD_REFRESH)
        finally:
            db.close()

    return Response(
        stream_with_context(event_gen()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.getenv("DASHBOARD_PORT", 5001))
    app.run(debug=False, port=port, threaded=True)

# ======================================================================
# from dashboard/chatbot.py (merged in)
# ======================================================================

"""
Beginner-friendly chatbot for the SentimentIQ dashboard.

Uses Groq's free tier (llama-3.1-8b-instant) — the same free API key that
powers the AI Narrative. No extra cost, no extra key.

The bot is grounded with a live snapshot of the dashboard (market mood, top
tickers, top headlines) so it can answer questions like "why is NVDA bullish
today?" using the actual data on screen, and it can explain every concept in
the app for people who are new to investing.
"""
import logging

from config.settings import GROQ_API_KEY, CHAT_LLM_MODEL

log = logging.getLogger(__name__)

# Strip the "groq/" prefix CrewAI uses — the raw SDK wants the bare model id
_MODEL = CHAT_LLM_MODEL.split("/", 1)[-1]   # "llama-3.3-70b-versatile"

_SYSTEM_PROMPT = """You are Sentiment Buddy, a friendly assistant built into the \
SentimentIQ dashboard — a real-time financial-news sentiment tool. You help \
COMPLETE BEGINNERS understand investing and how this dashboard works.

Your style:
- Talk like a friendly person texting, not an essay writer. KEEP IT SHORT: 1 to 3 \
sentences by default. Only write more when the user explicitly asks you to \
"explain", "go deeper", or "compare".
- Do NOT use em-dashes (the long dash). Do NOT write dash-bullet or numbered lists \
unless the user asks for a list. Answer in plain, warm sentences.
- Assume the person may know nothing about stocks; define jargon in a few words \
when it comes up, and use simple analogies.
- Be interactive: answer, then when it helps, ask one short follow-up question to \
keep the chat going. Vary how you open so you never sound like a template, and \
don't repeat yourself.
- Never tell anyone to buy or sell a specific stock, and never give personalized \
financial advice. You CAN explain concepts, what the data shows, and why a rating \
came out the way it did. Mention that this is for learning and you're not a \
licensed advisor ONCE per conversation — the first time it's relevant — not in \
every message. Repeating the disclaimer every turn makes you useless.

How THIS dashboard works (explain when asked):
- It pulls financial news every ~60 seconds from free sources: CNBC, MarketWatch, \
PR Newswire, GlobeNewswire, Seeking Alpha, Investing.com, Business Insider, \
Fortune, Google News, the FDA press-release feed, SEC EDGAR filings, Reddit \
(r/stocks, r/wallstreetbets, r/investing) and StockTwits. Be honest about this \
list — do not claim sources that aren't on it.
- Most of the raw volume is StockTwits (social chatter), which is why social \
posts are weighted lower than news outlets. Say so if someone asks how reliable \
the mix is.
- SENTIMENT SCORE: how positive or negative a headline sounds, from -1 (very \
bearish/negative) to +1 (very bullish/positive). It's measured by an AI model \
called FinBERT (trained on financial text) with a backup called VADER.
- BULLISH = optimistic/price-might-rise mood. BEARISH = pessimistic/price-might-fall mood. \
NEUTRAL = no strong feeling.
- MESSAGE DENSITY: how much people are talking about something — more coverage = higher density.
- TRUST WEIGHT: official/primary sources (FDA, company press releases) count \
most (0.9-1.0x), mainstream outlets somewhat less, and social posts (StockTwits, \
Reddit) least (0.3-0.4x) because anyone can post them.
- TIME DECAY: newer news matters more; old news fades in importance.
- RANK SCORE: combines all of the above (|sentiment| x density x trust x freshness) \
to sort which news matters most right now.
- TICKER: the short symbol for a company's stock, like AAPL for Apple or NVDA for Nvidia.
- The Ticker Heatmap ranks stocks by the combined sentiment of all their news.

If a question is off-topic from finance/the dashboard, gently steer back, but it's \
fine to answer simple general questions too.
"""

_SUPPORT_PROMPT = """You are the SentimentIQ Customer Care assistant — a friendly, \
professional support agent for SentimentIQ, a real-time financial-news sentiment \
dashboard. Your job is to HELP USERS with the product: how to use features, \
troubleshooting issues, and answering "how do I…" questions about the app.

Your style:
- Warm, professional, and concise — like a great customer-support agent.
- Acknowledge the user's issue first, then give clear, step-by-step help.
- If something looks like a bug, offer a workaround and suggest they note when it \
happened so it can be looked into.

What you can help with:
- How to read and use the dashboard: sentiment scores, the ticker heatmap, rank \
score, filters, search, and the live news feed.
- Troubleshooting: data not updating (the dashboard refreshes about every 60 \
seconds; a browser refresh can help), the AI features needing a free Groq API key, \
the market-status badge, or the page not loading.
- General "how does this work / how do I do X" questions about the product.

What you must NOT do:
- Do not give personalized financial or investment advice, or tell anyone to buy or \
sell a stock. If asked, politely explain that you're product support — not a \
financial advisor — and that the dashboard is for learning only.

If a user really wants to learn investing concepts, answer briefly, but your focus \
is product support. Match your length to the question — brief for simple ones, \
longer only when the steps genuinely require it.
"""


def _client():
    try:
        from groq import Groq
    except ImportError:
        return None
    if not GROQ_API_KEY or GROQ_API_KEY.startswith("PASTE_"):
        return None
    return Groq(api_key=GROQ_API_KEY)


def is_available() -> bool:
    return _client() is not None


def chat(messages: list[dict], context: str = "", mode: str = "tutor") -> str:
    """
    messages: [{"role": "user"|"assistant", "content": str}, ...] conversation history.
    context:  optional live dashboard snapshot to ground the answer.
    mode:     "tutor" for the Sentiment Buddy learning assistant, or "support" for
              the Customer Care product-support persona.
    Returns the assistant's reply text.
    """
    client = _client()
    if client is None:
        return ("The chatbot needs a free Groq API key to work. Add GROQ_API_KEY "
                "to your .env file (get one free at console.groq.com) and restart.")

    system = _SUPPORT_PROMPT if mode == "support" else _SYSTEM_PROMPT
    if context:
        system += f"\n\nLIVE DASHBOARD SNAPSHOT (use this for 'right now' questions):\n{context}"

    # Keep only the last 12 turns to stay fast and within free-tier limits
    convo = [{"role": "system", "content": system}] + messages[-12:]

    try:
        resp = client.chat.completions.create(
            model=_MODEL,
            messages=convo,
            temperature=0.6,
            max_tokens=1200,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        log.warning("Chatbot error: %s", exc)
        return ("Sorry — I couldn't reach the AI service just now. "
                "Please try again in a moment.")


# ────────────────────────────────────────────────────────────────────────────
# FILE: api/chat.py
# ────────────────────────────────────────────────────────────────────────────

"""
Vercel serverless chatbot endpoint.

This is the ONLY server-side code on the public site. It just relays to Groq's
free API — no PyTorch, no transformers, no database — so it fits Vercel's
250 MB limit with room to spare (the ML scoring already ran on the machine
that generated the static snapshot).

Needs one env var in Vercel: GROQ_API_KEY
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys
import time
import urllib.request
from collections import deque

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "llama-3.3-70b-versatile"

# ── Abuse limits ──────────────────────────────────────────────────────────────
# This endpoint relays to a free-tier Groq key with no auth in front of it, so
# anyone who finds the URL could drain the quota. These caps are per warm
# serverless instance (Vercel gives no shared state on the free plan) — not
# airtight, but they turn a trivial drain into a slow one.
RATE_WINDOW_SECONDS = 60
RATE_MAX_REQUESTS   = 12          # per client IP per window
MAX_MESSAGES        = 12
MAX_CHARS           = 2000
MAX_CONTEXT         = 16000       # dashboard snapshot the browser sends per turn

# Only these origins may call the endpoint from a browser. Set ALLOWED_ORIGIN in
# the Vercel project settings to your deployed domain.
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "")

_hits: dict[str, deque] = {}


def _rate_limited(ip: str) -> bool:
    now = time.time()
    q = _hits.setdefault(ip, deque())
    while q and now - q[0] > RATE_WINDOW_SECONDS:
        q.popleft()
    if len(q) >= RATE_MAX_REQUESTS:
        return True
    q.append(now)
    # keep the instance's memory bounded
    if len(_hits) > 512:
        for stale_ip in [k for k, v in _hits.items() if not v or now - v[-1] > 300]:
            _hits.pop(stale_ip, None)
    return False

SYSTEM_TUTOR = """You are Sentiment Buddy, a friendly assistant built into the \
SentimentIQ dashboard — a tool that predicts which stocks may improve by \
blending this week's financial news, the stock's price momentum, Wall-Street \
analyst consensus, and the company's SEC filings. You help COMPLETE BEGINNERS \
understand investing and how this dashboard works.

Style: talk like a friendly person texting, not an essay writer. KEEP IT SHORT: \
1 to 3 sentences by default. Only write more when the user explicitly asks you to \
"explain", "go deeper", or "compare". Never dump a long structured answer on a \
simple question. Do NOT use em-dashes (the long dash). Do NOT write dash-bullet \
or numbered lists unless the user asks for a list; answer in plain sentences. \
Warm, plain English, define jargon in a few words when it comes up. Vary how you \
open so you never sound like a template, and don't repeat yourself. Be \
interactive: answer, then when it helps, ask one short follow-up question to keep \
the conversation going, like a chat with a helpful friend.

Never tell anyone to buy or sell a specific stock, and never give personalized \
financial advice. You CAN explain what the data shows and why a rating came out \
the way it did. Mention that this is for learning and you're not a licensed \
advisor ONCE per conversation — the first time it's relevant — not in every \
message. Repeating the disclaimer every turn makes you useless.

How the dashboard works:
- AI SIGNALS: each stock gets BUY / SELL / HOLD with a confidence % and an \
uncertainty % (uncertainty is high when little data backs the call).
- The prediction blends four signals: 30% this week's financial-news sentiment, \
30% price momentum (whether the stock has actually been trending up), 25% \
Wall-Street analyst consensus, and 15% the trajectory of the company's own SEC \
filings (10-K annual / 10-Q quarterly). If some data is missing for a stock, the \
remaining parts are reweighted.
- SENTIMENT SCORE: how positive/negative text sounds, -1 (very bearish) to +1 \
(very bullish), measured by an AI model called FinBERT.
- BULLISH = optimistic/price-might-rise. BEARISH = pessimistic/might-fall.
- Each stock also shows a candlestick chart, all-time returns, max drop, and \
volatility so you can judge how reliable it has been.
- SECTOR MAP: sectors colored by their combined news sentiment.
- WATCHLIST: stars you save. NEWS: headlines linked to the stocks they mention.

Note: this public site is a SNAPSHOT — the data was generated at a point in \
time rather than streaming live. If asked about that, explain it honestly.
"""

SYSTEM_SUPPORT = """You are the SentimentIQ Customer Care assistant — friendly, \
professional product support for the SentimentIQ dashboard. Help with how to use \
features and troubleshooting. Acknowledge the issue first, then give clear steps.

Do NOT give financial or investment advice; you're product support, not a \
financial advisor. Match your length to the question — brief for simple ones, \
longer only when the steps genuinely require it.

Useful facts: the public site is a static snapshot (data as of the timestamp in \
the header), so it doesn't stream live updates. The AI Signals tab has the \
BUY/SELL/HOLD calls; each stock's Investment View has the candlestick chart.
"""


def _reply(messages, mode, context=""):
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return ("The chatbot needs a free Groq API key. Add GROQ_API_KEY in the "
                "Vercel project settings (get one free at console.groq.com).")
    system = SYSTEM_SUPPORT if mode == "support" else SYSTEM_TUTOR
    if context:
        # The browser sends a snapshot of exactly what the user is looking at, so
        # answers can quote the same numbers that are on screen.
        system += (
            "\n\nLIVE DASHBOARD DATA (what the user is looking at right now):\n"
            + context +
            "\n\nAnswer using these exact numbers and explain what they mean in "
            "plain English. The data includes a roster of EVERY stock on the "
            "dashboard (ticker, rating, confidence, sector, 1-year return, news "
            "mix) plus detailed blocks for any stock the user named — so you can "
            "answer 'which stocks are BUY?', 'list the tech stocks', or compare "
            "two tickers directly from the roster. If the user says \"this stock\" "
            "or \"it\", they mean the detailed stock above. If they ask something "
            "the data doesn't cover, say so plainly instead of inventing a figure. "
            "You may explain why a rating is what it is, but never tell them to "
            "buy or sell."
        )
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": system}] + messages[-MAX_MESSAGES:],
        "temperature": 0.6,
        "max_tokens": 1200,
    }).encode()
    req = urllib.request.Request(
        GROQ_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}",
                 # Cloudflare fronts the Groq API and 403s (error 1010) the default
                 # "Python-urllib/3.x" agent, so identify ourselves explicitly.
                 "User-Agent": "SentimentIQ/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read())
        return out["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        # Surface the cause in the Vercel function log; keep the UI message kind.
        detail = getattr(exc, "read", lambda: b"")()[:300] or str(exc).encode()
        print(f"groq call failed: {type(exc).__name__}: {detail!r}", file=sys.stderr)
        return "Sorry — I couldn't reach the AI service just now. Please try again."


class handler(BaseHTTPRequestHandler):
    def _send(self, status: int, obj: dict) -> None:
        out = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        if ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(out)

    def _client_ip(self) -> str:
        fwd = self.headers.get("X-Forwarded-For", "")
        return (fwd.split(",")[0].strip() if fwd else self.client_address[0]) or "?"

    def do_OPTIONS(self):
        self.send_response(204)
        if ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        # Reject cross-site callers when an origin allow-list is configured.
        origin = self.headers.get("Origin", "")
        if ALLOWED_ORIGIN and origin and origin != ALLOWED_ORIGIN:
            return self._send(403, {"reply": "This chatbot is not available from that site."})

        if _rate_limited(self._client_ip()):
            return self._send(429, {"reply": "You're sending messages very quickly — "
                                             "give it a few seconds and try again."})

        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 96_000:
                return self._send(413, {"reply": "That message is too long for me to read."})
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        messages = payload.get("messages")
        if not isinstance(messages, list):
            messages = []
        mode = payload.get("mode", "tutor")
        if mode not in ("tutor", "support"):
            mode = "tutor"
        clean = [
            {"role": "assistant" if m.get("role") == "assistant" else "user",
             "content": str(m.get("content", ""))[:MAX_CHARS]}
            for m in messages[-MAX_MESSAGES:]
            if isinstance(m, dict) and m.get("content")
        ]
        # Snapshot of the stock(s) the user is looking at, assembled in the browser
        context = str(payload.get("context", "") or "")[:MAX_CONTEXT]
        text = (_reply(clean, mode, context) if clean
                else "Ask me anything about the dashboard or investing basics!")
        self._send(200, {"reply": text})


# ────────────────────────────────────────────────────────────────────────────
# FILE: api/verify.py
# ────────────────────────────────────────────────────────────────────────────

"""
Vercel serverless Finviz cross-check.

On the static (Vercel) site, this is what makes verification LIVE: the page's
signals are a snapshot, but this function fetches Finviz in real time so a
visitor can confirm our BUY/SELL/HOLD against current analyst data. Stdlib
only (no ML deps) so it fits Vercel comfortably.

    GET /api/verify?t=AAPL&signal=BUY
"""
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import re
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
FIELDS = ["Price", "Recom", "Target Price",
          "Perf Week", "Perf Month", "Perf Year", "Perf YTD"]


def _grab(html, label):
    i = html.find(f">{label}</a>")
    if i == -1:
        i = html.find(f">{label}<")
    if i == -1:
        return None
    j = html.find("snapshot-td-content", i)
    if j == -1:
        return None
    chunk = html[j:html.find("</div>", j)]
    text = re.sub(r"<[^>]+>", "", chunk).replace('snapshot-td-content"', "").strip(' ">')
    return text or None


def _num(s):
    if not s:
        return None
    try:
        return float(s.replace("%", "").replace("$", "").replace(",", ""))
    except ValueError:
        return None


def _recom_signal(r):
    if r is None:
        return None
    return "BUY" if r <= 2.5 else "SELL" if r >= 3.5 else "HOLD"


def _agreement(ours, theirs):
    if theirs is None:
        return "UNKNOWN"
    if ours == theirs:
        return "AGREE"
    if "HOLD" in (ours, theirs):
        return "MIXED"
    return "DISAGREE"


def _fetch(ticker):
    url = f"https://finviz.com/quote.ashx?t={ticker}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except Exception:
        return None
    if "snapshot-td-content" not in html:
        return None
    price = _num(_grab(html, "Price"))
    recom = _num(_grab(html, "Recom"))
    target = _num(_grab(html, "Target Price"))
    upside = round((target / price - 1.0) * 100, 1) if (target and price) else None
    return {
        "ticker": ticker, "price": price, "recom": recom,
        "recom_signal": _recom_signal(recom),
        "target": target, "target_upside": upside,
        "perf_week": _grab(html, "Perf Week"),
        "perf_month": _grab(html, "Perf Month"),
        "perf_year": _grab(html, "Perf Year"),
        "perf_ytd": _grab(html, "Perf YTD"),
        "source_url": url,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        ticker = (q.get("t", [""])[0] or "").upper().lstrip("$")
        ticker = re.sub(r"[^A-Z.\-]", "", ticker)
        our = (q.get("signal", [""])[0] or "").upper()
        data = _fetch(ticker) if ticker else None
        if data and our:
            data["our_signal"] = our
            data["agreement"] = _agreement(our, data.get("recom_signal"))
        code = 200 if data else 404
        body = json.dumps(data or {"error": "finviz unavailable", "ticker": ticker}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "public, max-age=1800")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ────────────────────────────────────────────────────────────────────────────
# FILE: scripts/backfill_rescore.py
# ────────────────────────────────────────────────────────────────────────────

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


# ────────────────────────────────────────────────────────────────────────────
# FILE: scripts/refresh_once.py
# ────────────────────────────────────────────────────────────────────────────

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
