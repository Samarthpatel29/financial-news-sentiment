# What makes SentimentIQ different — a defensible novelty case

> For the IST 495 report / defense. The goal is to show the contribution is **methodological and architectural**, not a claim of higher accuracy (directional stock prediction has a hard ~55–60% ceiling on free public data — see `PROJECT_STATUS.md`). The novelty is *how* the system is built and *what it combines*, delivered as a free, open, honest tool.

## 1. The landscape today (what already exists)

Existing stock-rating/sentiment tools fall into three buckets, and **none** occupies the space this project does:

| Category | Examples | What they do | Limitation |
|---|---|---|---|
| **Commercial AI raters** | Danelfin, Kavout | Multi-signal ML scores (Danelfin: 10,000+ features → AI Score 1–10; Kavout: "Kai Score") | **Paid, proprietary, closed-source.** You cannot see the model, the data, or the code, and cannot reproduce or audit it. "Transparency" = a few sub-scores only. |
| **Free single-signal tools** | StockTwits, generic FinBERT demos, Google-News sentiment scripts | One signal — social chatter *or* raw headline sentiment | No fusion of news + fundamentals + price + analysts; no LLM nuance; no agentic pipeline; no self-validation |
| **Academic frameworks** | FinBERT+Gemini hybrids, multi-agent LLM sentiment papers (2024–2026) | Exactly the hybrid/agentic methods this project uses | They are **papers and prototypes**, not free, runnable, real-time tools with a live dashboard |

**The gap:** there is no *free, open, reproducible* tool that fuses multiple signals with modern agentic/LLM methods **and** reports its own accuracy honestly. That gap is the contribution.

## 2. The differentiation matrix

| Capability | **This project** | Danelfin | Kavout | Finviz | StockTwits | Free FinBERT tools |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| Free to use | ✅ | ❌ paid | ❌ paid | ⚠️ Elite paid | ✅ | ✅ |
| Open-source / auditable code | ✅ | ❌ | ❌ | ❌ | ❌ | ⚠️ some |
| **Full** transparency (model + weights + *raw inputs*) | ✅ | ⚠️ sub-scores only | ❌ | ❌ | ❌ | ❌ |
| Multi-signal fusion (news + SEC + price + analysts + social) | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| LLM-augmented nuance (beyond classic ML/FinBERT) | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Agentic** architecture (CrewAI autonomous agents) | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Honest self-validation (discloses own accuracy + external cross-check) | ✅ | ❌ markets "win rate" | ❌ | ❌ | ❌ | ❌ |
| Real-time (< 1–2 min) live dashboard | ✅ | ⚠️ daily | ⚠️ daily | ✅ | ✅ | ❌ |
| Fully reproducible by anyone | ✅ | ❌ | ❌ | ❌ | ❌ | ⚠️ |

**No other column is all-✅.** The paid raters are closed; the free tools are single-signal and non-agentic. This project is the only one at the intersection.

## 3. The five concrete differentiators (defense talking points)

1. **Open & transparent by construction, not by marketing.** Danelfin advertises "no black box" but you still can't see its model, features, or data. Here, every rating exposes its four component scores **and** the raw headlines, SEC filings, price stats, and analyst numbers that produced it — and the code is fully readable. A user (or professor) can trace any Buy/Sell/Hold to its exact inputs.

2. **Agentic AI, on-brief.** The internship theme is literally *"from generative AI to agentic AI."* The pipeline is built as autonomous agents (CrewAI) that collect → deduplicate → score → aggregate → narrate, using a free LLM (Groq) as a reasoning layer. Commercial tools use classic ML pipelines, not agentic LLM orchestration.

3. **Hybrid FinBERT + LLM nuance layer.** FinBERT classifies sentence sentiment but misses context ("cuts costs" = bullish vs "cuts guidance" = bearish). A free LLM judge resolves that nuance, with FinBERT/VADER as an offline fallback. This FinBERT+LLM fusion is validated in 2024–2026 research but absent from free tools.

4. **Intellectual honesty as a feature.** The system logs every prediction and grades it against realized prices on weekly *and* monthly horizons, discloses its real accuracy (~59%), cross-checks each call against an independent source (Finviz analyst consensus), and states the overfitting ceiling openly. Commercial tools advertise "70% win rates" with no auditable methodology; this project publishes its limits.

5. **Free, reproducible, multi-source fusion.** It fuses reputable news wires (Reuters, Dow Jones, PR/GlobeNewswire, CNBC…), SEC/EDGAR filings, StockTwits ("tweets"), price history, and analyst consensus — with zero paid APIs and zero paid data. Anyone can clone and run the entire stack.

## 4. Honest scope (what is *not* claimed — this strengthens the case)

- **Not** claiming a novel algorithm — FinBERT, LLM sentiment, and multi-signal fusion each exist individually.
- **Not** claiming higher accuracy than paid tools — directional prediction is capped near 55–60% on free data; that's disclosed, not hidden.
- **The novelty is the *system*:** the specific fusion + agentic/LLM architecture + full transparency + honest validation, delivered as one free, open, reproducible, real-time tool. That combination does not exist elsewhere.

## 5. One-line thesis (for the abstract)
> *SentimentIQ is, to our knowledge, the first free and fully open agentic system to fuse financial-news sentiment (FinBERT + LLM), SEC-filing trajectory, price momentum, analyst consensus, and social chatter into an explainable, self-validating stock-rating dashboard — occupying a niche that commercial tools fill only behind a paywall and a black box.*

---
*Sources: Danelfin & Kavout methodology/pricing reviews (2026); FinBERT+LLM hybrid and multi-agent sentiment research (arXiv/IEEE, 2024–2026).*
