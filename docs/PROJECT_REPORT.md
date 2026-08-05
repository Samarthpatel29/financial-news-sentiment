# Project Report — SentimentIQ

**Student:** Samarth Patel
**Course:** IST 495 — Agentic AI Internship
**Supervisor:** Prof. Kaamran Raahemifar
**Term:** Summer 2026 (May 5 – Aug 15, 2026 · 250 hours · 25 hrs/week · remote)

---

## 1. Summary

SentimentIQ reads free financial news and companies' official SEC reports, uses
AI to score how positive or negative each one is, and combines that with the
stock's price trend and Wall-Street analyst ratings into a simple **Buy / Sell /
Hold** rating for each stock. Results are shown on a live web dashboard.

Everything runs on free tools — no paid data or API. The code is Python.

**By the numbers (as of this report):**

| Metric | Value |
|---|---|
| News items scored | ~87,000 |
| Stocks tracked | 88 |
| Stocks with a full Buy/Sell/Hold signal | 71 |
| SEC filings downloaded and scored | ~980 |
| Signal accuracy (self-checked, see §6) | 55% of 161 graded calls |
| Lines of Python | ~4,200 across 31 files |
| Automated tests | 71 (all passing) |
| Update speed | new news scored in under 2 minutes |

---

## 2. How it meets the assignment

The table maps each item from the project brief to what was built.

| Requirement in the brief | Status | Notes |
|---|---|---|
| Analyze stocks and financial news from reputable sources | **Done** | 14 free sources (see §4) |
| Sources often have RSS feeds; scrape otherwise | **Done** | RSS where available, HTML scraping for the rest |
| Reputable outlets: CNBC, MarketWatch, PR Newswire, GlobeNewswire, FDA, SEC | **Done** | All included and working |
| Reuters, Dow Jones, TradingView | **Partial** | These killed their free public feeds; replaced with equivalent free sources |
| Finviz screener CSV extract (credentials to be provided) | **Not done** | Credentials were never provided; instead the app cross-checks each rating against Finviz's public page |
| TD Ameritrade / Interactive Brokers feeds | **Not done** | TD Ameritrade's API was retired after the Schwab merger; IBKR needs a funded brokerage account |
| Dashboard like the Finviz screener, tickers ranked by sentiment and message density | **Done** | Ranked stock list plus a sector heatmap |
| Ranked by *tweets'* sentiment | **Substituted** | X/Twitter API now costs $100+/month; used StockTwits and Reddit (free) instead |
| Pop-up window with real-time sentiment scores and message density | **Done** | Click any stock → detail panel with scores, chart, and breakdown |
| Code in Python | **Done** | 100% Python |
| Real-time with delay under 1–2 minutes | **Done** | News is scored within ~2 minutes; the news cycle runs every 60 seconds |
| Use AI tools (Claude.ai, CrewAI, agent builders) | **Done** | Built with Claude.ai; CrewAI + Groq used for the AI narrative and summaries |

**Beyond the brief**, the project also added: a Buy/Sell/Hold prediction that
blends four signals, candlestick charts, all-time price stats, a beginner
chatbot, a one-click Finviz verification tab, an honest accuracy self-check, and
a public website deployed on Vercel.

---

## 3. What the app does (feature list)

- **AI Signals** — each stock gets a Buy / Sell / Hold rating with a confidence
  percentage. This is the main screen.
- **Per-stock detail**, opened by clicking a stock:
  - *Chart* — candlestick price chart (last 45 days) with Open/High/Low/Close and
    1-year / 5-year returns.
  - *Why this rating* — the reasons in plain English.
  - *Reports* — the company's recent SEC filings with short AI summaries.
  - *Breakdown* — the four parts of the rating and how they add up.
  - *Verify on Finviz* — pulls live analyst data from Finviz and says whether it
    agrees, is mixed, or disagrees with our rating.
  - *Timeline* — that stock's filings and news, newest first.
- **Sector Map** — sectors coloured by sentiment; click one to see its stocks.
- **Watchlist** — save stocks (stored in the browser, no login).
- **News** — the week's headlines, each linked to the stock it is about.
- **Chatbot** — a beginner-friendly assistant that answers questions using the
  numbers on screen. It explains; it does not give buy/sell advice.
- **Live header** — market sentiment index, market open/closed, article counts.

---

## 4. Data sources (all free)

**News (RSS + scraping):** CNBC, MarketWatch, PR Newswire, GlobeNewswire,
Seeking Alpha, Investing.com, Business Insider, Fortune, Google News, and the
FDA press-release feed.

**Company filings:** SEC EDGAR — 10-K (annual), 10-Q (quarterly), 8-K (events),
downloaded straight from the official government site.

**Social:** StockTwits and Reddit (r/stocks, r/wallstreetbets, r/investing).
StockTwits is about two-thirds of the raw volume, so social posts are weighted
lower than professional news outlets.

**Prices:** Yahoo Finance (via the `yfinance` library).
**Analyst ratings:** Finviz public quote pages.

A source audit was run against the stored data; feeds that never returned
anything (for example Reuters and Dow Jones, which removed their public feeds)
were removed rather than left in and counted.

---

## 5. How the Buy / Sell / Hold rating is calculated

Each rating blends four independent signals. Every signal runs from −1 (bad) to
+1 (good). If a signal is missing, the others are re-weighted to fill in.

| Signal | Weight | Where it comes from |
|---|---|---|
| News this week | 30% | AI sentiment (FinBERT) of the week's headlines |
| Price momentum | 30% | 1-year and 5-year returns, distance from all-time high |
| Analyst consensus | 25% | Finviz analyst rating (1 = Strong Buy … 5 = Strong Sell) |
| SEC filings | 15% | AI verdict (Improving / Stable / Deteriorating) on the filings |

```
rating = 0.30·news + 0.30·momentum + 0.25·analysts + 0.15·reports
Buy if rating > +0.12   ·   Sell if rating < −0.12   ·   Hold otherwise
```

**Design note:** an earlier version used only news sentiment, which gave
misleading calls (for example a "Sell" on a stock that was up 70%). Price and
analyst signals were added on July 24 so the ratings line up with the market and
with the Finviz cross-check. This is a sentiment-and-data blend for learning, not
a guaranteed price forecast — the app states this clearly.

---

## 6. How accuracy is measured (honest self-check)

Every day the app saves each stock's current Buy/Sell/Hold call. Seven days
later it checks the call against the actual price move and marks it right or
wrong. The header shows the running score.

So far: **88 of 161 graded calls were correct (55%)**. This number is shown to
users as-is, and disagreements with Finviz are shown openly rather than hidden.

---

## 7. Technology used

- **Python 3.11** — all code.
- **FinBERT** (a finance-trained AI model) — reads sentiment; **VADER** as backup.
- **Groq (LLaMA)** — writes the plain-English summaries and powers the chatbot
  (free tier).
- **CrewAI** — organizes the AI narrative step.
- **Flask** — the web server; live updates stream to the browser.
- **SQLAlchemy + SQLite** — stores all the data (one file).
- **yfinance / BeautifulSoup / feedparser** — data collection.
- **Vercel** — hosts the public website.

The AI scoring runs locally on the computer, so it costs nothing to run.

---

## 8. Known limits (stated plainly)

1. It is not a guaranteed price predictor — it is a sentiment-and-data score.
2. Some sources (Finviz, Yahoo, SEC) are scraped or use unofficial access; they
   can occasionally be slow or unavailable. The app handles this gracefully — the
   missing signal simply drops out.
3. The public Vercel website is a snapshot, refreshed by re-running an export,
   not a 24/7 live server (the AI model is too large for free hosting).
4. Two brief items (Finviz screener CSV, broker feeds) are still open and depend
   on credentials or accounts that were not available.

---

## 9. How to run it

```
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
# then open http://localhost:5001
```

The first run downloads the AI model once (~440 MB), then caches it. A free Groq
key is optional and only affects the AI text features.

More detail for engineers is in `HANDOFF.md`; a step-by-step tour of the app is
in `DEMO_WALKTHROUGH.md`.

---

## 10. Deliverables in this folder

- `PROJECT_REPORT.md` — this document
- `README.md` — project overview and setup
- `HANDOFF.md` — technical guide for the next team
- `DEMO_WALKTHROUGH.md` — a tour of the app (also usable as a recording script)
- `FUNDAMENTALS_PLAN.md` — the design plan for the SEC-filings engine
- `requirements.txt` — the Python packages needed

Full source code: https://github.com/Samarthpatel29/financial-news-sentiment
