# Financial News Sentiment Pipeline

> IST 495 · Agentic AI Internship · Penn State · Summer 2026  
> Student: Samarth Patel · Supervisor: Prof. Kaamran Raahemifar

A real-time financial news sentiment analysis system that ingests headlines from 25+ free sources, scores them using FinBERT and VADER, ranks them by trust/time-decay weighted scores, and displays results on a live Bloomberg-style dashboard with a built-in AI tutor chatbot. The pipeline auto-fetches around the clock — every 60s in pre-market/after-hours, when overnight news matters most — so the board is always current before the market opens.

## Architecture

```
RSS Feeds / Scrapers (17+ sources)
        ↓
  Async Collector (aiohttp + feedparser + BeautifulSoup)
        ↓
  FinBERT Scorer (primary) + VADER (fallback)
        ↓
  Rank Score = |sentiment| × density × trust_weight × time_weight
        ↓
  Ticker Extraction + Per-Ticker Aggregation
        ↓
  CrewAI + Groq LLaMA 3.1 (AI Narrative)
        ↓
  Flask Dashboard (SSE real-time streaming)
```

## Sources (all free, no paid API)
Reuters · Dow Jones · CNBC · MarketWatch · PR Newswire · ACCESS Wires · FinanceWire · GlobeNewswire · Yahoo Finance · Seeking Alpha · TradingView · FinViz · SEC EDGAR · FDA · Nasdaq · Investing.com · Benzinga · Business Insider · CNN Business · Fortune · Google News · **StockTwits** (free social sentiment — the no-cost alternative to the paid X/Twitter API) · Reddit (r/stocks, r/wallstreetbets, r/investing)

## Features
- **Live card grid** — top-ranked news as image cards with sentiment badges, auto-refreshing via SSE
- **Ticker heatmap** — stocks ranked by aggregated news + social sentiment
- **Market-hours engine** — faster polling in pre-market/after-hours, live OPEN/CLOSED badge
- **AI Narrative** — Groq LLaMA 3.1 summarizes the market mood each cycle
- **Sentiment Buddy chatbot** — a free beginner-friendly AI tutor that explains every concept and answers questions about the live data on screen

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

## Project Timeline
May 5 – Aug 15, 2026 (15 weeks · 250 hours)
