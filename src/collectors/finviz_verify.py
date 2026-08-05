"""
Finviz cross-check — verify a prediction against an independent source.

Fetches the free Finviz quote page for a ticker and pulls the data points a
user can check our BUY/SELL/HOLD signal against:
  - analyst recommendation (Finviz "Recom", 1=Strong Buy … 5=Strong Sell)
  - analyst price target (+ implied upside vs current price)
  - actual recent performance (week / month / year / YTD)

We compare our signal to the analyst consensus and report AGREE / MIXED /
DISAGREE, so the prediction can be independently validated. Finviz is one of
the project's sanctioned sources; we fetch politely (real User-Agent, cached
~30 min, low volume) and degrade gracefully if the page is unavailable.
"""
from __future__ import annotations
import re
import time

import requests

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_URL = "https://finviz.com/quote.ashx?t={t}"
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 1800   # 30 min

_FIELDS = ["Price", "Recom", "Target Price",
           "Perf Week", "Perf Month", "Perf Year", "Perf YTD"]


def _grab(html: str, label: str) -> str | None:
    i = html.find(f">{label}</a>")
    if i == -1:
        i = html.find(f">{label}<")
    if i == -1:
        return None
    j = html.find("snapshot-td-content", i)
    if j == -1:
        return None
    chunk = html[j:html.find("</div>", j)]
    text = re.sub(r"<[^>]+>", "", chunk).replace('snapshot-td-content"', "").strip(' ">')
    return text or None


def _num(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace("%", "").replace("$", "").replace(",", ""))
    except ValueError:
        return None


def _recom_to_signal(recom: float | None) -> str | None:
    """Finviz Recom 1..5 → analyst consensus as BUY/HOLD/SELL."""
    if recom is None:
        return None
    if recom <= 2.5:
        return "BUY"
    if recom >= 3.5:
        return "SELL"
    return "HOLD"


def _agreement(ours: str, theirs: str | None) -> str:
    if theirs is None:
        return "UNKNOWN"
    if ours == theirs:
        return "AGREE"
    # one side neutral, the other directional
    if "HOLD" in (ours, theirs):
        return "MIXED"
    return "DISAGREE"   # BUY vs SELL


def verify(ticker: str, our_signal: str = "") -> dict | None:
    """
    Cross-check our signal against Finviz for one ticker.
    Returns a dict of the Finviz data + agreement verdict, or None on failure.
    """
    key = ticker.upper().lstrip("$")
    now = time.time()
    if key in _CACHE and (now - _CACHE[key][0]) < _TTL:
        data = dict(_CACHE[key][1])
    else:
        try:
            resp = requests.get(_URL.format(t=key), headers={"User-Agent": _UA},
                                timeout=15, allow_redirects=True)
            if resp.status_code != 200 or "snapshot-td-content" not in resp.text:
                return None
            html = resp.text
        except Exception:
            return None

        raw = {f: _grab(html, f) for f in _FIELDS}
        price  = _num(raw.get("Price"))
        recom  = _num(raw.get("Recom"))
        target = _num(raw.get("Target Price"))
        upside = round((target / price - 1.0) * 100, 1) if (target and price) else None
        data = {
            "ticker":         key,
            "price":          price,
            "recom":          recom,
            "recom_signal":   _recom_to_signal(recom),
            "target":         target,
            "target_upside":  upside,
            "perf_week":      raw.get("Perf Week"),
            "perf_month":     raw.get("Perf Month"),
            "perf_year":      raw.get("Perf Year"),
            "perf_ytd":       raw.get("Perf YTD"),
            "source_url":     f"https://finviz.com/quote.ashx?t={key}",
            "checked_at":     time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now)),
        }
        _CACHE[key] = (now, dict(data))

    if our_signal:
        data["our_signal"] = our_signal.upper()
        data["agreement"] = _agreement(our_signal.upper(), data.get("recom_signal"))
    return data
