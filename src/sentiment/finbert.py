from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import List

from config.settings import FINBERT_MODEL, SENTIMENT_BATCH, FINBERT_MIN_CONF

log = logging.getLogger(__name__)


@dataclass
class FinBERTResult:
    label:      str    # positive | negative | neutral
    score:      float  # continuous polarity: P(positive) - P(negative), in [-1, 1]
    confidence: float  # raw softmax probability of the winning class


class FinBERTScorer:
    """Lazy-loaded FinBERT inference with batching."""

    _pipeline = None

    @classmethod
    def _load(cls):
        if cls._pipeline is None:
            from transformers import pipeline
            log.info("Loading FinBERT model %s …", FINBERT_MODEL)
            cls._pipeline = pipeline(
                "text-classification",
                model=FINBERT_MODEL,
                tokenizer=FINBERT_MODEL,
                top_k=None,          # return all three class scores
                truncation=True,
                max_length=512,
            )
            log.info("FinBERT loaded.")
        return cls._pipeline

    def score_batch(self, texts: List[str]) -> List[FinBERTResult | None]:
        """Score a batch; returns None for items that fall below confidence threshold."""
        pipe = self._load()
        results: List[FinBERTResult | None] = []
        for i in range(0, len(texts), SENTIMENT_BATCH):
            batch = texts[i : i + SENTIMENT_BATCH]
            try:
                outputs = pipe(batch)
            except Exception as exc:
                log.warning("FinBERT batch error: %s", exc)
                results.extend([None] * len(batch))
                continue
            for item_scores in outputs:
                best = max(item_scores, key=lambda x: x["score"])
                if best["score"] < FINBERT_MIN_CONF:
                    results.append(None)
                    continue
                # Continuous polarity rather than a hard +1/-1/0 label.
                # The label-map version collapsed every neutral article to
                # exactly 0.0, which zeroed its rank_score AND its aggregation
                # weight — silently discarding ~80% of the corpus and letting a
                # small confident minority set each ticker's composite.
                probs = {s["label"].lower(): s["score"] for s in item_scores}
                polarity = probs.get("positive", 0.0) - probs.get("negative", 0.0)
                results.append(FinBERTResult(
                    label=best["label"].lower(),
                    score=polarity,
                    confidence=best["score"],
                ))
        return results

    def score(self, text: str) -> FinBERTResult | None:
        return self.score_batch([text])[0]
