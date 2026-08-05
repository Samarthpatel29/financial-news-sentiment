from __future__ import annotations
from dataclasses import dataclass

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# VADER ships a general-purpose social-media lexicon that misreads financial
# language: "beat" is scored as violence (-), "surges", "downgrade" and
# "all-time high" are absent entirely. "Earnings beat expectations, stock surges
# to all-time high" scored exactly 0.0 (neutral) before this table was added.
# Valences are on VADER's -4..+4 scale.
_FINANCE_LEXICON: dict[str, float] = {
    # bullish
    "beat": 2.0, "beats": 2.0, "outperform": 2.4, "outperformed": 2.4,
    "surge": 2.8, "surges": 2.8, "surged": 2.8, "soar": 3.0, "soars": 3.0,
    "soared": 3.0, "rally": 2.2, "rallies": 2.2, "rallied": 2.2,
    "jump": 1.8, "jumps": 1.8, "jumped": 1.8, "climb": 1.4, "climbs": 1.4,
    "gain": 1.6, "gains": 1.6, "gained": 1.6, "upgrade": 2.4,
    "upgraded": 2.4, "upgrades": 2.4, "bullish": 2.6, "record": 1.6,
    "profit": 1.8, "profits": 1.8, "profitable": 2.0, "growth": 1.6,
    "dividend": 1.2, "buyback": 1.6, "expansion": 1.4, "guidance": 0.4,
    "raised": 1.6, "raises": 1.6, "topped": 1.8, "tops": 1.8,
    "breakout": 2.0, "momentum": 1.2, "recovery": 1.6, "rebound": 1.8,
    # bearish
    "miss": -2.0, "misses": -2.0, "missed": -2.0, "plunge": -3.0,
    "plunges": -3.0, "plunged": -3.0, "slump": -2.4, "slumps": -2.4,
    "tumble": -2.6, "tumbles": -2.6, "tumbled": -2.6, "crash": -3.2,
    "crashes": -3.2, "crashed": -3.2, "slide": -1.8, "slides": -1.8,
    "sink": -2.2, "sinks": -2.2, "sank": -2.2, "drop": -1.6, "drops": -1.6,
    "dropped": -1.6, "fell": -1.6, "falls": -1.6, "decline": -1.8,
    "declines": -1.8, "downgrade": -2.6, "downgraded": -2.6,
    "downgrades": -2.6, "bearish": -2.6, "loss": -2.0, "losses": -2.0,
    "layoff": -2.4, "layoffs": -2.4, "bankruptcy": -3.4, "default": -2.8,
    "lawsuit": -2.0, "probe": -1.8, "investigation": -1.8, "recall": -2.0,
    "selloff": -2.4, "sell-off": -2.4, "warning": -2.0, "warns": -2.0,
    "cuts": -1.4, "slashed": -2.2, "halted": -2.0, "delisted": -3.0,
    "shortfall": -2.2, "writedown": -2.4, "restructuring": -1.2,
}

_analyzer = SentimentIntensityAnalyzer()
_analyzer.lexicon.update(_FINANCE_LEXICON)


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
