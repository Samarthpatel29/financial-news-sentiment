"""
Free-tier news collectors replacing the broker API feeds.

  FinnhubCollector  — finnhub.io    (60 calls/min free, no credit card)
  NewsAPICollector  — newsapi.org   (100 req/day free developer plan)

Both are commonly used in student/academic finance projects.
"""
from __future__ import annotations
import datetime
import logging
from typing import List

import certifi
import requests

from config.settings import FINNHUB_API_KEY, NEWSAPI_KEY
from .rss_collector import RawArticle

log = logging.getLogger(__name__)


class FinnhubCollector:
    """
    Pulls general market news from Finnhub's free tier.
    Sign up at finnhub.io — the API key is shown right on your dashboard.
    """

    BASE_URL = "https://finnhub.io/api/v1"

    def collect(self) -> List[RawArticle]:
        if not FINNHUB_API_KEY or FINNHUB_API_KEY == "PASTE_YOUR_FINNHUB_KEY_HERE":
            log.info("Finnhub key not set — skipping (add FINNHUB_API_KEY to .env)")
            return []

        articles: List[RawArticle] = []

        # General market news (category: general, forex, crypto, merger)
        for category in ("general", "merger"):
            try:
                resp = requests.get(
                    f"{self.BASE_URL}/news",
                    params={"category": category, "token": FINNHUB_API_KEY},
                    timeout=10,
                    verify=certifi.where(),
                )
                resp.raise_for_status()
                for item in resp.json()[:30]:
                    articles.append(RawArticle(
                        source=f"finnhub_{category}",
                        title=item.get("headline", ""),
                        url=item.get("url", ""),
                        body=item.get("summary", ""),
                        published=datetime.datetime.utcfromtimestamp(
                            item.get("datetime", 0) or 0
                        ),
                    ))
            except Exception as exc:
                log.warning("Finnhub [%s] error: %s", category, exc)

        log.debug("Finnhub → %d articles", len(articles))
        return articles


class NewsAPICollector:
    """
    Pulls financial headlines from NewsAPI's free developer plan.
    Sign up at newsapi.org — you get 100 requests/day free.
    """

    BASE_URL = "https://newsapi.org/v2"

    QUERIES = [
        "stock market",
        "earnings report",
        "federal reserve",
        "IPO merger acquisition",
    ]

    def collect(self) -> List[RawArticle]:
        if not NEWSAPI_KEY or NEWSAPI_KEY == "PASTE_YOUR_NEWSAPI_KEY_HERE":
            log.info("NewsAPI key not set — skipping (add NEWSAPI_KEY to .env)")
            return []

        articles: List[RawArticle] = []
        seen: set[str] = set()

        for query in self.QUERIES:
            try:
                resp = requests.get(
                    f"{self.BASE_URL}/everything",
                    verify=certifi.where(),
                    params={
                        "q":        query,
                        "language": "en",
                        "sortBy":   "publishedAt",
                        "pageSize": 10,
                        "apiKey":   NEWSAPI_KEY,
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                for item in resp.json().get("articles", []):
                    url = item.get("url", "")
                    if url in seen:
                        continue
                    seen.add(url)
                    articles.append(RawArticle(
                        source="newsapi",
                        title=item.get("title", "") or "",
                        url=url,
                        body=item.get("description", "") or "",
                        published=_parse_newsapi_date(item.get("publishedAt")),
                    ))
            except Exception as exc:
                log.warning("NewsAPI [%s] error: %s", query, exc)

        log.debug("NewsAPI → %d articles", len(articles))
        return articles


def _parse_newsapi_date(s: str | None) -> datetime.datetime:
    if not s:
        return datetime.datetime.utcnow()
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return datetime.datetime.utcnow()


class BrokerCollector:
    """Wraps all free-tier API collectors under one interface."""

    def __init__(self):
        self._finnhub  = FinnhubCollector()
        self._newsapi  = NewsAPICollector()

    def collect(self, tickers: list[str] | None = None) -> List[RawArticle]:
        articles = []
        articles.extend(self._finnhub.collect())
        articles.extend(self._newsapi.collect())
        return articles
