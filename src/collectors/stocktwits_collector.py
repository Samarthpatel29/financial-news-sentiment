"""
StockTwits trending-stream collector.

Free public API, no key required — the zero-cost alternative to the Twitter/X
API for "tweets' sentiment". Each trending message becomes a RawArticle whose
body is the message text; cashtags ($AAPL) flow straight into the existing
3-pass ticker extractor.

Rate limit: 200 req/hr unauthenticated. We make exactly 1 request per pipeline
cycle (max 60/hr), so we stay well under it.
"""
from __future__ import annotations
import datetime
import json
import logging
import subprocess
from typing import List

from config.settings import STOCKTWITS_TRENDING_URL, STOCKTWITS_ENABLED
from src.collectors.rss_collector import RawArticle

log = logging.getLogger(__name__)

# Cloudflare blocks Python's TLS fingerprint (requests/aiohttp get 403)
# but curl's fingerprint passes — so we shell out to curl.
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


class StockTwitsCollector:
    """Fetch trending messages from StockTwits' free public API."""

    def collect(self) -> List[RawArticle]:
        if not STOCKTWITS_ENABLED:
            return []
        try:
            out = subprocess.run(
                ["curl", "-s", "--max-time", "10", "-H", f"User-Agent: {_UA}",
                 STOCKTWITS_TRENDING_URL],
                capture_output=True, text=True, timeout=15,
            )
            messages = json.loads(out.stdout).get("messages", [])
        except Exception as exc:
            log.warning("StockTwits fetch failed: %s", exc)
            return []

        articles: List[RawArticle] = []
        for m in messages:
            body = m.get("body", "")
            if not body:
                continue
            symbols = [s.get("symbol", "") for s in m.get("symbols", [])]
            # Prefix cashtags so the $TICKER extraction pass catches them
            cashtags = " ".join(f"${s}" for s in symbols if s)
            user = (m.get("user") or {}).get("username", "user")

            created = m.get("created_at", "")
            try:
                published = datetime.datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, TypeError):
                published = datetime.datetime.utcnow()

            articles.append(RawArticle(
                source="stocktwits",
                title=f"@{user}: {body[:120]}",
                url=f"https://stocktwits.com/{user}/message/{m.get('id','')}",
                body=f"{cashtags} {body}".strip(),
                published=published,
                image_url=(m.get("entities") or {}).get("chart", {}).get("url", "") or "",
            ))

        log.info("StockTwits → %d trending messages", len(articles))
        return articles
