"""
SentimentScorer: FinBERT primary, VADER fallback.

Rank formula (v2):
    rank_score = |sentiment_score| × message_density × trust_weight × time_weight

trust_weight = 1.0  for Tier-1 sources (Reuters, Dow Jones, SEC, FDA)
             = 0.75 for everything else

time_weight  = exp( -ln(2) / HALFLIFE_HOURS × hours_old )
               → article published NOW gets 1.0
               → 24-h-old article gets 0.5 (with default 24-h half-life)
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

        # message density = count of articles per source in this collection window
        all_articles = window_articles or articles
        source_counts = Counter(a.source for a in all_articles)

        texts = [f"{a.title}. {a.body}"[:512] for a in articles]
        finbert_results = self._finbert.score_batch(texts)

        results: List[SentimentResult] = []
        for article, fb, text in zip(articles, finbert_results, texts):
            density = float(source_counts[article.source])
            tw      = _trust_weight(article.source)
            dw      = _time_weight(article.published)

            if fb is not None:
                sentiment_score = fb.score * fb.confidence
                finbert_label   = fb.label
                finbert_score   = fb.score
                finbert_conf    = fb.confidence
            else:
                # VADER fallback
                vr = vader_score(text)
                sentiment_score = vr.compound
                finbert_label   = None
                finbert_score   = None
                finbert_conf    = None
                log.debug("VADER fallback for: %s", article.title[:60])

            vr_full        = vader_score(text)
            vader_compound = vr_full.compound

            rank_score = abs(sentiment_score) * density * tw * dw

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
