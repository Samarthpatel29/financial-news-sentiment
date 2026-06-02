from __future__ import annotations
import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Text, create_engine
)
from sqlalchemy.orm import DeclarativeBase, Session

from config.settings import DATABASE_URL


class Base(DeclarativeBase):
    pass


class Article(Base):
    __tablename__ = "articles"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    source      = Column(String(64), nullable=False, index=True)
    title       = Column(Text, nullable=False)
    url         = Column(String(512), unique=True)
    published   = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    body        = Column(Text)
    fetched_at  = Column(DateTime, default=datetime.datetime.utcnow)


class SentimentResult(Base):
    __tablename__ = "sentiment_results"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    article_id    = Column(Integer, nullable=False, index=True)
    source        = Column(String(64), nullable=False, index=True)
    title         = Column(Text, nullable=False)
    url           = Column(String(512))
    published     = Column(DateTime, index=True)
    # FinBERT: positive / negative / neutral + confidence
    finbert_label = Column(String(16))
    finbert_score = Column(Float)
    finbert_conf  = Column(Float)
    # VADER compound [-1, 1]
    vader_compound = Column(Float)
    # Final blended score: positive=+1, negative=-1, neutral=0, scaled by conf
    sentiment_score = Column(Float, nullable=False)
    # Density score: how many articles from this source in the last window
    message_density = Column(Float, default=1.0)
    # Composite ranking key
    rank_score    = Column(Float, nullable=False)
    scored_at     = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    # Trust / time weighting (new)
    trust_weight  = Column(Float, default=1.0)
    time_weight   = Column(Float, default=1.0)
    # Comma-separated ticker symbols extracted from the article (e.g. "AAPL,MSFT")
    tickers       = Column(String(256), default="")
    # Article thumbnail image URL (from RSS media:thumbnail or OG image)
    image_url     = Column(String(1024), default="")


class TickerSentiment(Base):
    """Per-ticker aggregated sentiment, recomputed each pipeline cycle."""
    __tablename__ = "ticker_sentiment"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    ticker          = Column(String(10), unique=True, nullable=False, index=True)
    composite_score = Column(Float, nullable=False)   # weighted-avg sentiment
    article_count   = Column(Integer, default=0)
    bullish_count   = Column(Integer, default=0)
    bearish_count   = Column(Integer, default=0)
    neutral_count   = Column(Integer, default=0)
    avg_trust       = Column(Float, default=1.0)
    top_headline    = Column(Text)
    top_source      = Column(String(64))
    top_url         = Column(String(512))
    last_updated    = Column(DateTime, default=datetime.datetime.utcnow)


def init_db() -> Session:
    import os
    from sqlalchemy import inspect, text

    os.makedirs("data", exist_ok=True)
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

    # Additive migration: add any new columns that don't exist yet
    inspector = inspect(engine)
    if inspector.has_table("sentiment_results"):
        existing = {c["name"] for c in inspector.get_columns("sentiment_results")}
        new_cols = {
            "trust_weight": "REAL DEFAULT 1.0",
            "time_weight":  "REAL DEFAULT 1.0",
            "tickers":      'TEXT DEFAULT ""',
            "image_url":    'TEXT DEFAULT ""',
        }
        with engine.connect() as conn:
            for col, typedef in new_cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE sentiment_results ADD COLUMN {col} {typedef}"))
            conn.commit()

    Base.metadata.create_all(engine)
    return Session(engine)
