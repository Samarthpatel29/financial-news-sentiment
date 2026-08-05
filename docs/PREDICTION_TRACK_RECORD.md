# SentimentIQ — Prediction Track Record (Honest Report)

**Prepared for:** IST 495 review
**Data source:** the system's own `signal_history` database — every figure below is measured from logged predictions, not estimated or claimed.
**Reporting window:** 2026-07-15 → 2026-07-29 (predictions graded on a 7-day horizon; the most recent gradings run to early August).

---

## 1. Summary (read this first)

Over **486 graded predictions**, the system is **right about 54% of the time on its directional (Buy/Sell) calls** — a real but modest edge over a 50% coin flip. That edge is driven almost entirely by its **Sell calls, which are correct ~61% of the time**; its Buy calls are near chance (~46%). Accuracy varies week to week and is not yet trending upward.

This report presents the full record honestly, including the weak spots, because the value of the project is a **transparent, self-validating method** — not a claim of market-beating accuracy, which is not realistic on free public data (the credible ceiling is ~55–60%; anything above ~65% would signal overfitting).

---

## 2. How predictions are measured

The measurement is deliberately transparent and reproducible:

1. Each day, every tracked stock's **Buy / Sell / Hold** prediction is logged together with the price at that moment.
2. **Seven days later**, the system grades the prediction against the stock's *actual* price:
   - **Buy** = correct if the stock rose **> +1%**
   - **Sell** = correct if it fell **< −1%**
   - **Hold** = correct if it stayed within **±3%**
3. "Directional accuracy" counts only **Buy/Sell** calls — the cases where the model commits to a direction. (Hold is a "no big move" bet, not a directional claim.)
4. A prediction is a **Buy** only when the blended score exceeds **+0.25**, a **Sell** below **−0.12** (an asymmetric threshold chosen because Sell calls proved more reliable than Buy calls).

All predictions are timestamped and stored, so any figure here can be re-derived from the database.

---

## 3. All-time results

**Scale:** 1,081 prediction snapshots logged across 93 tickers; 486 have aged enough to be graded on the 7-day horizon.

### Overall
| Metric | Result |
|---|---|
| Directional accuracy (Buy + Sell) | **54.1%** (98 of 181) |
| Coin-flip baseline | 50% |
| Realistic ceiling (free-data prediction) | ~55–60% |

### By call type — where the accuracy comes from
| Call | Count | Accuracy |
|---|---|---|
| **Sell** | 102 | **61%** ← the system's genuine strength |
| **Buy** | 79 | 46% ← near chance; the weaker side |

The Sell edge is the honest headline: the system is meaningfully good at flagging weakness. The Buy side does not yet beat chance and is the clearest area for improvement.

### Consistency over time (the honest part)
Accuracy is **variable, not yet consistent**, and the most recent week is the weakest:

| Week beginning | Directional accuracy |
|---|---|
| 2026-07-13 | 76% |
| 2026-07-20 | 49% |
| 2026-07-27 | 37% |

Day-to-day it swings widely (e.g., 89% on Jul 17, 35% on Jul 26). With only a few weeks of graded data and small daily samples, this variance is expected — but it means **we cannot yet claim a stable or improving trend.** More data is needed before consistency can be demonstrated.

### Does confidence track accuracy?
Partly — a positive but imperfect signal that the model captures something real:

| Model confidence (\|score\|) | Predictions | Accuracy |
|---|---|---|
| 0.00–0.10 (weak) | 200 | 40% |
| 0.10–0.25 (moderate) | 181 | 41% |
| 0.25–0.40 (strong) | 65 | 54% |
| 0.40+ (very strong) | 40 | 48% |

Accuracy rises from ~40% at low confidence to ~54% at high confidence — evidence the score carries information — though the very-highest bucket dips, so the relationship is not perfectly clean.

---

## 4. Concrete evidence

**Correct Sell calls (the strength):**
- FUBO — Sell, then **−18.9%** (Jul 16)
- HOOD — Sell, then **−18.9%** (Jul 22)
- COIN — Sell, then **−18.0%** (Jul 22)
- RIVN — Sell, then **−13.6%** (Jul 22)

**Correct Buy calls:**
- AMC — Buy, then **+24.2%** (Jul 26)
- DELL — Buy, then **+21.4%** (Jul 29)
- INTC — Buy, then **+16.9%** (Jul 29)

**Honest misses (reported, not hidden):**
- OPEN — Buy, then **−19.2%** (Jul 16)
- AMZN — Sell, then **+17.0%** (Jul 26)
- ORCL — Sell, then **+12.9%** (Jul 26)

Roughly **46% of directional calls were wrong** — expected for a ~54% system, and disclosed here on purpose. A track record with no misses would be a warning sign of overfitting or selective reporting.

---

## 5. Why our stocks are not the daily "top 5" on other platforms

This is a design property, not a flaw, and it is important for interpreting the results.

- **Our universe is news-driven.** The dashboard surfaces the stocks that *financial news is actively moving right now*. A technical screener (e.g., Finviz's top-5) selects by different criteria (price action, analyst ratings). Two different selection methods naturally produce different lists.
- **So our list intentionally will not mirror another tool's list.** If it had to, the project would simply be a worse copy of that tool. Being news-driven is the entire premise.
- **Accuracy is therefore measured correctly as *prediction vs. reality*** — did our call on a stock match how that stock actually moved — **not as *our list vs. someone else's list*.** A stock we flag because of the news is a correct call if it then moves the way we predicted, regardless of whether it appears on any other platform's ranking that day.

---

## 6. Limitations (stated plainly)

- **Small, early sample.** ~2 weeks of graded predictions. The numbers will move as more data accumulates.
- **High variance / no upward trend yet.** Week-to-week accuracy ranges 37–76%; consistency is not yet established.
- **Buy side underperforms** (46%) and needs the most work; the edge today is on the Sell side.
- **Monthly (30-day) horizon not yet available** — it requires predictions 30+ days old and is still accumulating. For a long-term tool this will be the most meaningful measure.
- **Ceiling is real.** ~54% is within the honest range for free-data stock-direction prediction; large gains beyond ~60% are not realistically attainable without paid data or overfitting.

---

## 7. Conclusion

The honest, defensible claim is this: **over ~500 logged, timestamped predictions, the system beats a coin flip (54% directional), with a genuine edge on Sell calls (61%), measured through a fully transparent, self-grading method.** It does **not** yet demonstrate consistency, and its Buy calls need improvement.

The contribution is the **method and its honesty** — a free, transparent, self-validating pipeline that reports its real accuracy (including misses and the realistic ceiling), rather than a claim of market-beating performance. That transparency is what makes the result credible.

---
*Every figure in this report is computed directly from `data/signal_history` and can be reproduced on request.*
