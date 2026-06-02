# Financial News Sentiment Pipeline

> IST 495 · Agentic AI Internship · Penn State · Summer 2026  
> Student: Samarth Patel · Supervisor: Prof. Kaamran Raahemifar

A real-time financial news sentiment analysis system that ingests headlines from 17+ sources, scores them using FinBERT and VADER, ranks them by trust/time-decay weighted scores, and displays results on a live Bloomberg-style dashboard.

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

## Sources
Reuters · Dow Jones · CNBC · MarketWatch · PR Newswire · ACCESS Wires · FinanceWire · GlobeNewswire · Yahoo Finance · Seeking Alpha · TradingView · FinViz · SEC EDGAR · FDA · Finnhub · NewsAPI

## Stack
- **Python 3.11** — all code
- **FinBERT** (ProsusAI/finbert) — financial NLP
- **VADER** — fallback sentiment
- **Flask + SSE** — real-time dashboard
- **SQLAlchemy + SQLite** — storage
- **CrewAI + Groq** (LLaMA 3.1 8B) — AI narrative
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
