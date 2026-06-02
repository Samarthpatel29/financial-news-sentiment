"""
Ticker extractor: finds stock ticker symbols mentioned in article text.

Three pass strategy (ordered by precision):
  1. $TICKER  — explicit dollar-prefix (highest precision)
  2. ALL-CAPS words filtered against TICKER_UNIVERSE (medium precision)
  3. Company name substring match from COMPANY_TO_TICKER (catches "Apple", "Tesla" etc.)
"""
from __future__ import annotations
import re
import logging
from typing import Sequence

from config.tickers import TICKER_UNIVERSE, COMPANY_TO_TICKER, _STOPWORDS

log = logging.getLogger(__name__)

# Pre-compiled patterns
_DOLLAR_PAT  = re.compile(r'\$([A-Z]{1,5}(?:\.[A-B])?)')  # $AAPL, $BRK.B
_ALLCAPS_PAT = re.compile(r'\b([A-Z]{2,5})\b')            # standalone ALL-CAPS


def extract_tickers(text: str, *, max_tickers: int = 10) -> list[str]:
    """
    Return a sorted list of unique ticker symbols found in *text*.
    Limited to *max_tickers* to avoid noise from very long articles.
    """
    if not text:
        return []

    found: set[str] = set()

    # ── Pass 1: $TICKER ────────────────────────────────────────────────────────
    for m in _DOLLAR_PAT.finditer(text):
        sym = m.group(1)
        if sym in TICKER_UNIVERSE:
            found.add(sym)

    # ── Pass 2: ALL-CAPS words ─────────────────────────────────────────────────
    for m in _ALLCAPS_PAT.finditer(text):
        sym = m.group(1)
        if sym in TICKER_UNIVERSE and sym not in _STOPWORDS:
            found.add(sym)

    # ── Pass 3: Company name substrings ───────────────────────────────────────
    text_lower = text.lower()
    for name, ticker in COMPANY_TO_TICKER.items():
        if name in text_lower:
            found.add(ticker)

    tickers = sorted(found)
    if len(tickers) > max_tickers:
        # Keep the most "specific" ones — prefer those found by $-prefix or
        # company name (Passes 1 & 3). For simplicity just trim the sorted list.
        tickers = tickers[:max_tickers]

    return tickers


def extract_primary_ticker(text: str) -> str | None:
    """
    Return the single most prominent ticker, or None.
    Priority: $-prefix > company name > all-caps.
    """
    if not text:
        return None

    # $-prefix first
    for m in _DOLLAR_PAT.finditer(text):
        sym = m.group(1)
        if sym in TICKER_UNIVERSE:
            return sym

    # Company name
    text_lower = text.lower()
    for name, ticker in COMPANY_TO_TICKER.items():
        if name in text_lower:
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
