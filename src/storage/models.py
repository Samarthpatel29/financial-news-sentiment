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
    # ── Long-term fundamentals (7-day SEC-filing signal) ──────────────────────
    fundamental_score   = Column(Float, default=0.0)
    fundamental_verdict = Column(String(16), default="")    # Improving|Stable|Deteriorating
    filing_count_7d     = Column(Integer, default=0)
    last_filing_at      = Column(DateTime)
    # ── Continuation signal (news + filings + all-time price record) ──────────
    price_score         = Column(Float, default=0.0)        # long-term price trend, -1..1
    price_return_1y     = Column(Float)                     # % (display)
    price_return_5y     = Column(Float)                     # % (display)
    pct_from_ath        = Column(Float)                     # % below all-time high (display)
    price_volatility    = Column(Float)                     # annualized %, for STABLE/VOLATILE tag
    continuation_score  = Column(Float, default=0.0)        # blended -1..1
    continuation_label  = Column(String(20), default="")    # Strong|Building|Mixed|Weak


class SignalHistory(Base):
    """
    Daily snapshot of each ticker's BUY/SELL/HOLD signal, scored against the
    actual price 7 days later — powers the honest "model accuracy" stat.
    """
    __tablename__ = "signal_history"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    ticker          = Column(String(10), index=True, nullable=False)
    signal          = Column(String(8), nullable=False)      # BUY | SELL | HOLD
    score           = Column(Float, default=0.0)             # continuation at signal time
    price_at_signal = Column(Float)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    # outcome (filled ~7 days later)
    price_after     = Column(Float)
    pct_change      = Column(Float)
    correct         = Column(Integer)                        # 1 | 0 | NULL = not scored yet


class Filing(Base):
    """
    A single SEC EDGAR filing (10-K annual, 10-Q earnings, 8-K contract/event)
    for one ticker, scored for the long-term fundamentals signal.
    """
    __tablename__ = "filings"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    cik               = Column(String(12), index=True)
    ticker            = Column(String(10), index=True)
    form_type         = Column(String(12), index=True)       # 10-K | 10-Q | 8-K
    section_kind      = Column(String(16))                   # earnings | annual | contract
    filed_at          = Column(DateTime, index=True)
    accession         = Column(String(24), unique=True, index=True)
    title             = Column(Text, default="")
    url               = Column(String(512))
    # Scoring (filled in later phases; nullable so Phase A can store raw filings)
    finbert_score     = Column(Float)
    fundamental_score = Column(Float)
    llm_summary       = Column(Text, default="")
    llm_verdict       = Column(String(16), default="")       # Improving|Stable|Deteriorating
    fetched_at        = Column(DateTime, default=datetime.datetime.utcnow)


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

    # Additive migration for long-term fundamentals columns on ticker_sentiment
    if inspector.has_table("ticker_sentiment"):
        existing = {c["name"] for c in inspector.get_columns("ticker_sentiment")}
        ts_cols = {
            "fundamental_score":   "REAL DEFAULT 0.0",
            "fundamental_verdict": 'TEXT DEFAULT ""',
            "filing_count_7d":     "INTEGER DEFAULT 0",
            "last_filing_at":      "TIMESTAMP",
            "price_score":         "REAL DEFAULT 0.0",
            "price_return_1y":     "REAL",
            "price_return_5y":     "REAL",
            "pct_from_ath":        "REAL",
            "price_volatility":    "REAL",
            "continuation_score":  "REAL DEFAULT 0.0",
            "continuation_label":  'TEXT DEFAULT ""',
        }
        with engine.connect() as conn:
            for col, typedef in ts_cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE ticker_sentiment ADD COLUMN {col} {typedef}"))
            conn.commit()

    Base.metadata.create_all(engine)
    return Session(engine)
