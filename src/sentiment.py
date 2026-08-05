"""sentiment — merged from 5 modules for a simpler layout."""
from __future__ import annotations

# ======================================================================
# from sentiment/finbert.py
# ======================================================================

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


# ======================================================================
# from sentiment/vader.py
# ======================================================================

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


# ======================================================================
# from sentiment/ticker_extractor.py
# ======================================================================

"""
Ticker extractor: finds stock ticker symbols mentioned in article text.

Three pass strategy (ordered by precision):
  1. $TICKER  — explicit dollar-prefix (highest precision)
  2. ALL-CAPS words filtered against TICKER_UNIVERSE (medium precision)
  3. Company name substring match from COMPANY_TO_TICKER (catches "Apple", "Tesla" etc.)
"""
import re
import logging
from typing import Sequence

from config.tickers import (
    TICKER_UNIVERSE, COMPANY_TO_TICKER, AMBIGUOUS_NAMES, _STOPWORDS
)

log = logging.getLogger(__name__)

# Pre-compiled patterns
_DOLLAR_PAT  = re.compile(r'\$([A-Z]{1,5}(?:\.[A-B])?)')  # $AAPL, $BRK.B
_ALLCAPS_PAT = re.compile(r'\b([A-Z]{2,5})\b')            # standalone ALL-CAPS


def _name_pattern(name: str) -> re.Pattern:
    """
    Word-boundary matcher for a company name.

    Plain `name in text` matches inside unrelated words — "meta" inside
    "Rheinmetall", "ups" inside "groups", "intel" inside "intelligence",
    "unity" inside "opportunity". Anchoring with \\b removes those.
    \\b is only useful next to a word character, so it is added conditionally
    (a name like "at&t" ends in one, "s&p 500" does not start with a symbol).
    """
    left  = r'\b' if name[:1].isalnum() else ''
    right = r'\b' if name[-1:].isalnum() else ''
    return re.compile(left + re.escape(name) + right)


# (name, lowercase pattern, ticker, needs_capital), built once at import
_COMPANY_PATTERNS: list[tuple[str, re.Pattern, str, bool]] = [
    (name, _name_pattern(name), ticker, name in AMBIGUOUS_NAMES)
    for name, ticker in COMPANY_TO_TICKER.items()
]


def _company_matches(text: str, text_lower: str):
    """Yield tickers whose company name appears as a whole word in *text*."""
    for name, pat, ticker, needs_capital in _COMPANY_PATTERNS:
        # cheap substring prefilter first, then the authoritative boundary check
        if name not in text_lower:
            continue
        match = pat.search(text_lower)
        if not match:
            continue
        # Ambiguous aliases must be capitalised in the original headline to
        # count — "Snap beat estimates" yes, "a snap decision" no. Compare the
        # matched span back against the original-case text.
        if needs_capital:
            spans = [m.start() for m in pat.finditer(text_lower)]
            if not any(text[i:i + 1].isupper() for i in spans):
                continue
        yield ticker


def extract_tickers(text: str, *, max_tickers: int = 10) -> list[str]:
    """
    Return a sorted list of unique ticker symbols found in *text*.
    Limited to *max_tickers* to avoid noise from very long articles.
    """
    if not text:
        return []

    # Track which pass found each symbol so truncation can keep the most
    # reliable ones: $-prefix (1) > company name (2) > bare ALL-CAPS (3).
    precision: dict[str, int] = {}

    def _add(sym: str, rank: int) -> None:
        if rank < precision.get(sym, 99):
            precision[sym] = rank

    # ── Pass 1: $TICKER ────────────────────────────────────────────────────────
    # No stopword filter here: an explicit cashtag is unambiguous intent, and
    # several real symbols are also common words ($ON, $OPEN, $NOW, $ALL).
    for m in _DOLLAR_PAT.finditer(text):
        sym = m.group(1)
        if sym in TICKER_UNIVERSE:
            _add(sym, 1)

    # ── Pass 2: Company names (whole word only) ───────────────────────────────
    text_lower = text.lower()
    for ticker in _company_matches(text, text_lower):
        _add(ticker, 2)

    # ── Pass 3: ALL-CAPS words ─────────────────────────────────────────────────
    for m in _ALLCAPS_PAT.finditer(text):
        sym = m.group(1)
        if sym in TICKER_UNIVERSE and sym not in _STOPWORDS:
            _add(sym, 3)

    if len(precision) > max_tickers:
        # Drop the least reliable matches first, then break ties alphabetically
        keep = sorted(precision, key=lambda s: (precision[s], s))[:max_tickers]
        return sorted(keep)

    return sorted(precision)


def extract_primary_ticker(text: str) -> str | None:
    """
    Return the single most prominent ticker, or None.
    Priority: $-prefix > company name > all-caps.
    """
    if not text:
        return None

    # $-prefix first (explicit cashtag — no stopword filter, see extract_tickers)
    for m in _DOLLAR_PAT.finditer(text):
        sym = m.group(1)
        if sym in TICKER_UNIVERSE:
            return sym

    # Company name (whole word only)
    text_lower = text.lower()
    for ticker in _company_matches(text, text_lower):
        return ticker

    # All-caps fallback
    for m in _ALLCAPS_PAT.finditer(text):
        sym = m.group(1)
        if sym in TICKER_UNIVERSE and sym not in _STOPWORDS:
            return sym

    return None


def tickers_to_str(tickers: Sequence[str]) -> str:
    """Serialize ticker list to comma-separated string for DB storage."""
    return ",".join(tickers) if tickers else ""


def str_to_tickers(s: str | None) -> list[str]:
    """Deserialize comma-separated ticker string from DB."""
    if not s:
        return []
    return [t.strip() for t in s.split(",") if t.strip()]


# ======================================================================
# from sentiment/llm_judge.py
# ======================================================================

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


# ======================================================================
# from sentiment/scorer.py
# ======================================================================

"""
SentimentScorer: FinBERT primary, VADER fallback.

Rank formula (v3):
    rank_score = |sentiment_score| × message_density × trust_weight

Time decay is NOT stored in rank_score — it is applied at read time by the
dashboard and the aggregator, so a score keeps decaying as the article ages
instead of being frozen at the moment it was scored. `time_weight` is still
recorded on the row for reference/analysis.

sentiment_score = FinBERT P(positive) - P(negative), continuous in [-1, 1]
                  (VADER compound when FinBERT is below FINBERT_MIN_CONF)

message_density = this source's share of the window, normalised to (0, 1] so
                  rank_score stays bounded and comparable across cycles

trust_weight = 1.0  for Tier-1 sources (Reuters, Dow Jones, SEC, FDA)
             = 0.75 for everything else

time_weight  = exp( -ln(2) / HALFLIFE_HOURS × hours_old )
               → article published NOW gets 1.0
               → 24-h-old article gets 0.5 (with default 24-h half-life)
               (recorded on the row; applied live by readers, not baked into
                rank_score — see above)
"""
import datetime
import logging
import math
from collections import Counter
from typing import List

from config.settings import (
    SOURCE_TRUST, DEFAULT_TRUST_WEIGHT, TIME_DECAY_HALFLIFE_HOURS
)
from src.collectors import RawArticle
from src.storage import SentimentResult
vader_score = score

log = logging.getLogger(__name__)

_LN2 = math.log(2)


def _trust_weight(source: str) -> float:
    return SOURCE_TRUST.get(source, DEFAULT_TRUST_WEIGHT)


def _time_weight(published: datetime.datetime | None) -> float:
    """Exponential decay based on article age."""
    if published is None:
        return 1.0
    now = datetime.datetime.utcnow()
    # Ensure naive comparison
    if published.tzinfo is not None:
        published = published.replace(tzinfo=None)
    hours_old = max(0.0, (now - published).total_seconds() / 3600)
    return math.exp(-_LN2 / TIME_DECAY_HALFLIFE_HOURS * hours_old)


class SentimentScorer:
    """
    FinBERT primary; VADER fallback when FinBERT confidence < FINBERT_MIN_CONF.
    rank_score = |sentiment_score| × density × trust_weight × time_weight
    """

    def __init__(self):
        self._finbert = FinBERTScorer()

    def score_articles(
        self,
        articles: List[RawArticle],
        window_articles: List[RawArticle] | None = None,
    ) -> List[SentimentResult]:
        if not articles:
            return []

        # Message density = share of the window contributed by this article's
        # source, normalised to (0, 1]. The raw count was unusable as a rank
        # factor: a source with 50k rows (StockTwits) gave every one of its
        # posts a 10-50x multiplier over a CNBC story, so rank_score measured
        # how chatty a source is rather than how important the news is. It was
        # also unbounded, making scores incomparable across cycles.
        all_articles = window_articles or articles
        source_counts = Counter(a.source for a in all_articles)
        max_count = max(source_counts.values()) if source_counts else 1

        texts = [f"{a.title}. {a.body}"[:512] for a in articles]
        finbert_results = self._finbert.score_batch(texts)

        # Free-tier Groq LLM judge over the headlines — reads nuance FinBERT
        # misses ("cuts costs" bullish vs "cuts guidance" bearish). Returns
        # None per item when Groq is unavailable, so FinBERT/VADER still drive
        # the fully-offline path. Attribution is title-based, so judge titles.
        llm_results = score_headlines([a.title for a in articles])

        results: List[SentimentResult] = []
        for article, fb, llm, text in zip(articles, finbert_results, llm_results, texts):
            density = source_counts[article.source] / max_count
            tw      = _trust_weight(article.source)
            dw      = _time_weight(article.published)

            # VADER is computed once for every article: it is stored as an
            # independent second opinion, and reused as the fallback below.
            vader_compound = vader_score(text).compound

            # FinBERT fields are always recorded for reference/display when the
            # model was confident, regardless of which engine drives the score.
            if fb is not None:
                finbert_label = fb.label
                finbert_score = fb.score
                finbert_conf  = fb.confidence
            else:
                finbert_label = finbert_score = finbert_conf = None

            # Sentiment priority: LLM judge (nuance) → FinBERT → VADER. Sign is
            # preserved; all three use the same [-1, 1] convention.
            if llm is not None:
                sentiment_score = llm["score"]
                if finbert_label is None:
                    finbert_label = llm["label"]   # give the UI a label to show
            elif fb is not None:
                # fb.score is already continuous polarity in [-1, 1]; it carries
                # the model's confidence in its magnitude, so multiplying by
                # fb.confidence again would double-count it.
                sentiment_score = fb.score
            else:
                # VADER fallback (FinBERT below FINBERT_MIN_CONF or errored)
                sentiment_score = vader_compound
                log.debug("VADER fallback for: %s", article.title[:60])

            # rank_score is the *undecayed* base. Time decay is applied when the
            # score is read (dashboard `_ranked_rows`, aggregator
            # `_live_time_weight`) so it keeps decaying as the article ages.
            # Baking dw in here meant the readers multiplied by decay a second
            # time — an article scored 12h after publication was decayed twice.
            rank_score = abs(sentiment_score) * density * tw

            # Extract tickers from title (fast, no body needed)
            tickers = extract_tickers(article.title)
            tickers_str = tickers_to_str(tickers)

            results.append(SentimentResult(
                article_id      = 0,
                source          = article.source,
                title           = article.title,
                url             = article.url,
                published       = article.published,
                finbert_label   = finbert_label,
                finbert_score   = finbert_score,
                finbert_conf    = finbert_conf,
                vader_compound  = vader_compound,
                sentiment_score = sentiment_score,
                message_density = density,
                trust_weight    = tw,
                time_weight     = dw,
                rank_score      = rank_score,
                tickers         = tickers_str,
                image_url       = getattr(article, "image_url", "") or "",
                scored_at       = datetime.datetime.utcnow(),
            ))
        return results

