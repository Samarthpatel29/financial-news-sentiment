# Project Status — quick reference

Live site: **https://financial-news-sentiment.vercel.app**

## ✅ Done (live)
- **Predictions** from financial **news + earnings (SEC filings) + price momentum + analyst consensus** → Buy/Sell/Hold per stock
- **Reputable sources**: Reuters, Dow Jones (WSJ), ACCESS Newswire, Business Wire, CNBC, MarketWatch, PR Newswire, GlobeNewswire, FDA, SEC EDGAR, + Reddit & StockTwits (tweets)
- **Finviz-style screener** ranked table + a sortable **Tweets** column (rank by tweets' sentiment + volume)
- **Real-time pop-up** showing sentiment + message density (article & ticker)
- **Accuracy tracking** on weekly (7-day) and monthly (30-day) horizons
- **Sentiment Buddy** chatbot that knows every stock on the board
- **Auto-refresh**: launchd job every 2h (Mac on) → pushes → Vercel redeploys. Live app (`start.sh`) streams every ~60s (that's the real-time <1–2 min deliverable).

## ⚠️ Fixed 2026-09-20 — the analyst component was mostly missing

The blend documents analyst consensus at **25%**, but it was sourced by scraping
Finviz, which bot-blocks server-side requests. A circuit breaker in
`_fetch_recom` gave up after 4 failures per cycle, so with ~125 tracked tickers
only a handful ever got a value: **`comp_analysts` was populated in 94 of 4,661
logged signals (2.0%)**. For 98% of predictions the weights renormalised and the
rating was really a *three*-signal blend of news/momentum/filings.

It now comes from **Finnhub**'s free API (60 calls/min, no card), with the
Finviz scrape kept as a fallback. Analyst counts are collapsed onto the same
1..5 scale Finviz publishes, so nothing downstream changed.

Measured after the change: **124 of the 125 tracked tickers resolve (99.2%)** —
the one miss has no analyst coverage at all. A single cold-start fundamentals
cycle now fills `ticker_sentiment.analyst_recom` for **124 of 127 rows (97.6%)**
in 157s. Because `_record_signals` snapshots once per ticker per day,
`signal_history.comp_analysts` reaches that level on the next daily snapshot
rather than immediately.

Requests are paced under a 50/min cap (the free tier allows 60). A first
uncached pass costs ~2.5 min; a 24-hour per-ticker cache makes later cycles
near-free. Pacing is affordable because `_fetch_recom` is only called by this
6-hourly pass, not by the 120s news cycle.

Consequence for the numbers below: every accuracy figure in this file and in
`docs/PREDICTION_TRACK_RECORD.md` was produced by the three-signal model. They
are not a measurement of the documented four-signal design.

## ⏳ Remaining — needs YOUR input (can't get from the brief alone)
1. **Finviz Elite CSV** — professor provides the credentials.
   - Screener: https://finviz.com/screener.ashx → **Export** → save as `data/finviz_screener.csv` (the loader auto-picks it up).
2. **TD Ameritrade → now Schwab API** (TD's own API was retired after the Schwab merger).
   - Register a free app: https://developer.schwab.com/ → get API keys.
3. **Interactive Brokers API** — free account https://www.interactivebrokers.com/ + Client Portal Web API + free market-data setup.
   - Easier free alternative for real-time: **Alpaca** https://alpaca.markets/ (free IEX streaming).

**When you have any of these:** put the keys in `.env` (never in chat), tell me which one, and I'll wire it in.

## Accuracy roadmap (evidence-based, no overfitting)
Current: **~59% directional** (Buy 52% / Sell 62%), 7-day horizon. Realistic ceiling for free-data stock prediction is ~55–60% — anything >65% is a red flag for leakage/overfitting.
- ✅ **Phase 1a — done:** asymmetric thresholds (Buy 0.25 / Sell 0.12) → 47%→59%.
- ✅ **Phase 1b — done:** log the 4 signal components (news/momentum/analysts/filings) with every prediction (`SignalHistory.comp_*`). This is the foundation for real optimization.
- ⏳ **Phase 2 (weeks out):** once components + outcomes accumulate, train a LightGBM meta-learner + isotonic calibration, validated with **purged walk-forward CV** (avoids leakage). This is the legitimate path past 60%.
- 🔲 **Optional signals:** insider buying (SEC Form 4), options put/call ratio (yfinance), LLM event-type tagging — all free.

## Notes
- **Groq free tier** = 100k tokens/day, resets daily. When it's used up, the LLM sentiment layer falls back to FinBERT automatically (no breakage).
- **Real-time <1–2 min** is the **local live app** (`start.sh`); the Vercel link is the shareable snapshot (refreshes every ~2h).
- Predictions are **weekly-to-monthly** (long-term). Don't judge them on a single day — that's noise for this horizon.
