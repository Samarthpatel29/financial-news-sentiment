---
title: SentimentIQ
emoji: 📈
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# Financial News Sentiment Pipeline

> IST 495 · Agentic AI Internship · Penn State · Summer 2026  
> Student: Samarth Patel · Supervisor: Prof. Kaamran Raahemifar

A real-time financial news sentiment analysis system that ingests headlines from 15 free sources, scores them using FinBERT and VADER, ranks them by trust/time-decay weighted scores, and displays results on a live Bloomberg-style dashboard with a built-in AI tutor chatbot. The pipeline auto-fetches around the clock — every 60s in pre-market/after-hours, when overnight news matters most — so the board is always current before the market opens.

## Quick start (for testing)

Requires **Python 3.11+**. From the project folder:

```bash
python -m venv .venv && source .venv/bin/activate   # create + activate a virtualenv
pip install -r requirements.txt                      # install dependencies (first run downloads FinBERT ~400 MB)
cp .env.example .env                                 # optional: add a free Groq key for the chatbot/LLM
bash start.sh                                         # runs the pipeline + dashboard, opens http://localhost:5001
```

That's it — the live dashboard streams updates every ~60s. To run the automated tests:

```bash
pytest tests/ -q
```

No paid API keys are required; everything runs on free data. The Groq key is optional (the LLM layer falls back to FinBERT without it).

## Project structure

```
run.py             → single entry point (pipeline + dashboard)
start.sh           → one-command launch (calls run.py)
requirements.txt   → dependencies
src/
  collectors.py    → all data collectors (RSS news, StockTwits, SEC/EDGAR, Finviz, prices)
  sentiment.py     → sentiment scoring (FinBERT + VADER + optional LLM judge + ranking)
  pipeline.py      → orchestration: crew, per-ticker aggregation, fundamentals/predictions
  storage.py       → database models (SQLAlchemy)
  utils.py         → market-hours helper
  dashboard/       → Flask app (app.py) + the HTML dashboard (templates/index.html)
config/            → settings, ticker universe, sector map
api/               → serverless functions for the Vercel deploy (chatbot, verify)
tests/             → automated test suite   ·   scripts/ → maintenance scripts
docs/              → all documentation (reports, accuracy, differentiation, handoff)
data/              → runtime database (created on first run)
public/            → generated static snapshot for the web deploy
project-admin/     → school/admin files (activity logs, charts) — not part of the code
SOURCE_CODE_BUNDLE.py → all Python source in one file, for quick review
```

## Architecture

```
RSS Feeds + StockTwits + SEC EDGAR (15 live sources)
        ↓
  Async Collector (aiohttp + feedparser + BeautifulSoup)
        ↓
  Dedup by URL *and* normalised headline (cross-source re-syndication)
        ↓
  FinBERT Scorer (primary) + VADER (fallback, finance-tuned lexicon)
  sentiment = P(positive) − P(negative), continuous in [-1, 1]
        ↓
  Rank Score = |sentiment| × density × trust_weight × time_weight
        (density normalised to (0,1] — a source's share of the window)
        ↓
  Ticker Extraction + Per-Ticker Aggregation
        ↓
  CrewAI + Groq LLaMA 3.1 (AI Narrative)
        ↓
  Flask Dashboard (SSE real-time streaming)
```

## Sources (all free, no paid API)

**News (10):** CNBC · MarketWatch · PR Newswire · GlobeNewswire · Seeking Alpha · Investing.com · Business Insider · Fortune · Google News · FDA press releases

**Filings:** SEC EDGAR (10-K / 10-Q / 8-K, via `edgar_collector.py`)

**Social (4):** **StockTwits** (free social sentiment — the no-cost alternative to the paid X/Twitter API) · Reddit (r/stocks, r/wallstreetbets, r/investing)

> An audit on 2026-07-21 checked all configured feeds against 80k stored rows and
> found eight that had never returned a single article (Reuters, Dow Jones,
> Nasdaq, Yahoo Finance, ACCESS Wires, FinanceWire, Benzinga, CNN Business) plus
> three scraper targets that were bot-blocked or pointed at a non-existent
> endpoint (FinViz, TradingView, an SEC full-text URL). Those are parked in
> `DEAD_FEEDS` / `DEAD_SCRAPERS` in `config/settings.py` rather than advertised.
> StockTwits supplies roughly two-thirds of raw volume, so social content is
> explicitly down-weighted (0.3–0.4x) against news outlets.

## Features
- **AI Signals** — every tracked stock gets a **Buy / Sell / Hold** rating with a confidence % (see the prediction engine below)
- **Per-stock detail** — candlestick chart, plain-English "why this rating", SEC reports with AI summaries, a four-signal breakdown, a **Finviz cross-check**, and an event timeline
- **Sector Map** — sectors coloured by combined sentiment; click one to see its stocks and drivers
- **Watchlist** — star stocks (saved in the browser, no account)
- **News feed** — top-ranked headlines, each linked to the stock it's about (click → jump to that stock's signal)
- **Market-hours engine** — faster polling in pre-market/after-hours, live OPEN/CLOSED badge
- **AI Narrative** — Groq summarizes the market mood each cycle
- **Sentiment Buddy chatbot** — a free beginner-friendly AI tutor, grounded in the live data, that can answer stock-specific questions ("why is JPM a hold?")

## Prediction Engine (AI Signals)

Each stock's **Buy / Sell / Hold** rating blends **four independent signals**, each on a −1 (bad) … +1 (good) scale. Weights renormalise if a signal is missing:

| Signal | Weight | Source |
|---|---|---|
| 📰 **News** (this week) | 30% | FinBERT sentiment of the week's headlines |
| 📈 **Price momentum** | 30% | 1-yr / 5-yr returns + distance from all-time high (yfinance) |
| 👔 **Analyst consensus** | 25% | Finviz "Recom" (1=Strong Buy … 5=Strong Sell), mapped to −1…+1 |
| 🏛️ **SEC filings** | 15% | Groq verdict (Improving/Stable/Deteriorating) on 10-K/10-Q/8-K |

```
rating = 0.30·news + 0.30·momentum + 0.25·analysts + 0.15·reports   (renormalised)
Buy  if rating >  0.12   ·   Sell if rating < −0.12   ·   Hold otherwise
```

> **Design note (important for the integration team):** this is a *sentiment-and-data blend*, **not** a guaranteed price forecast. Earlier versions were ~100% short-term news sentiment, which produced misleading calls (e.g. "Sell" on a stock that was up 70%). Price momentum and analyst consensus were added on **2026-07-24** specifically so ratings line up with market reality and with the built-in Finviz cross-check. The reports signal is deliberately the *lowest* weight because FinBERT flatlines on dry filing text — Groq verdicts do the real work there.

**Honest self-scoring:** every day the model's Buy/Sell/Hold calls are snapshotted and graded 7 days later against the actual price (`SignalHistory` table). The header shows the running accuracy (`accuracy self-check: N% of M signals`).

**Independent verification:** the *Verify on Finviz* tab fetches live analyst data and reports **AGREE / MIXED / DISAGREE** vs our rating, so any prediction can be checked against an external source.

## Stack (100% free / open-source)
- **Python 3.11** — all code
- **FinBERT** (ProsusAI/finbert) — financial NLP
- **VADER** — fallback sentiment
- **Flask + SSE** — real-time dashboard
- **SQLAlchemy + SQLite** — storage
- **Groq** (LLaMA 3.1 8B, free tier) — AI narrative + chatbot
- **CrewAI** — agentic narrative orchestration
- **aiohttp + feedparser + BeautifulSoup** — data collection

## Quick Start
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
# Dashboard at http://localhost:5001
```

Or just run:
```bash
bash start.sh
```

First run downloads the FinBERT model (~440 MB) from HuggingFace; subsequent runs are cached. Optional: put a free `GROQ_API_KEY` in a `.env` file to enable the AI narrative, filing summaries, and chatbot (everything else works without it).

## Public deployment (Vercel)

The live ML stack is ~1.4 GB (PyTorch), far over Vercel's 250 MB serverless limit, so the public site is a **static snapshot**:

```bash
python export_static.py        # runs the pipeline locally, writes ./public/*.json
vercel deploy --prod --yes     # deploys the 1.9 MB snapshot
```

- All scoring happens on your machine; Vercel just serves the JSON.
- Two things stay **live** on the public site via tiny stdlib-only serverless functions: the **chatbot** (`api/chat.py` → Groq) and the **Finviz verify** check (`api/verify.py`).
- Set `GROQ_API_KEY` (and optionally `ALLOWED_ORIGIN`) in the Vercel project settings.

See **`docs/HANDOFF.md`** for the integration team's guide (file map, API reference, test status, known gaps, and next steps) and **`docs/DEMO_WALKTHROUGH.md`** for a screenshot walkthrough of the app.

## Project Timeline
May 5 – Aug 15, 2026 (15 weeks · 250 hours)
