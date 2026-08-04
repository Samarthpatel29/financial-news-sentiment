"""
Finviz screener CSV loader.

The brief: "A CSV file is extracted via an existing screener on Finviz." Finviz
(Elite) lets you export a screener's results as a CSV — a "Ticker" column plus
whatever fields the screener shows. Drop that export at data/finviz_screener.csv
(or point FINVIZ_SCREENER_CSV at it) and the tracked ticker universe picks it up
automatically. When no CSV is present we fall back to the built-in universe, so
nothing breaks before your Finviz credentials arrive.

Pure stdlib (csv) — free, no Finviz API or login needed to READ an export.
"""
from __future__ import annotations
import csv
import logging
import os

log = logging.getLogger(__name__)

# Where the Finviz screener export is expected. Override with the env var.
DEFAULT_CSV = os.getenv("FINVIZ_SCREENER_CSV", "data/finviz_screener.csv")

# Finviz's export header is "Ticker"; accept a couple of common variants.
_TICKER_COLS = {"ticker", "symbol", "tickers"}


def _looks_like_ticker(t: str) -> bool:
    # 1–6 chars, letters/digits with an optional . or - (e.g. BRK.B, RDS-A)
    return bool(t) and len(t) <= 6 and t.replace(".", "").replace("-", "").isalnum()


def load_screener_tickers(path: str | None = None) -> set[str]:
    """
    Return the set of tickers from a Finviz screener CSV export.
    Empty set when the file is missing or unreadable (caller falls back).
    """
    path = path or DEFAULT_CSV
    if not path or not os.path.isfile(path):
        return set()

    out: set[str] = set()
    try:
        # utf-8-sig strips the BOM Finviz sometimes prepends.
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            col = next((name for name in (reader.fieldnames or [])
                        if name and name.strip().lower() in _TICKER_COLS), None)
            if col is None:
                log.warning("Finviz CSV %s has no Ticker/Symbol column (%s)",
                            path, reader.fieldnames)
                return set()
            for row in reader:
                t = (row.get(col) or "").strip().upper()
                if _looks_like_ticker(t):
                    out.add(t)
    except Exception as exc:  # noqa: BLE001 — never break startup on a bad file
        log.warning("Finviz screener CSV load failed (%s): %s", path, exc)
        return set()

    log.info("Finviz screener CSV: loaded %d tickers from %s", len(out), path)
    return out
