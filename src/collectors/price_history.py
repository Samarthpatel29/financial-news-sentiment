"""
All-time price history + reliability stats — free, no API key.

Source: Yahoo Finance via the `yfinance` library (free, handles Yahoo's session
cookie/crumb). We pull full monthly history (period="max") — light and fast,
and plenty of resolution for a long-term reliability view.

We turn that history into the "is this stock reliable?" stats a long-term
investor wants: all-time high/low, distance from the peak, 1-year / 5-year /
all-time returns, max drawdown, annualized volatility, plus a sparkline.

Results are cached in-memory for a few hours (history barely changes intraday
and we stay polite to Yahoo).
"""
from __future__ import annotations
import logging
import math
import time

log = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, dict]] = {}     # ticker -> (fetched_at, stats)
_NEG_CACHE: dict[str, float] = {}              # ticker -> when we last got nothing
_TTL = 6 * 3600                                # 6 hours
_NEG_TTL = 1800                                # don't re-hit a dead ticker for 30 min
_SPARK_POINTS = 64


def _downsample(closes: list[float], n: int = _SPARK_POINTS) -> list[float]:
    if len(closes) <= n:
        return [round(c, 2) for c in closes]
    step = len(closes) / n
    return [round(closes[min(int(i * step), len(closes) - 1)], 2) for i in range(n)]


def _fetch_closes(ticker: str) -> tuple[list[str], list[float]]:
    """Return (dates, monthly closes) oldest→newest, or ([],[]) on failure."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="max", interval="1mo", auto_adjust=True)
    except Exception as exc:
        log.debug("yfinance failed [%s]: %s", ticker, exc)
        return [], []
    if hist is None or hist.empty or "Close" not in hist:
        return [], []
    closes, dates = [], []
    for idx, val in hist["Close"].items():
        try:
            c = float(val)
        except (TypeError, ValueError):
            continue
        if c > 0 and not math.isnan(c):
            closes.append(c)
            dates.append(idx.date().isoformat())
    return dates, closes


_CANDLE_CACHE: dict[str, tuple[float, list]] = {}
_CANDLE_TTL = 1800   # 30 min — daily candles barely move intraday


def get_candles(ticker: str, days: int = 45) -> list[dict]:
    """
    Recent daily OHLC candles for the candlestick chart (free, via yfinance).
    Returns [{d, o, h, l, c}, ...] oldest→newest, or [] on failure.
    """
    key = ticker.upper().lstrip("$")
    now = time.time()
    if key in _CANDLE_CACHE and (now - _CANDLE_CACHE[key][0]) < _CANDLE_TTL:
        return _CANDLE_CACHE[key][1]
    try:
        import yfinance as yf
        hist = yf.Ticker(key).history(period="3mo", interval="1d", auto_adjust=True)
    except Exception as exc:
        log.debug("yfinance candles failed [%s]: %s", key, exc)
        return []
    if hist is None or hist.empty:
        return []
    out: list[dict] = []
    for idx, row in hist.iterrows():
        try:
            o, h, l, c = (float(row["Open"]), float(row["High"]),
                          float(row["Low"]), float(row["Close"]))
        except (KeyError, TypeError, ValueError):
            continue
        if any(map(math.isnan, (o, h, l, c))) or c <= 0:
            continue
        out.append({"d": idx.date().isoformat(),
                    "o": round(o, 2), "h": round(h, 2),
                    "l": round(l, 2), "c": round(c, 2)})
    out = out[-days:]
    _CANDLE_CACHE[key] = (now, out)
    return out


def get_price_stats(ticker: str) -> dict | None:
    """All-time reliability stats for a ticker, or None if no data."""
    key = ticker.upper().lstrip("$")
    now = time.time()
    if key in _CACHE and (now - _CACHE[key][0]) < _TTL:
        return _CACHE[key][1]
    if key in _NEG_CACHE and (now - _NEG_CACHE[key]) < _NEG_TTL:
        return None

    dates, closes = _fetch_closes(key)
    if len(closes) < 12:
        _NEG_CACHE[key] = now
        return None

    latest = closes[-1]
    ath    = max(closes)
    atl    = min(closes)

    def ret_over(months: int) -> float | None:
        if len(closes) <= months:
            return None
        past = closes[-months - 1]
        return (latest / past - 1.0) * 100 if past else None

    # max drawdown on monthly closes (largest peak-to-trough drop)
    peak = closes[0]; max_dd = 0.0
    for c in closes:
        peak = max(peak, c)
        if peak:
            max_dd = min(max_dd, (c / peak - 1.0))

    # annualized volatility from monthly returns (×√12)
    rets = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes)) if closes[i - 1]]
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
        vol = math.sqrt(var) * math.sqrt(12) * 100
    else:
        vol = 0.0

    stats = {
        "ticker":       key,
        "latest":       round(latest, 2),
        "latest_date":  dates[-1],
        "first_date":   dates[0],
        "years":        round(len(closes) / 12, 1),
        "ath":          round(ath, 2),
        "atl":          round(atl, 2),
        "pct_from_ath": round((latest / ath - 1.0) * 100, 1) if ath else 0.0,
        "return_1y":    None if ret_over(12) is None else round(ret_over(12), 1),
        "return_5y":    None if ret_over(60) is None else round(ret_over(60), 1),
        "return_all":   round((latest / closes[0] - 1.0) * 100, 1) if closes[0] else 0.0,
        "max_drawdown": round(max_dd * 100, 1),
        "volatility":   round(vol, 1),
        "spark":        _downsample(closes),
    }
    _CACHE[key] = (now, stats)
    return stats
