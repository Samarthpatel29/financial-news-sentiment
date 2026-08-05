"""
Regression tests for per-ticker aggregation.

Covers the three defects found in the 2026-07-21 audit:
  * stale tickers were never removed (upsert-only), so a ticker last seen weeks
    ago kept its score and was served as current
  * a single article could pin a ticker at ±0.95 and outrank well-sampled names
  * time decay was read from a column frozen at scoring time, so old articles
    never actually decayed inside the aggregation window
"""
from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.storage import Base, SentimentResult, TickerSentiment
from src.pipeline import aggregate_tickers, _live_time_weight


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _row(tickers, score, *, hours_old=1.0, source="cnbc", title=None, url=None):
    now = datetime.datetime.utcnow()
    stamp = now - datetime.timedelta(hours=hours_old)
    return SentimentResult(
        article_id=0,
        source=source,
        title=title or f"{tickers} headline",
        url=url or f"https://example.com/{tickers}/{score}/{hours_old}",
        published=stamp,
        sentiment_score=score,
        message_density=1.0,
        trust_weight=1.0,
        time_weight=1.0,
        rank_score=abs(score),
        tickers=tickers,
        scored_at=stamp,
    )


class TestMinimumSample:
    def test_thinly_sampled_ticker_is_withheld(self, db):
        db.add(_row("DHR", -0.96))          # a single article
        db.commit()
        aggregate_tickers(db)
        assert db.query(TickerSentiment).filter_by(ticker="DHR").first() is None

    def test_well_sampled_ticker_is_published(self, db):
        for i in range(4):
            db.add(_row("NVDA", -0.9, hours_old=i + 1))
        db.commit()
        aggregate_tickers(db)
        row = db.query(TickerSentiment).filter_by(ticker="NVDA").first()
        assert row is not None
        assert row.article_count == 4
        assert row.composite_score < 0


class TestStalePruning:
    def test_ticker_no_longer_in_the_news_is_dropped(self, db):
        db.add(TickerSentiment(
            ticker="JACK", composite_score=-0.57, article_count=131,
            last_updated=datetime.datetime.utcnow() - datetime.timedelta(days=19),
        ))
        for i in range(3):
            db.add(_row("NVDA", -0.8, hours_old=i + 1))
        db.commit()

        aggregate_tickers(db)

        assert db.query(TickerSentiment).filter_by(ticker="JACK").first() is None
        assert db.query(TickerSentiment).filter_by(ticker="NVDA").first() is not None

    def test_empty_cycle_does_not_wipe_the_board(self, db):
        """A failed fetch must not clear every ticker on the dashboard."""
        db.add(TickerSentiment(
            ticker="AAPL", composite_score=0.4, article_count=50,
            last_updated=datetime.datetime.utcnow(),
        ))
        db.commit()
        aggregate_tickers(db)          # no SentimentResult rows at all
        assert db.query(TickerSentiment).filter_by(ticker="AAPL").first() is not None

    def test_ticker_with_live_filings_is_kept_but_news_zeroed(self, db):
        db.add(TickerSentiment(
            ticker="MSFT", composite_score=-0.8, article_count=99,
            filing_count_7d=2,
            last_updated=datetime.datetime.utcnow() - datetime.timedelta(days=10),
        ))
        for i in range(3):
            db.add(_row("NVDA", 0.5, hours_old=i + 1))
        db.commit()

        aggregate_tickers(db)

        msft = db.query(TickerSentiment).filter_by(ticker="MSFT").first()
        assert msft is not None, "rows with live filings should survive"
        assert msft.article_count == 0
        assert msft.composite_score == 0.0


class TestTimeDecay:
    def test_decay_is_evaluated_now_not_at_scoring_time(self, db):
        """A stored time_weight of 1.0 must not keep an old article fresh."""
        stale = _row("AAPL", 0.9, hours_old=168)   # a week old
        assert stale.time_weight == 1.0            # frozen value, as written by the scorer
        now = datetime.datetime.utcnow()
        assert _live_time_weight(stale, now) < 0.05

    def test_recent_article_keeps_most_of_its_weight(self, db):
        fresh = _row("AAPL", 0.9, hours_old=0.5)
        assert _live_time_weight(fresh, datetime.datetime.utcnow()) > 0.95

    def test_fresh_news_outweighs_week_old_news(self, db):
        # 3 week-old bullish articles vs 3 fresh bearish ones
        for i in range(3):
            db.add(_row("TSLA", +0.9, hours_old=168 + i))
        for i in range(3):
            db.add(_row("TSLA", -0.9, hours_old=1 + i))
        db.commit()

        aggregate_tickers(db)

        row = db.query(TickerSentiment).filter_by(ticker="TSLA").first()
        assert row.composite_score < 0, "recent bearish news should dominate"


class TestNewsSocialSplit:
    """
    Retail chatter supplies ~90% of all rows. Averaged in with news it drowned
    the reporting entirely — PayPal's buyout coverage was 1 row against 287
    StockTwits posts whose mean was +0.008, which flipped the ticker's sign.
    """

    def test_chatter_does_not_move_the_news_composite(self, db):
        # 3 clearly bullish news articles ...
        for i in range(3):
            db.add(_row("PYPL", +0.9, source="marketwatch", hours_old=i + 1,
                        url=f"https://mw.com/{i}"))
        # ... buried under 60 bearish retail posts
        for i in range(60):
            db.add(_row("PYPL", -0.9, source="stocktwits", hours_old=1,
                        url=f"https://stocktwits.com/{i}"))
        db.commit()

        aggregate_tickers(db)

        row = db.query(TickerSentiment).filter_by(ticker="PYPL").first()
        assert row.composite_score > 0, "news sentiment must survive the chatter"
        assert row.article_count == 3, "social posts must not count as articles"
        assert row.social_score < 0, "chatter is still reported, just separately"
        assert row.social_count == 60

    def test_social_only_ticker_is_not_published(self, db):
        for i in range(40):
            db.add(_row("GME", +0.9, source="reddit_wsb", hours_old=1,
                        url=f"https://reddit.com/{i}"))
        db.commit()
        aggregate_tickers(db)
        assert db.query(TickerSentiment).filter_by(ticker="GME").first() is None


class TestTopHeadline:
    def test_duplicate_titles_do_not_misattribute_the_source(self, db):
        shared = "Stock market falls as chip selloff broadens"
        db.add(_row("MU", -0.2, source="seeking_alpha", title=shared,
                    url="https://seekingalpha.com/a"))
        db.add(_row("MU", -0.3, source="cnbc", title=shared,
                    url="https://cnbc.com/b"))
        db.add(_row("MU", -0.4, source="marketwatch", title="Another headline",
                    url="https://mw.com/c"))
        db.commit()
        # make the cnbc copy the clear rank winner
        for r in db.query(SentimentResult).filter_by(source="cnbc"):
            r.rank_score = 99.0
        db.commit()

        aggregate_tickers(db)

        row = db.query(TickerSentiment).filter_by(ticker="MU").first()
        assert row.top_headline == shared
        assert row.top_source == "cnbc"
        assert row.top_url == "https://cnbc.com/b"
