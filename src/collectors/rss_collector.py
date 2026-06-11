from __future__ import annotations
import asyncio
import datetime
import logging
import re
from dataclasses import dataclass, field
from typing import List

import ssl
import aiohttp
import certifi
import feedparser

_SSL = ssl.create_default_context(cafile=certifi.where())

from config.settings import RSS_FEEDS, MAX_ARTICLES_PER_SOURCE

log = logging.getLogger(__name__)

# ── Google News URL decoding ──────────────────────────────────────────────────
# Google News RSS links are redirects (news.google.com/rss/articles/...).
# They hide the real article URL, which breaks OG-image scraping and gives
# users an ugly redirect. Google's own batchexecute endpoint decodes them.
# Cache: encoded URL → decoded URL, so each article is decoded exactly once
# per process lifetime.
_GN_CACHE: dict[str, str] = {}
_GN_SEMAPHORE = asyncio.Semaphore(8)
_GN_SIG_RE = re.compile(r'data-n-a-sg="([^"]+)"')
_GN_TS_RE  = re.compile(r'data-n-a-ts="([^"]+)"')
_GN_ID_RE  = re.compile(r"/articles/([^?]+)")
_GN_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


async def _decode_gnews_url(session: aiohttp.ClientSession, article: "RawArticle") -> None:
    """Resolve a news.google.com redirect to the real article URL (in place)."""
    enc = article.url
    if enc in _GN_CACHE:
        if _GN_CACHE[enc]:
            article.url = _GN_CACHE[enc]
        return

    async with _GN_SEMAPHORE:
        try:
            async with session.get(enc, timeout=aiohttp.ClientTimeout(total=8),
                                   headers={"User-Agent": _GN_UA}) as resp:
                page = await resp.text()
            sig = _GN_SIG_RE.search(page)
            ts  = _GN_TS_RE.search(page)
            gn_id = _GN_ID_RE.search(enc)
            if not (sig and ts and gn_id):
                _GN_CACHE[enc] = ""
                return
            payload = (
                '[[["Fbv4je","[\\"garturlreq\\",[[\\"X\\",\\"X\\",[\\"X\\",\\"X\\"],'
                'null,null,1,1,\\"US:en\\",null,1,null,null,null,null,null,0,1],'
                '\\"X\\",\\"X\\",1,[1,1,1],1,1,null,0,0,null,0],'
                f'\\"{gn_id.group(1)}\\",{ts.group(1)},\\"{sig.group(1)}\\"]",'
                'null,"generic"]]]'
            )
            async with session.post(
                "https://news.google.com/_/DotsSplashUi/data/batchexecute",
                headers={"User-Agent": _GN_UA,
                         "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                data={"f.req": payload},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp2:
                body = await resp2.text()
            if "garturlres" in body:
                m = re.search(r'https?://[^"\\]+', body.split("garturlres", 1)[1])
                if m:
                    _GN_CACHE[enc] = m.group(0)
                    article.url = m.group(0)
                    return
            _GN_CACHE[enc] = ""
        except Exception:
            _GN_CACHE[enc] = ""


# Concurrency cap for OG-image scraping (don't hammer article sites)
_OG_SEMAPHORE = asyncio.Semaphore(10)
# Browser UA — bot UAs get blocked or served stripped HTML by many sites
_OG_HEADERS = {
    "User-Agent": _GN_UA,
    "Accept": "text/html",
}
_OG_TIMEOUT = aiohttp.ClientTimeout(total=5)
# Regex to find og:image or twitter:image in <head> HTML
_OG_RE = re.compile(
    r'<meta[^>]+(?:property=["\']og:image["\']|name=["\']twitter:image["\'])[^>]+'
    r'content=["\']([^"\']+)["\']'
    r'|'
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
    r'(?:property=["\']og:image["\']|name=["\']twitter:image["\'])',
    re.IGNORECASE,
)


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
        connector = aiohttp.TCPConnector(limit=30, ssl=_SSL)
        # max_*_size raised: Yahoo Finance sends CSP headers > aiohttp's 8 KB
        # default limit, which kills the request with LineTooLong
        async with aiohttp.ClientSession(
            connector=connector,
            max_line_size=32_768,
            max_field_size=32_768,
        ) as session:
            tasks = [
                self._fetch_feed(session, name, url)
                for name, url in self._feeds.items()
            ]
            results = await asyncio.gather(*tasks)
            articles = [a for batch in results for a in batch]

            # Resolve Google News redirect URLs to real article URLs first,
            # so dedup keys on the real URL and OG scraping hits the article
            gnews = [a for a in articles if "news.google.com/rss/articles" in a.url]
            if gnews:
                await asyncio.gather(*[_decode_gnews_url(session, a) for a in gnews],
                                     return_exceptions=True)
                decoded = sum(1 for a in gnews if "news.google.com" not in a.url)
                if decoded:
                    log.info("Google News decoder resolved %d/%d URLs", decoded, len(gnews))

            # Enrich articles that have no image by scraping OG tags
            no_img = [a for a in articles if not a.image_url and a.url.startswith("http")]
            if no_img:
                og_tasks = [_fetch_og_image(session, a) for a in no_img]
                await asyncio.gather(*og_tasks, return_exceptions=True)
                enriched = sum(1 for a in no_img if a.image_url)
                if enriched:
                    log.info("OG scraper enriched %d/%d articles with images", enriched, len(no_img))

        return articles

    def collect(self) -> List[RawArticle]:
        return asyncio.run(self.collect_async())


async def _fetch_og_image(session: aiohttp.ClientSession, article: "RawArticle") -> None:
    """Fetch the article page and extract og:image / twitter:image into article.image_url."""
    async with _OG_SEMAPHORE:
        try:
            async with session.get(
                article.url, timeout=_OG_TIMEOUT,
                headers=_OG_HEADERS, allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    return
                # Read first 120 KB — heavy pages (Yahoo Finance) bury
                # og:image past 60 KB of inlined scripts
                chunk = await resp.content.read(120_000)
                text = chunk.decode("utf-8", errors="ignore")
        except Exception:
            return

    m = _OG_RE.search(text)
    if m:
        img_url = (m.group(1) or m.group(2) or "").strip()
        if img_url.startswith("http"):
            article.image_url = img_url


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
