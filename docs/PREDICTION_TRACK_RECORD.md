# SentimentIQ — Prediction Track Record (Honest Report)

**Data source:** the system's own `signal_history` table. Every number below is
measured from logged predictions, not estimated.
**Reproduce it yourself:** `python scripts/measure_accuracy.py` — it prints this
section straight from the database.
**Last measured:** 2026-10-01.

---

## 1. Summary (read this first)

Over **3,097 graded directional calls**, the system is right **43.7%** of the
time on a 7-day horizon — **below a coin flip**. Sell calls do best at 51.4%;
Buy calls are the weak side at 40.0%. On a 30-day horizon it is worse: 37.9%.

That is the honest result, and it is worse than this report said in August, when
a much smaller sample (486 graded calls over about two weeks) showed 54%. The
larger sample did not confirm it. **The early number was small-sample noise.**

Two things to know when reading these figures:

1. They were produced by a **three-signal model, not the four-signal design.**
   The analyst component, documented at 25% of every rating, was populated in
   only 2% of predictions until it was fixed on **2026-09-20** (see
   `docs/HANDOFF.md` §7). Predictions logged after that date are the first ones
   made with all four signals; there are not yet enough graded ones to judge
   the fixed model.
2. **This is a sentiment-and-data blend, not a forecast.** The value of the
   project is the transparent, self-grading method — a tool that publishes how
   often it is wrong — not a claim of market-beating accuracy.

---

## 2. How predictions are measured

1. Each day, every tracked stock's **Buy / Sell / Hold** call is logged with the
   price at that moment.
2. **Seven days later** (and again at 30 days) the system grades it against the
   real price:
   - **Buy** correct if the stock rose **> +1%**
   - **Sell** correct if it fell **< −1%**
   - **Hold** correct if it stayed within **±3%**
3. "Directional accuracy" counts only **Buy/Sell** — the calls that commit to a
   direction. Hold is a "no big move" bet, not a directional claim.
4. A call is a **Buy** only above a blended score of **+0.25**, a **Sell** below
   **−0.12** (asymmetric because Sell has consistently been the stronger side).

Nothing is hand-picked: every logged prediction is graded, including the bad ones.

---

## 3. Results

**Window:** 2026-07-15 → 2026-10-02  ·  **6,021 snapshots** across **125 tickers**

### 7-day horizon — 4,897 graded

| Call | Count | Correct |
|---|---|---|
| BUY | 2,087 | 40.0% |
| SELL | 1,010 | 51.4% |
| HOLD | 1,800 | 46.9% |
| **Directional (Buy+Sell)** | **3,097** | **43.7%** |

### 30-day horizon — 2,233 graded

| Call | Count | Correct |
|---|---|---|
| BUY | 1,053 | 35.6% |
| SELL | 389 | 44.0% |
| HOLD | 791 | 39.6% |
| **Directional (Buy+Sell)** | **1,442** | **37.9%** |

### 7-day directional accuracy by month

| Month | Calls | Correct |
|---|---|---|
| 2026-07 | 374 | 49.5% |
| 2026-08 | 1,961 | 41.8% |
| 2026-09 | 762 | 45.7% |

The drop at the 30-day horizon is expected — a single week's news has less to do
with where a stock sits a month later.

### Does confidence track accuracy?

No, and this is a problem worth fixing. If the score carried real information,
higher-confidence calls would be more accurate. They are slightly *less*:

| Model confidence (abs score) | Calls | Correct |
|---|---|---|
| 0.10–0.25 | 697 | 46% |
| 0.25–0.40 | 1,130 | 45% |
| 0.40+ | 1,270 | 41% |

A good next task is to find out why — likely candidates are the momentum signal
dominating at the extremes, and the missing analyst input during this window.

---

## 4. Concrete examples

**Best calls**
- MRNA — Buy, then **+173.9%** (Aug 13)
- PLTR — Buy, then **+41.5%** (Aug 3)

**Worst misses (reported, not hidden)**
- MSTR — Sell, then **+37.1%** (Aug 19)
- CIFR — Buy, then **−33.2%** (Aug 3)
- ZS — Sell, then **+32.5%** (Sep 9)

A track record with no misses would be a warning sign, not a good sign.

---

## 5. Why these stocks are not another site's "top 5"

The dashboard surfaces stocks that **financial news is actively moving**. A
technical screener selects on price action and analyst ratings instead, so the
two lists will not match — by design. Accuracy is therefore measured as
*prediction vs. what the stock actually did*, never as *our list vs. someone
else's list*.

---

## 6. Limitations, stated plainly

- **Below a coin flip on direction (43.7%).** The system does not currently
  demonstrate predictive skill on Buy/Sell calls.
- **Buy is the weak side** (40.0%) and drags the average down; Sell is near
  even (51.4%).
- **Confidence does not predict correctness** — see §3.
- **Measured on a three-signal model.** The four-signal design has only been
  live since 2026-09-20 and has not been graded over a full window yet.
- **Free public data has a real ceiling.** ~55–60% would be a strong result
  here; anything above ~65% would suggest overfitting rather than skill.

---

## 7. Conclusion

The defensible claim is about **method, not performance**: a free, transparent
pipeline that logs every prediction, grades itself against reality, and
publishes the result even when the result is bad. Right now the result is bad —
43.7% directional, below chance — and the most valuable next piece of work is
to re-measure once the fixed four-signal model has enough graded history, and to
find out why higher confidence is not producing higher accuracy.

---
*Every figure here comes from `data/sentiment.db` and can be regenerated with
`python scripts/measure_accuracy.py`.*
