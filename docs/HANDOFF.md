# Integration & Handoff Guide — SentimentIQ

_Prepared for the integration/testing team. Companion to the top-level `README.md`._
_Author: Samarth Patel · IST 495 · Summer 2026_

This document is everything a new engineer needs to run the project, understand
how it fits together, test it, and pick up the next pieces of work.

---

## 0. Where the code is

This handoff folder holds **documentation only**. The full source code is here:

```
git clone https://github.com/Samarthpatel29/financial-news-sentiment
```

Clone that, then follow §2 to run it.

## 1. What this is (60 seconds)

It reads free financial **news** and companies' official **SEC reports**, an AI
scores how positive/negative each one is, and it combines that with **price
momentum** and **Wall-Street analyst consensus** into a simple **Buy / Sell /
Hold** rating per stock — shown on a live dashboard with charts, a sector map, a
news feed, a beginner chatbot, and a one-click Finviz cross-check.

**100% free / no paid APIs.** The only optional key is a free Groq key (AI text).

## 1a. First 30 minutes (start here)

1. `git clone` the repo (above) and `cd` into it.
2. `python3.11 -m venv .venv && source .venv/bin/activate`
3. `pip install -r requirements.txt`  *(installs PyTorch etc. — a few minutes)*
4. `pytest -q` — should print **71 passed**. If it does, your setup is good.
5. `python run.py` — first run downloads FinBERT (~440 MB) once, then serves
   `http://localhost:5001`. Wait ~1–2 minutes for the first data to appear.
6. Open the dashboard and click a stock — you should see the Chart, then the
   other tabs fill in.
7. (Optional) put `GROQ_API_KEY=…` in a `.env` file to turn on the AI narrative,
   filing summaries, and chatbot. Everything else works without it.

If step 4 passes and step 5 shows stock ratings, you have a working system to
test against.

---

## 2. Run it locally

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py                      # → http://localhost:5001
```

- First launch downloads FinBERT (~440 MB) once, then caches it.
- Port 5001 (5000 is taken by macOS AirPlay). Override with `DASHBOARD_PORT`.
- `python run.py --once` runs a single pipeline cycle and exits (handy for CI).
- `python run.py --no-dashboard` runs the pipeline without the web server.
- Optional `.env`: `GROQ_API_KEY=…` enables AI narrative, filing summaries, chatbot.

**Env vars worth knowing** (all have safe defaults, see `config/settings.py`):
`PIPELINE_INTERVAL_SECONDS`, `TICKER_WINDOW_HOURS` (168 = 1 week),
`USE_GROQ_SUMMARIES`, `REPORT_HISTORY_MAX_FILINGS`, `DATABASE_URL`.

---

## 3. How the pieces fit

```
run.py  ── starts two schedulers + the Flask app in threads
  │
  ├─ NEWS cycle (every 60s, faster off-hours)      src/pipeline/crew.py
  │     collect → dedup → FinBERT/VADER score → rank → aggregate per ticker
  │     sources: src/collectors/{rss,scraper,stocktwits,broker}_collector.py
  │
  ├─ FUNDAMENTALS cycle (every 6h)                 src/pipeline/fundamentals.py
  │     SEC EDGAR filings → extract sections → FinBERT + Groq verdict
  │     → blend 4 signals (news/momentum/analysts/reports) → Buy/Sell/Hold
  │     price: src/collectors/price_history.py   analysts: finviz_verify.py
  │
  └─ Flask dashboard + JSON API                    src/dashboard/app.py
        SSE live updates · one big template        src/dashboard/templates/index.html
        chatbot                                     src/dashboard/chatbot.py

Storage: SQLite via SQLAlchemy                      src/storage/models.py
```

### The prediction
`src/pipeline/fundamentals.py :: _aggregate()` computes, per ticker:
```
rating = 0.30·news + 0.30·price_momentum + 0.25·analyst_consensus + 0.15·reports
```
(weights renormalise over whichever signals are available). Threshold ±0.12 →
Buy / Sell / Hold. See `README.md` → "Prediction Engine" for the full table and
the design rationale.

---

## 4. File map (where to look)

| Area | File | Notes |
|---|---|---|
| Entry point | `run.py` | schedulers + Flask, in threads |
| Settings | `config/settings.py` | feeds, weights, windows, dead-feed lists |
| Ticker universe | `config/tickers.py` | symbols + company-name map |
| Sector map | `config/sectors.py` | ticker → sector |
| News collectors | `src/collectors/{rss,scraper,stocktwits,broker}_collector.py` | |
| SEC filings | `src/collectors/edgar_collector.py`, `edgar_extractor.py` | fetch + section-extract |
| Prices / candles | `src/collectors/price_history.py` | yfinance, cached |
| Analyst check | `src/collectors/finviz_verify.py` | Finviz scrape, cached 30 min |
| Sentiment | `src/sentiment/{finbert,vader,scorer}.py` | FinBERT primary, VADER fallback |
| Ticker extraction | `src/sentiment/ticker_extractor.py` | 3-pass ($TAG / ALL-CAPS / name) |
| News pipeline | `src/pipeline/crew.py` | one 60s cycle |
| Prediction engine | `src/pipeline/fundamentals.py` | filings + 4-signal blend + self-scoring |
| Per-ticker rollup | `src/pipeline/aggregator.py` | news → ticker sentiment |
| DB models | `src/storage/models.py` | additive migrations; **never drops columns** |
| Web app / API | `src/dashboard/app.py` | all endpoints |
| Front-end | `src/dashboard/templates/index.html` | single self-contained page (HTML/CSS/JS) |
| Chatbot | `src/dashboard/chatbot.py` | Groq relay, two personas |
| Static export | `export_static.py` | renders `./public` for Vercel |
| Serverless | `api/chat.py`, `api/verify.py` | the only server-side code on Vercel |

---

## 5. API reference (Flask, local)

| Endpoint | Returns |
|---|---|
| `GET /` | the dashboard (server-rendered with initial data) |
| `GET /stream` | SSE stream of live updates (news, tickers, stats, narrative) |
| `GET /api/ranked` | top news articles (each linked to its tickers) |
| `GET /api/tickers` | per-ticker news sentiment (feeds the Sector Map) |
| `GET /api/stats` | header stats + AI narrative |
| `GET /api/fundamentals` | **the signals**: Buy/Sell/Hold + 4 components + accuracy |
| `GET /api/price/<ticker>` | all-time price stats + sparkline |
| `GET /api/candles/<ticker>` | daily OHLC for the candlestick chart |
| `GET /api/verify/<ticker>?signal=BUY` | Finviz cross-check (AGREE/MIXED/DISAGREE) |
| `GET /api/ticker-events/<ticker>` | timeline: filings + news |
| `GET /api/market-status` | market open/closed |
| `POST /api/chat` | chatbot (`{messages, mode, ticker}`) |

On Vercel the static shim rewrites the read-only endpoints to pre-exported JSON;
`/api/chat` and `/api/verify` stay live serverless functions.

---

## 6. Tests

```bash
pytest -q          # 71 tests, ~0.6s, all green as of 2026-07-24
```
Coverage: sentiment scoring, ticker extraction, aggregation, collectors (mocked
network). Network-dependent modules (EDGAR, yfinance, Finviz) are exercised via
their pure-function parsers, not live calls, so the suite is deterministic and
offline-safe.

**Good first integration tests to add:** an end-to-end `run.py --once` smoke test;
a golden-file test on `_aggregate()` weighting; contract tests for each
`/api/*` JSON shape.

### Manual test checklist (click-through)
After `python run.py`, confirm each of these in the browser:
- [ ] Stock list loads with Buy/Sell/Hold labels and confidence %.
- [ ] Clicking a stock opens the Chart tab with candlesticks.
- [ ] Breakdown tab shows four components that match the overall rating.
- [ ] Verify on Finviz shows AGREE/MIXED/DISAGREE (needs internet).
- [ ] Sector Map colours load; clicking a sector lists its stocks.
- [ ] Watchlist star saves and the Watchlist tab filters to saved stocks.
- [ ] News tab shows headlines with ticker tags that jump to the stock.
- [ ] Chatbot answers a stock question (needs a Groq key).
- [ ] Header numbers and the bottom news strip update within ~60s.

---

## 7. Known gaps & honest caveats

1. **Not a price oracle.** The rating is a sentiment+data blend, not a forecast.
   Keep the "educational only" framing; don't let it drift into advice.
2. **Finviz / EDGAR / yfinance are scraped or unofficial.** They can rate-limit or
   change HTML. All three fail *gracefully* (cached, best-effort, the signal just
   drops out) — but expect occasional gaps. Finviz has a per-cycle failure
   circuit-breaker (`_fetch_recom`).
3. **StockTwits is ~2/3 of raw news volume** and is intentionally down-weighted
   (0.3–0.4×). If social noise dominates a ticker, that's why.
4. **Reports signal is weak by design** — FinBERT reads filings as neutral, so
   Groq verdicts carry it. Without a Groq key, the reports component ≈ 0.
5. **Vercel is a snapshot**, not live. Re-run `export_static.py` + redeploy to
   refresh. (A GitHub Action could automate this — see below.)
6. **Brief items still open:** Finviz screener-CSV ingest and TD Ameritrade / IBKR
   feeds were in the original assignment but blocked on credentials / retired APIs.
   "Tweets' sentiment" is served by StockTwits + Reddit (X API is paywalled).

---

## 8. Suggested next steps (roadmap)

- **Automate the public refresh:** a scheduled GitHub Action can run the pipeline
  (GitHub runners have 16 GB RAM → PyTorch fits) and redeploy the snapshot hourly.
- **Backtest the accuracy self-score** over a longer window; surface a per-signal
  precision breakdown (Buy vs Sell hit-rate).
- **Tune the blend weights** against the graded `SignalHistory` outcomes instead
  of the current hand-set 30/30/25/15.
- **Swap SQLite → Postgres** if multiple workers/write-concurrency are needed.
- **Add real analyst price-target history** and earnings dates as extra signals.
- **Harden scrapers** with retries/backoff and a shared fetch layer.

---

## 9. Contacts / provenance

- Built by Samarth Patel for IST 495 (Prof. Kaamran Raahemifar), Summer 2026.
- AI-assisted development (Claude / CrewAI); architecture, source/model choices,
  prompts, testing, and direction by the student.
- Repo: https://github.com/Samarthpatel29/financial-news-sentiment
