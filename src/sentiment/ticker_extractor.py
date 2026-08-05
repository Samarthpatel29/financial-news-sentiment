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

from config.tickers import (
    TICKER_UNIVERSE, COMPANY_TO_TICKER, AMBIGUOUS_NAMES, _STOPWORDS
)

log = logging.getLogger(__name__)

# Pre-compiled patterns
_DOLLAR_PAT  = re.compile(r'\$([A-Z]{1,5}(?:\.[A-B])?)')  # $AAPL, $BRK.B
_ALLCAPS_PAT = re.compile(r'\b([A-Z]{2,5})\b')            # standalone ALL-CAPS


def _name_pattern(name: str) -> re.Pattern:
    """
    Word-boundary matcher for a company name.

    Plain `name in text` matches inside unrelated words — "meta" inside
    "Rheinmetall", "ups" inside "groups", "intel" inside "intelligence",
    "unity" inside "opportunity". Anchoring with \\b removes those.
    \\b is only useful next to a word character, so it is added conditionally
    (a name like "at&t" ends in one, "s&p 500" does not start with a symbol).
    """
    left  = r'\b' if name[:1].isalnum() else ''
    right = r'\b' if name[-1:].isalnum() else ''
    return re.compile(left + re.escape(name) + right)


# (name, lowercase pattern, ticker, needs_capital), built once at import
_COMPANY_PATTERNS: list[tuple[str, re.Pattern, str, bool]] = [
    (name, _name_pattern(name), ticker, name in AMBIGUOUS_NAMES)
    for name, ticker in COMPANY_TO_TICKER.items()
]


def _company_matches(text: str, text_lower: str):
    """Yield tickers whose company name appears as a whole word in *text*."""
    for name, pat, ticker, needs_capital in _COMPANY_PATTERNS:
        # cheap substring prefilter first, then the authoritative boundary check
        if name not in text_lower:
            continue
        match = pat.search(text_lower)
        if not match:
            continue
        # Ambiguous aliases must be capitalised in the original headline to
        # count — "Snap beat estimates" yes, "a snap decision" no. Compare the
        # matched span back against the original-case text.
        if needs_capital:
            spans = [m.start() for m in pat.finditer(text_lower)]
            if not any(text[i:i + 1].isupper() for i in spans):
                continue
        yield ticker


def extract_tickers(text: str, *, max_tickers: int = 10) -> list[str]:
    """
    Return a sorted list of unique ticker symbols found in *text*.
    Limited to *max_tickers* to avoid noise from very long articles.
    """
    if not text:
        return []

    # Track which pass found each symbol so truncation can keep the most
    # reliable ones: $-prefix (1) > company name (2) > bare ALL-CAPS (3).
    precision: dict[str, int] = {}

    def _add(sym: str, rank: int) -> None:
        if rank < precision.get(sym, 99):
            precision[sym] = rank

    # ── Pass 1: $TICKER ────────────────────────────────────────────────────────
    # No stopword filter here: an explicit cashtag is unambiguous intent, and
    # several real symbols are also common words ($ON, $OPEN, $NOW, $ALL).
    for m in _DOLLAR_PAT.finditer(text):
        sym = m.group(1)
        if sym in TICKER_UNIVERSE:
            _add(sym, 1)

    # ── Pass 2: Company names (whole word only) ───────────────────────────────
    text_lower = text.lower()
    for ticker in _company_matches(text, text_lower):
        _add(ticker, 2)

    # ── Pass 3: ALL-CAPS words ─────────────────────────────────────────────────
    for m in _ALLCAPS_PAT.finditer(text):
        sym = m.group(1)
        if sym in TICKER_UNIVERSE and sym not in _STOPWORDS:
            _add(sym, 3)

    if len(precision) > max_tickers:
        # Drop the least reliable matches first, then break ties alphabetically
        keep = sorted(precision, key=lambda s: (precision[s], s))[:max_tickers]
        return sorted(keep)

    return sorted(precision)


def extract_primary_ticker(text: str) -> str | None:
    """
    Return the single most prominent ticker, or None.
    Priority: $-prefix > company name > all-caps.
    """
    if not text:
        return None

    # $-prefix first (explicit cashtag — no stopword filter, see extract_tickers)
    for m in _DOLLAR_PAT.finditer(text):
        sym = m.group(1)
        if sym in TICKER_UNIVERSE:
            return sym

    # Company name (whole word only)
    text_lower = text.lower()
    for ticker in _company_matches(text, text_lower):
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
