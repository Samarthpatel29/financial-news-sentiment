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
from __future__ import annotations
import datetime
import logging
import math
from collections import Counter
from typing import List

from config.settings import (
    SOURCE_TRUST, DEFAULT_TRUST_WEIGHT, TIME_DECAY_HALFLIFE_HOURS
)
from src.collectors.rss_collector import RawArticle
from src.storage.models import SentimentResult
from src.sentiment.ticker_extractor import extract_tickers, tickers_to_str
from .finbert import FinBERTScorer
from .vader import score as vader_score
from .llm_judge import score_headlines

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
