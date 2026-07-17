# Long-Term Fundamentals Engine — Architecture Plan

> Goal: extend SentimentIQ from fast **news sentiment** (48-hour reaction signal)
> to slow **fundamental sentiment** for **long-term investing**, by reading each
> company's official SEC filings — earnings (10-Q), annual reports (10-K), and
> contracts (8-K material agreements) — over a rolling **7-day window**.
>
> Constraint: **100% free.** Everything below uses the official SEC EDGAR APIs
> (no key, no cost) plus the Groq/FinBERT stack we already run.

---

## 1. Why this fits the project

The professor's brief asks for sentiment-ranked tickers from reputable sources
including **SEC**. Right now we only scrape SEC's *news* page. SEC EDGAR actually
exposes the **full primary documents** for free, which is exactly what long-term
investors read. This turns "headline mood" into "what the company itself filed."

Two complementary signals on the dashboard:

| Signal | Window | Source | Use |
|---|---|---|---|
| **News sentiment** (have it) | ~48 h | RSS, social, wires | Fast reaction / trading mood |
| **Fundamental sentiment** (new) | ~7 days | SEC filings | Slow, long-term conviction |

---

## 2. Free data sources (SEC EDGAR — no key required)

All endpoints require a descriptive `User-Agent` header (SEC policy) and allow
up to ~10 requests/sec. We will stay far below that.

1. **Ticker → CIK map** (once per day, cached):
   `https://www.sec.gov/files/company_tickers.json`
   Maps `AAPL → 0000320193`. ~10k companies, one small file.

2. **Company filing history** (per ticker):
   `https://data.sec.gov/submissions/CIK##########.json`
   Returns recent filings with form type (10-K, 10-Q, 8-K), filing date,
   accession number, and primary document name.

3. **The filing document itself**:
   `https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{primary_doc}`
   The actual HTML/text of the 10-K, 10-Q, or 8-K.

4. **Full-text search (optional, for discovery)**:
   `https://efts.sec.gov/LATEST/search-index?q=...&forms=8-K`

---

## 3. What we extract from each document type

Filings are large (a 10-K can be 300+ pages), so we extract **only the
high-signal sections** rather than scoring the whole thing.

### Earnings — 10-Q (quarterly) + 8-K Exhibit 99.1 (earnings press release)
- Management's Discussion & Analysis (MD&A) — narrative on results & outlook.
- Revenue / net income direction vs. prior period (from the press release text).
- Forward **guidance** language ("we expect", "raised", "lowered").

### Annual report — 10-K
- **Item 1A Risk Factors** — new or worsening risks (bearish weight).
- **Item 7 MD&A** — full-year outlook and trends.
- Business outlook / strategy language.

### Contracts — 8-K
- **Item 1.01** "Entry into a Material Definitive Agreement".
- **EX-10** material-contract exhibits (supply deals, partnerships).
- M&A items (Item 2.01) when present.

A small per-form **section extractor** (regex/anchor on item headings) pulls
these sections; we cap each to a few thousand characters before scoring.

---

## 4. Scoring → long-term signal

For each extracted section:

1. **FinBERT** sentiment score (reuse existing scorer) → numeric −1..+1.
2. **Groq LLaMA 3.1** (free tier) summarizes the filing into:
   - a one-paragraph plain-English **long-term read**, and
   - a verdict label: `Improving` / `Stable` / `Deteriorating`.
3. A **fundamental score** per filing =
   `finbert_sentiment × form_weight × recency_weight`
   - `form_weight`: 10-K = 1.0, 10-Q = 0.9, 8-K contract = 0.8 (tunable).
   - `recency_weight`: linear decay across the 7-day window.

**Per-ticker fundamental score** = weighted average of its filings in the
trailing 7 days. Stored separately from the news-based ticker score.

---

## 5. Data model changes (additive, no data loss)

Reuse the existing additive-migration pattern (`ALTER TABLE ADD COLUMN`).

New table **`Filing`**:
```
id, cik, ticker, form_type (10-K|10-Q|8-K), filed_at, accession,
url, section_kind (earnings|annual|contract),
finbert_score, fundamental_score,
llm_summary (text), llm_verdict (Improving|Stable|Deteriorating),
fetched_at
```
Extend **`TickerSentiment`** with:
```
fundamental_score   (7-day weighted filing sentiment)
fundamental_verdict (rolled-up label)
filing_count_7d
last_filing_at
```

---

## 6. Pipeline integration

A **separate, slower scheduler** — filings don't change every minute:

- Run the **filings cycle every ~6 hours** (independent of the 60-second news
  cycle). EDGAR updates a few times a day; 6 h is plenty and very polite.
- Cycle steps:
  1. Refresh ticker→CIK map (daily).
  2. For each tracked ticker, fetch submissions JSON, find filings filed in the
     last 7 days with form ∈ {10-K, 10-Q, 8-K}.
  3. Skip accessions already in the `Filing` table (dedup by accession).
  4. Download new filings, extract sections, FinBERT-score, Groq-summarize.
  5. Recompute each ticker's `fundamental_score` over the 7-day window.

Politeness/robustness: descriptive User-Agent, ≤5 req/sec, retry/backoff,
graceful skip on any fetch error (same resilience as current collectors).

---

## 7. Dashboard surface

New top-nav tab **"Fundamentals"** (separate from the fast news feed):

- **Ranked table**: tickers sorted by 7-day fundamental score, with verdict
  chip (Improving/Stable/Deteriorating), filing count, last filing date.
- **Per-ticker deep-dive modal**: list of recent filings (form badge, date,
  link to the actual SEC document), each with its FinBERT score and the Groq
  plain-English summary.
- The existing **Sentiment Buddy** chatbot gains context: it can answer
  "what did NVDA's latest 10-Q say?" from the stored summaries.

Clear labeling: this is **educational analysis, not investment advice.**

---

## 8. Build phases (each independently testable)

- **Phase A — EDGAR collector**: ticker→CIK map + submissions fetch + filing
  download + dedup. Output: new filings landing in the `Filing` table.
- **Phase B — Section extractor**: pull MD&A / risk factors / Item 1.01 / 99.1
  per form type; cap length.
- **Phase C — Scoring + summaries**: FinBERT score + Groq summary + verdict;
  compute per-filing fundamental score.
- **Phase D — Weekly aggregation**: roll filings into per-ticker 7-day score on
  `TickerSentiment`; wire the 6-hour scheduler.
- **Phase E — Dashboard tab + modal**: Fundamentals tab, deep-dive, chatbot
  context.

---

## 9. Risks & mitigations

- **Large documents** → extract only key sections, cap characters before scoring.
- **EDGAR rate/politeness** → 6-hour cadence, User-Agent, backoff, dedup.
- **Groq free-tier limits** → summarize only *new* filings (a handful/day), not
  every cycle; cache summaries in the DB.
- **FinBERT trained on short text** → use it on extracted sections/sentences,
  and lean on Groq for the long-form narrative verdict.
- **Ticker→CIK gaps** (ETFs, foreign listings) → skip cleanly when no CIK.

---

## 10. Cost summary

| Component | Cost |
|---|---|
| SEC EDGAR APIs & documents | **Free** (official, no key) |
| FinBERT / VADER | **Free** (local) |
| Groq LLaMA 3.1 summaries | **Free tier** (few filings/day, well within limits) |
| Storage (SQLite) | **Free** |

**Total ongoing cost: $0.**
