from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import List

from config.settings import FINBERT_MODEL, SENTIMENT_BATCH, FINBERT_MIN_CONF

log = logging.getLogger(__name__)


@dataclass
class FinBERTResult:
    label:      str    # positive | negative | neutral
    score:      float  # mapped: +1 / -1 / 0
    confidence: float  # raw softmax probability


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
                else:
                    label_map = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}
                    results.append(FinBERTResult(
                        label=best["label"].lower(),
                        score=label_map.get(best["label"].lower(), 0.0),
                        confidence=best["score"],
                    ))
        return results

    def score(self, text: str) -> FinBERTResult | None:
        return self.score_batch([text])[0]
