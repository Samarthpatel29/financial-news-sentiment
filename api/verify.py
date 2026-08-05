"""
Vercel serverless Finviz cross-check.

On the static (Vercel) site, this is what makes verification LIVE: the page's
signals are a snapshot, but this function fetches Finviz in real time so a
visitor can confirm our BUY/SELL/HOLD against current analyst data. Stdlib
only (no ML deps) so it fits Vercel comfortably.

    GET /api/verify?t=AAPL&signal=BUY
"""
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import re
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
FIELDS = ["Price", "Recom", "Target Price",
          "Perf Week", "Perf Month", "Perf Year", "Perf YTD"]


def _grab(html, label):
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


def _num(s):
    if not s:
        return None
    try:
        return float(s.replace("%", "").replace("$", "").replace(",", ""))
    except ValueError:
        return None


def _recom_signal(r):
    if r is None:
        return None
    return "BUY" if r <= 2.5 else "SELL" if r >= 3.5 else "HOLD"


def _agreement(ours, theirs):
    if theirs is None:
        return "UNKNOWN"
    if ours == theirs:
        return "AGREE"
    if "HOLD" in (ours, theirs):
        return "MIXED"
    return "DISAGREE"


def _fetch(ticker):
    url = f"https://finviz.com/quote.ashx?t={ticker}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except Exception:
        return None
    if "snapshot-td-content" not in html:
        return None
    price = _num(_grab(html, "Price"))
    recom = _num(_grab(html, "Recom"))
    target = _num(_grab(html, "Target Price"))
    upside = round((target / price - 1.0) * 100, 1) if (target and price) else None
    return {
        "ticker": ticker, "price": price, "recom": recom,
        "recom_signal": _recom_signal(recom),
        "target": target, "target_upside": upside,
        "perf_week": _grab(html, "Perf Week"),
        "perf_month": _grab(html, "Perf Month"),
        "perf_year": _grab(html, "Perf Year"),
        "perf_ytd": _grab(html, "Perf YTD"),
        "source_url": url,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        ticker = (q.get("t", [""])[0] or "").upper().lstrip("$")
        ticker = re.sub(r"[^A-Z.\-]", "", ticker)
        our = (q.get("signal", [""])[0] or "").upper()
        data = _fetch(ticker) if ticker else None
        if data and our:
            data["our_signal"] = our
            data["agreement"] = _agreement(our, data.get("recom_signal"))
        code = 200 if data else 404
        body = json.dumps(data or {"error": "finviz unavailable", "ticker": ticker}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "public, max-age=1800")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
