from __future__ import annotations
import asyncio
import datetime
import logging
from dataclasses import dataclass, field
from typing import List

import ssl
import aiohttp
import certifi
import feedparser

_SSL = ssl.create_default_context(cafile=certifi.where())

from config.settings import RSS_FEEDS, MAX_ARTICLES_PER_SOURCE

log = logging.getLogger(__name__)


@dataclass
class RawArticle:
    source:    str
    title:     str
    url:       str
    body:      str
    published: datetime.datetime = field(default_factory=datetime.datetime.utcnow)
    image_url: str = ""


class RSSCollector:
    """Async parallel collection from all configured RSS feeds."""

    def __init__(self, feeds: dict[str, str] = RSS_FEEDS):
        self._feeds = feeds

    async def _fetch_feed(
        self,
        session: aiohttp.ClientSession,
        name: str,
        url: str,
    ) -> List[RawArticle]:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                text = await resp.text()
        except Exception as exc:
            log.warning("RSS fetch failed [%s]: %s", name, exc)
            return []

        parsed = feedparser.parse(text)
        articles: List[RawArticle] = []
        for entry in parsed.entries[:MAX_ARTICLES_PER_SOURCE]:
            published = _parse_time(entry)
            body = (
                entry.get("summary")
                or entry.get("description")
                or entry.get("content", [{}])[0].get("value", "")
            )
            articles.append(RawArticle(
                source=name,
                title=entry.get("title", ""),
                url=entry.get("link", ""),
                body=body,
                published=published,
                image_url=_get_media_image(entry),
            ))
        log.debug("RSS [%s] → %d articles", name, len(articles))
        return articles

    async def collect_async(self) -> List[RawArticle]:
        connector = aiohttp.TCPConnector(limit=20, ssl=_SSL)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = [
                self._fetch_feed(session, name, url)
                for name, url in self._feeds.items()
            ]
            results = await asyncio.gather(*tasks)
        return [a for batch in results for a in batch]

    def collect(self) -> List[RawArticle]:
        return asyncio.run(self.collect_async())


def _get_media_image(entry) -> str:
    """Extract the best available image URL from an RSS entry."""
    # media:thumbnail (most common in news RSS)
    thumbs = getattr(entry, "media_thumbnail", None)
    if thumbs and isinstance(thumbs, list) and thumbs[0].get("url"):
        return thumbs[0]["url"]

    # media:content with image type
    content = getattr(entry, "media_content", None)
    if content and isinstance(content, list):
        for m in content:
            url = m.get("url", "")
            if url and (m.get("medium") == "image" or
                        any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"))):
                return url

    # enclosures (podcasts/images)
    for enc in getattr(entry, "enclosures", []):
        if enc.get("type", "").startswith("image/"):
            return enc.get("href") or enc.get("url", "")

    # img tag buried in summary HTML
    summary = entry.get("summary", "") or entry.get("description", "")
    if summary and "<img" in summary:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(summary, "html.parser")
            img = soup.find("img")
            if img and img.get("src", "").startswith("http"):
                return img["src"]
        except Exception:
            pass

    return ""


def _parse_time(entry) -> datetime.datetime:
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            return datetime.datetime(*entry.published_parsed[:6])
        except Exception:
            pass
    return datetime.datetime.utcnow()
