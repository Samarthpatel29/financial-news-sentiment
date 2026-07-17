"""
SEC filing section extractor — Phase B of the long-term fundamentals engine.

Filings are huge (a 10-K can be 300+ pages), so we download the primary document
and pull only the high-signal sections per form type:

  10-K (annual)   -> Risk Factors (Item 1A) + MD&A (Item 7)
  10-Q (earnings) -> MD&A (Item 2) + results-of-operations text
  8-K  (contract) -> Item 1.01 material agreement / Item 2.02 results / body

Everything is capped to a few thousand characters before scoring so FinBERT and
Groq stay fast and within the free tier. Pure stdlib + BeautifulSoup (free).
"""
from __future__ import annotations
import logging
import re

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "SentimentIQ Research (academic project; shp5246@psu.edu)",
    "Accept-Encoding": "gzip, deflate",
}

MAX_CHARS = 6000   # cap fed to scoring per filing

# Anchor phrases that mark the start of high-signal sections, by section kind.
_ANCHORS = {
    "annual": [
        "risk factors",
        "management's discussion and analysis",
        "management’s discussion and analysis",
    ],
    "earnings": [
        "management's discussion and analysis",
        "management’s discussion and analysis",
        "results of operations",
    ],
    "contract": [
        "item 1.01",
        "entry into a material definitive agreement",
        "item 2.02",
        "results of operations and financial condition",
    ],
}


def _clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "table"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _slice_around_anchors(text: str, kind: str) -> str:
    """Return text starting at the first matching section anchor, capped."""
    low = text.lower()
    best = None
    for anchor in _ANCHORS.get(kind, []):
        idx = low.find(anchor)
        # Skip a hit that's only in the table of contents (very early in doc)
        if idx != -1 and idx > 500:
            best = idx if best is None else min(best, idx)
    if best is not None:
        return text[best : best + MAX_CHARS]
    # Fallback: skip the cover page, take the first substantive chunk
    return text[500 : 500 + MAX_CHARS]


def extract_section(url: str, section_kind: str) -> str:
    """
    Download a filing's primary document and return the high-signal section text
    for scoring. Returns "" on any failure (caller skips gracefully).
    """
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        if resp.status_code != 200 or not resp.text:
            return ""
    except Exception as exc:
        log.debug("Filing fetch failed [%s]: %s", url, exc)
        return ""

    text = _clean_text(resp.text)
    if len(text) < 200:
        return ""
    return _slice_around_anchors(text, section_kind)
