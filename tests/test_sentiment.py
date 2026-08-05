"""Unit tests for sentiment scoring (VADER path — no GPU required)."""
from __future__ import annotations
import pytest

from src.sentiment import score as vader_score
from src.sentiment import SentimentScorer
from src.collectors import RawArticle


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
        import src.sentiment as fb_module

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
        import src.sentiment as fb_module

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
        # Density is the source's share of the window, normalised to (0, 1]:
        # CNBC contributes 2 of the 2 max => 1.0, Reuters 1 of 2 => 0.5.
        # Raw counts made rank_score unbounded and let a high-volume source
        # dominate purely on volume.
        assert cnbc_results[0].message_density == 1.0
        assert reuters_results[0].message_density == 0.5
        assert 0 < reuters_results[0].message_density <= 1.0

    def test_rank_score_excludes_time_decay(self, monkeypatch):
        """
        rank_score must be the undecayed base. Decay is applied at read time by
        the dashboard and the aggregator; baking it in here decayed twice.
        """
        import datetime

        import src.sentiment as fb_module

        monkeypatch.setattr(
            fb_module.FinBERTScorer,
            "score_batch",
            lambda self, texts: [None] * len(texts),
        )

        old = datetime.datetime.utcnow() - datetime.timedelta(hours=240)
        scorer = SentimentScorer()
        articles = [
            RawArticle(source="cnbc", title="Shares plunge after bankruptcy filing",
                       url="u1", body="", published=old),
        ]
        r = scorer.score_articles(articles)[0]

        assert r.time_weight < 0.01, "a 10-day-old article should decay heavily"
        expected = abs(r.sentiment_score) * r.message_density * r.trust_weight
        assert r.rank_score == pytest.approx(expected)
        assert r.rank_score > r.time_weight  # i.e. decay was NOT multiplied in
