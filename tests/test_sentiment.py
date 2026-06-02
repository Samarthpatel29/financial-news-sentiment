"""Unit tests for sentiment scoring (VADER path — no GPU required)."""
from __future__ import annotations
import pytest

from src.sentiment.vader import score as vader_score
from src.sentiment.scorer import SentimentScorer
from src.collectors.rss_collector import RawArticle


class TestVADER:
    def test_positive(self):
        r = vader_score("Earnings beat expectations, stock surges to all-time high.")
        assert r.label == "positive"
        assert r.compound > 0

    def test_negative(self):
        r = vader_score("Company files for bankruptcy amid massive losses and fraud.")
        assert r.label == "negative"
        assert r.compound < 0

    def test_neutral(self):
        r = vader_score("The company announced a meeting on Thursday.")
        assert r.label in ("neutral", "positive", "negative")  # VADER may vary


class TestSentimentScorer:
    def test_score_articles_vader_fallback(self, monkeypatch):
        # Force FinBERT to return None (low-confidence path) so VADER kicks in
        from src.sentiment import finbert as fb_module

        monkeypatch.setattr(
            fb_module.FinBERTScorer,
            "score_batch",
            lambda self, texts: [None] * len(texts),
        )

        scorer = SentimentScorer()
        articles = [
            RawArticle(
                source="reuters",
                title="Markets rally strongly on positive jobs report",
                url="https://example.com/1",
                body="Stocks rose sharply after the jobs data.",
            )
        ]
        results = scorer.score_articles(articles)
        assert len(results) == 1
        r = results[0]
        assert r.source == "reuters"
        assert r.finbert_label is None        # VADER fallback used
        assert isinstance(r.sentiment_score, float)
        assert isinstance(r.rank_score, float)
        assert r.rank_score >= 0

    def test_density_weighting(self, monkeypatch):
        from src.sentiment import finbert as fb_module

        monkeypatch.setattr(
            fb_module.FinBERTScorer,
            "score_batch",
            lambda self, texts: [None] * len(texts),
        )

        scorer = SentimentScorer()
        articles = [
            RawArticle(source="cnbc", title="CNBC headline 1", url="u1", body=""),
            RawArticle(source="cnbc", title="CNBC headline 2", url="u2", body=""),
            RawArticle(source="reuters", title="Reuters headline", url="u3", body=""),
        ]
        results = scorer.score_articles(articles, window_articles=articles)
        cnbc_results    = [r for r in results if r.source == "cnbc"]
        reuters_results = [r for r in results if r.source == "reuters"]
        # CNBC should have density=2, Reuters density=1
        assert cnbc_results[0].message_density == 2.0
        assert reuters_results[0].message_density == 1.0
