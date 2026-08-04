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
