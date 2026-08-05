# Demo Walkthrough — SentimentIQ

A simple, step-by-step tour of the app. It works two ways:

1. **As a demo guide** — follow it to see everything the app does.
2. **As a recording script** — read it out while screen-recording to make the
   demo / technical MP4s (the code can't record video itself).

Start the app first: `python run.py`, then open `http://localhost:5001`.

---

## Part 1 — Demo (for a non-technical viewer)

**1. The top bar.**
Left to right: the app name, the **FSI** (Financial Sentiment Index — how bullish
this week's news is overall), the tabs, the market open/closed badge, and a live
clock. Point out that the numbers update on their own every minute.

**2. AI Signals (the home tab).**
This is the main screen. The middle column lists stocks, each with a **Buy**,
**Sell**, or **Hold** label and a **confidence %**. The left has a short AI market
summary and the chatbot. Explain the legend at the top: green = likely to
improve, amber = unclear, red = likely to weaken.

**3. Click a stock.**
The right panel fills in. It opens on the **Chart** tab — a candlestick chart of
the last 45 days, with the day's Open/High/Low/Close and long-term returns
underneath.

**4. Walk through that stock's tabs (left to right):**
- **Chart** — the price chart and returns.
- **Why this rating** — plain-English reasons, e.g. "news is negative, but
  analysts lean Buy and the stock has been trending up."
- **Reports** — the company's recent SEC filings with short AI summaries.
- **Breakdown** — the four things behind the rating: News (30%), Price momentum
  (30%), Analyst view (25%), SEC filings (15%), and the overall score.
- **Verify on Finviz** — pulls live analyst data from Finviz and says whether it
  **agrees**, is **mixed**, or **disagrees** with our rating. This is how you
  check the rating against an outside source.
- **Timeline** — the filings and news for that stock, newest first.

**5. Sector Map tab.**
Sectors coloured green/red by sentiment. Click a sector to see its stocks and the
headlines moving it.

**6. Watchlist tab.**
Click the star on any stock to save it. The Watchlist tab shows just your saved
stocks. It's stored in the browser, no login needed.

**7. News tab.**
The week's top headlines, each showing the stock(s) it's about. Click a stock tag
to jump to that stock's signal.

**8. Chatbot (left side).**
Ask it something like "why is JPM a hold?" It answers using the same numbers on
screen, in plain English. It explains — it does not tell you to buy or sell.

---

## Part 2 — Technical walkthrough (for an engineer)

**1. Where the data comes from.**
Every ~60 seconds `src/pipeline/crew.py` pulls news from free RSS feeds,
StockTwits, and Reddit. Every 6 hours `src/pipeline/fundamentals.py` pulls SEC
filings from EDGAR. All sources are free; no paid API.

**2. How news is scored.**
`src/sentiment/` — FinBERT (a finance-trained model) rates each headline from −1
to +1. VADER is the backup. `ticker_extractor.py` figures out which stock each
headline is about.

**3. How the Buy/Sell/Hold rating is built.**
`fundamentals.py :: _aggregate()` combines four signals per stock:
`0.30·news + 0.30·price_momentum + 0.25·analyst_consensus + 0.15·reports`.
Price comes from yfinance, analyst consensus from Finviz, reports from a Groq
verdict on the filings. Above +0.12 = Buy, below −0.12 = Sell, else Hold.
Show one stock's four component values in the Breakdown tab and point out they
add up to the rating.

**4. Storage.**
Everything is saved in a SQLite file (`data/sentiment.db`) through
`src/storage/models.py`. Migrations only add columns, never drop them.

**5. The web layer.**
`src/dashboard/app.py` is Flask. It reads the database and returns JSON
(`/api/fundamentals`, `/api/ranked`, `/api/verify/<ticker>`, etc.) and streams
live updates over SSE. The whole front-end is one file:
`src/dashboard/templates/index.html`.

**6. The Finviz check is live.**
`src/collectors/finviz_verify.py` fetches the Finviz quote page, reads the
analyst rating and performance, and compares them to our signal. Open the Verify
tab, then open the same ticker on finviz.com to show they match.

**7. Self-scoring.**
Each day the model's calls are saved and graded 7 days later against the real
price (`SignalHistory` table). The header shows the running accuracy.

**8. Public deployment.**
`export_static.py` runs the pipeline locally and writes JSON files to `./public`.
Vercel serves those (the ML stack is too big for Vercel). The chatbot and the
Finviz check stay live through two small serverless functions in `api/`.

---

## What to record for the two MP4s

- **Demo MP4 (~3–4 min):** follow Part 1. Screen-record the browser only.
- **Technical MP4 (~5–7 min):** follow Part 2. Show the terminal running
  `python run.py`, then the code files named above, then the running app.

Use the built-in macOS recorder: **⌘⇧5 → Record Selected Portion / Entire
Screen**. Save as `.mov`, or export to `.mp4` from QuickTime (File → Export).
