"""
Market-hours helper for US equities (NYSE/NASDAQ).
All times in US/Eastern.
"""
from __future__ import annotations
import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Market open / close in ET
_OPEN  = datetime.time(9, 30)
_CLOSE = datetime.time(16, 0)
_PRE   = datetime.time(4, 0)
_POST  = datetime.time(20, 0)


def now_et() -> datetime.datetime:
    return datetime.datetime.now(tz=ET)


def market_status() -> dict:
    """
    Returns a dict with keys:
      status  : "OPEN" | "PRE-MARKET" | "AFTER-HOURS" | "CLOSED"
      label   : short display label
      color   : hex color hint for the UI
      is_open : bool — True only during regular trading hours
    """
    now = now_et()
    weekday = now.weekday()  # 0=Mon … 6=Sun
    t = now.time()

    if weekday >= 5:  # Sat or Sun
        return {"status": "CLOSED", "label": "Market Closed", "color": "#5a7a96", "is_open": False}

    if _OPEN <= t < _CLOSE:
        return {"status": "OPEN", "label": "Market Open", "color": "#00d47e", "is_open": True}

    if _PRE <= t < _OPEN:
        return {"status": "PRE-MARKET", "label": "Pre-Market", "color": "#f5a623", "is_open": False}

    # Everything else on a weekday (after close OR overnight) = after-hours
    # This is the prime window for overnight sentiment analysis
    return {"status": "AFTER-HOURS", "label": "After-Hours", "color": "#9b59b6", "is_open": False}


def pipeline_interval_seconds() -> int:
    """
    Recommended pipeline interval based on current market status:
      After-hours / Pre-market  → 60s  (overnight news is high-value)
      Market open               → 90s  (still active, slightly relaxed)
      Weekend / overnight       → 600s (market fully closed, no rush)
    """
    ms = market_status()
    if ms["status"] in ("AFTER-HOURS", "PRE-MARKET"):
        return 60
    if ms["status"] == "OPEN":
        return 90
    return 600  # CLOSED (weekend / overnight)
