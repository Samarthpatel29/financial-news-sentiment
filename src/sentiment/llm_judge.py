"""
LLM sentiment judge — free-tier Groq nuance layer over financial headlines.

Why this exists
---------------
FinBERT is a strong *free* financial-sentiment model (~89% accuracy) but it
classifies single sentences and misses context that flips a headline's meaning:
"Acme cuts costs" (bullish) vs "Acme cuts guidance" (bearish), "beats but warns",
"misses on revenue, raises buyback", etc. A general LLM reads that nuance.

This module asks Groq's free tier (llama-3.3-70b-versatile — no credit card,
14,400 req/day, fast enough for the real-time board) to score a BATCH of
headlines in one call. It returns a continuous score in [-1, 1] per headline,
in the SAME convention as FinBERT's `score` (P(pos) - P(neg)), so callers can
use it as a drop-in replacement.

It NEVER raises and NEVER blocks the pipeline: with no key, a missing `groq`
package, a rate-limit, or any error, it returns `None` for the affected
headlines and the caller falls back to FinBERT/VADER (fully offline).
"""
from __future__ import annotations
import json
import logging

from config.settings import GROQ_API_KEY, NEWS_LLM_MODEL, USE_LLM_SENTIMENT

log = logging.getLogger(__name__)

# Batch size: one Groq call scores this many headlines. ~20 short headlines is
# well under the free-tier per-minute token budget and keeps latency low.
_BATCH = 20

_SYSTEM = (
    "You are a financial-news sentiment analyst. For each numbered headline, "
    "judge its impact on the mentioned company's stock for a LONG-TERM investor. "
    "Read context carefully: 'cuts costs' is bullish, 'cuts guidance' is bearish; "
    "'beats but warns' is roughly neutral. Output ONLY a JSON array, one object "
    "per headline, in the same order, each: "
    '{"i": <number>, "score": <float -1..1>, "label": "positive"|"negative"|"neutral"}. '
    "score = +1 very bullish, 0 neutral, -1 very bearish. No prose, no code fences."
)


def _client():
    """Return a Groq client, or None when unavailable (no key / package)."""
    if not USE_LLM_SENTIMENT:
        return None
    try:
        from groq import Groq
    except ImportError:
        return None
    if not GROQ_API_KEY or GROQ_API_KEY.startswith("PASTE_"):
        return None
    return Groq(api_key=GROQ_API_KEY)


def is_available() -> bool:
    return _client() is not None


# Strip the "groq/" prefix the CrewAI config uses — the raw SDK wants the bare id.
_MODEL = NEWS_LLM_MODEL.split("/", 1)[-1]


def _label_of(score: float) -> str:
    return "positive" if score > 0.05 else "negative" if score < -0.05 else "neutral"


def _score_batch(client, headlines: list[str]) -> list[dict | None]:
    """Score one batch. Returns per-headline dict or None on any failure."""
    numbered = "\n".join(f"{i}. {h}" for i, h in enumerate(headlines))
    try:
        resp = client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": numbered},
            ],
            temperature=0.0,
            max_tokens=1500,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content.strip()
        data = json.loads(raw)
        # The model may wrap the array in a key (json_object mode); unwrap it.
        if isinstance(data, dict):
            arr = next((v for v in data.values() if isinstance(v, list)), None)
        else:
            arr = data
        if not isinstance(arr, list):
            return [None] * len(headlines)

        out: list[dict | None] = [None] * len(headlines)
        for obj in arr:
            if not isinstance(obj, dict):
                continue
            idx = obj.get("i")
            if not isinstance(idx, int) or not (0 <= idx < len(headlines)):
                continue
            try:
                score = max(-1.0, min(1.0, float(obj.get("score"))))
            except (TypeError, ValueError):
                continue
            label = obj.get("label")
            if label not in ("positive", "negative", "neutral"):
                label = _label_of(score)
            out[idx] = {"score": round(score, 4), "label": label}
        return out
    except Exception as exc:  # noqa: BLE001 — never break the pipeline
        log.warning("LLM judge batch failed (%s); falling back to FinBERT", exc)
        return [None] * len(headlines)


def score_headlines(headlines: list[str]) -> list[dict | None]:
    """
    Score each headline in [-1, 1] (FinBERT `score` convention).

    Returns a list the same length as `headlines`; each element is
    {"score": float, "label": str} or None (caller should fall back).
    Fully graceful: returns all-None when Groq is unavailable.
    """
    if not headlines:
        return []
    client = _client()
    if client is None:
        return [None] * len(headlines)

    results: list[dict | None] = []
    for start in range(0, len(headlines), _BATCH):
        batch = headlines[start:start + _BATCH]
        results.extend(_score_batch(client, batch))
    return results
