from __future__ import annotations
from dataclasses import dataclass

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_analyzer = SentimentIntensityAnalyzer()


@dataclass
class VADERResult:
    compound: float   # [-1, 1]
    label:    str     # positive / negative / neutral


def score(text: str) -> VADERResult:
    scores  = _analyzer.polarity_scores(text)
    compound = scores["compound"]
    if compound >= 0.05:
        label = "positive"
    elif compound <= -0.05:
        label = "negative"
    else:
        label = "neutral"
    return VADERResult(compound=compound, label=label)
