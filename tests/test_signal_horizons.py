"""
Weekly + monthly signal grading (long-term validation).

Locks in that _score_signals grades BOTH horizons: the weekly (7-day) outcome
and the monthly (30-day) outcome with its wider thresholds. This is the
long-term self-check the dashboard reports.
"""
from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.storage.models import Base, SignalHistory
import src.pipeline.fundamentals as fund
import src.collectors.price_history as ph


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _row(s, *, signal, days_old, price_at):
    s.add(SignalHistory(
        ticker="AAPL",
        signal=signal,
        score=0.5 if signal == "BUY" else -0.5,
        price_at_signal=price_at,
        created_at=datetime.datetime.utcnow() - datetime.timedelta(days=days_old),
    ))
    s.commit()


def test_monthly_grade_filled_for_old_row(db, monkeypatch):
    # BUY at 100, now 110 (+10%). A 31-day-old row must be graded on BOTH the
    # weekly and the monthly horizon; +10% is a correct BUY on each.
    monkeypatch.setattr(ph, "get_price_stats", lambda t: {"latest": 110.0})
    _row(db, signal="BUY", days_old=31, price_at=100.0)

    fund._score_signals(db)

    s = db.query(SignalHistory).first()
    assert s.correct == 1                       # weekly (7d) graded too
    assert s.correct_30d == 1                    # monthly graded
    assert s.pct_change_30d == pytest.approx(10.0, abs=0.01)
    assert s.price_after_30d == 110.0


def test_monthly_not_graded_before_30_days(db, monkeypatch):
    # A 10-day-old row is old enough for the weekly grade but NOT the monthly one.
    monkeypatch.setattr(ph, "get_price_stats", lambda t: {"latest": 105.0})
    _row(db, signal="BUY", days_old=10, price_at=100.0)

    fund._score_signals(db)

    s = db.query(SignalHistory).first()
    assert s.correct == 1                         # weekly done (+5% > 1%)
    assert s.correct_30d is None                   # monthly still pending


def test_monthly_wider_threshold(db, monkeypatch):
    # +1.5% is a correct BUY weekly (>1%) but NOT monthly (needs >2%).
    monkeypatch.setattr(ph, "get_price_stats", lambda t: {"latest": 101.5})
    _row(db, signal="BUY", days_old=31, price_at=100.0)

    fund._score_signals(db)

    s = db.query(SignalHistory).first()
    assert s.correct == 1
    assert s.correct_30d == 0
