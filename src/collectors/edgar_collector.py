"""
SEC EDGAR filings collector — Phase A of the long-term fundamentals engine.

Pulls each tracked ticker's recent official filings (10-K annual, 10-Q earnings,
8-K contract/event) from the **free** SEC EDGAR APIs (no key required) and
returns them as RawFiling records for storage + scoring in later phases.

Free endpoints used (SEC asks for a descriptive User-Agent and <=10 req/sec):
  - ticker->CIK map:  https://www.sec.gov/files/company_tickers.json
  - filing history:   https://data.sec.gov/submissions/CIK##########.json
  - the document:     https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}

See docs/FUNDAMENTALS_PLAN.md for the full design.
"""
from __future__ import annotations
import datetime
import logging
import time
from dataclasses import dataclass, field
from typing import Iterable

import requests

log = logging.getLogger(__name__)

# SEC requires a real, descriptive User-Agent (their fair-access policy).
_HEADERS = {
    "User-Agent": "SentimentIQ Research (academic project; shp5246@psu.edu)",
    "Accept-Encoding": "gzip, deflate",
}

_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"

# Form type -> our long-term section category
_FORM_KIND = {
    "10-K":  "annual",
    "10-K/A":"annual",
    "10-Q":  "earnings",
    "10-Q/A":"earnings",
    "8-K":   "contract",   # 8-Ks include material agreements / earnings releases
    "8-K/A": "contract",
}

# Rolling window for "long-term, within 1 week" signal
LOOKBACK_DAYS = 7


@dataclass
class RawFiling:
    cik:          str
    ticker:       str
    form_type:    str
    section_kind: str
    filed_at:     datetime.datetime
    accession:    str
    url:          str
    title:        str = ""


class EdgarCollector:
    """Fetches recent 10-K / 10-Q / 8-K filings for a set of tickers."""

    def __init__(self, lookback_days: int = LOOKBACK_DAYS):
        self.lookback_days = lookback_days
        self._cik_map: dict[str, str] | None = None     # TICKER -> 10-digit CIK
        self._map_loaded_at: float = 0.0

    # ── ticker -> CIK map (cached ~24h) ────────────────────────────────────────
    def _load_cik_map(self) -> dict[str, str]:
        if self._cik_map is not None and (time.time() - self._map_loaded_at) < 86400:
            return self._cik_map
        try:
            resp = requests.get(_TICKER_MAP_URL, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("EDGAR ticker map fetch failed: %s", exc)
            return self._cik_map or {}

        # data is {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
        mapping: dict[str, str] = {}
        for row in data.values():
            tic = str(row.get("ticker", "")).upper()
            cik = str(row.get("cik_str", "")).zfill(10)
            if tic:
                mapping[tic] = cik
        self._cik_map = mapping
        self._map_loaded_at = time.time()
        log.info("EDGAR ticker->CIK map loaded (%d companies)", len(mapping))
        return mapping

    # ── recent filings for one ticker ──────────────────────────────────────────
    def _filings_for_ticker(self, ticker: str, cik: str,
                            cutoff: datetime.datetime) -> list[RawFiling]:
        url = _SUBMISSIONS_URL.format(cik10=cik)
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
            if resp.status_code != 200:
                return []
            recent = resp.json().get("filings", {}).get("recent", {})
        except Exception as exc:
            log.debug("EDGAR submissions failed [%s]: %s", ticker, exc)
            return []

        forms      = recent.get("form", [])
        dates      = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primaries  = recent.get("primaryDocument", [])
        titles     = recent.get("primaryDocDescription", [])

        out: list[RawFiling] = []
        cik_int = str(int(cik))   # Archives path uses the un-padded CIK
        for i, form in enumerate(forms):
            if form not in _FORM_KIND:
                continue
            try:
                filed = datetime.datetime.strptime(dates[i], "%Y-%m-%d")
            except (ValueError, IndexError):
                continue
            if filed < cutoff:
                continue
            acc = accessions[i]
            acc_nodash = acc.replace("-", "")
            doc = primaries[i] if i < len(primaries) else ""
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"
                if doc else
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
            )
            out.append(RawFiling(
                cik=cik, ticker=ticker, form_type=form,
                section_kind=_FORM_KIND[form], filed_at=filed,
                accession=acc, url=doc_url,
                title=(titles[i] if i < len(titles) else "") or form,
            ))
        return out

    # ── all-time report history (10-K / 10-Q) for one ticker ──────────────────
    def collect_history(self, ticker: str, max_filings: int = 16) -> list[RawFiling]:
        """
        The company's report history going back years — 10-K annual reports and
        10-Q earnings reports, newest first, capped at max_filings. Same free
        submissions JSON as collect(); no extra API, no key.
        """
        cik_map = self._load_cik_map()
        tic = str(ticker).upper().lstrip("$")
        cik = cik_map.get(tic)
        if not cik:
            return []
        url = _SUBMISSIONS_URL.format(cik10=cik)
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
            if resp.status_code != 200:
                return []
            recent = resp.json().get("filings", {}).get("recent", {})
        except Exception as exc:
            log.debug("EDGAR history failed [%s]: %s", tic, exc)
            return []

        forms      = recent.get("form", [])
        dates      = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primaries  = recent.get("primaryDocument", [])
        titles     = recent.get("primaryDocDescription", [])

        out: list[RawFiling] = []
        cik_int = str(int(cik))
        for i, form in enumerate(forms):
            if form not in ("10-K", "10-Q"):
                continue
            try:
                filed = datetime.datetime.strptime(dates[i], "%Y-%m-%d")
            except (ValueError, IndexError):
                continue
            acc = accessions[i]
            doc = primaries[i] if i < len(primaries) else ""
            if not doc:
                continue
            out.append(RawFiling(
                cik=cik, ticker=tic, form_type=form,
                section_kind=_FORM_KIND[form], filed_at=filed,
                accession=acc,
                url=f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc.replace('-','')}/{doc}",
                title=(titles[i] if i < len(titles) else "") or form,
            ))
            if len(out) >= max_filings:
                break
        return out

    # ── public API ─────────────────────────────────────────────────────────────
    def collect(self, tickers: Iterable[str]) -> list[RawFiling]:
        cik_map = self._load_cik_map()
        if not cik_map:
            return []
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=self.lookback_days)

        results: list[RawFiling] = []
        seen_ciks: set[str] = set()
        for raw_tic in tickers:
            tic = str(raw_tic).upper().lstrip("$")
            cik = cik_map.get(tic)
            if not cik or cik in seen_ciks:
                continue
            seen_ciks.add(cik)
            results.extend(self._filings_for_ticker(tic, cik, cutoff))
            time.sleep(0.12)   # be polite: well under SEC's 10 req/sec limit

        log.info("EDGAR: %d filings in last %dd across %d tickers",
                 len(results), self.lookback_days, len(seen_ciks))
        return results
