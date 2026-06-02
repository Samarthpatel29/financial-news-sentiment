from __future__ import annotations
import asyncio
import datetime
import logging
from typing import List

import aiohttp
import feedparser
from bs4 import BeautifulSoup

from config.settings import SCRAPER_TARGETS, SCRAPER_TIMEOUT, SCRAPER_USER_AGENT
from .rss_collector import RawArticle, _SSL

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": SCRAPER_USER_AGENT}


def _get_og_image(soup) -> str:
    """Extract Open Graph or Twitter card image from a BeautifulSoup page."""
    for attr, key in [("property", "og:image"), ("name", "twitter:image"),
                      ("property", "og:image:url"), ("itemprop", "image")]:
        tag = soup.find("meta", {attr: key})
        if tag and tag.get("content", "").startswith("http"):
            return tag["content"]
    return ""


class ScraperCollector:
    """HTML scraper for TradingView, FinViz, SEC EDGAR, and FDA."""

    async def _scrape_generic(
        self,
        session: aiohttp.ClientSession,
        name: str,
        cfg: dict,
    ) -> List[RawArticle]:
        # FDA exposes RSS — delegate to feedparser
        if "rss" in cfg:
            try:
                async with session.get(
                    cfg["rss"],
                    headers=HEADERS,
                    timeout=aiohttp.ClientTimeout(total=SCRAPER_TIMEOUT),
                ) as resp:
                    text = await resp.text()
                parsed = feedparser.parse(text)
                return [
                    RawArticle(
                        source=name,
                        title=e.get("title", ""),
                        url=e.get("link", ""),
                        body=e.get("summary", ""),
                        published=datetime.datetime.utcnow(),
                    )
                    for e in parsed.entries[:50]
                ]
            except Exception as exc:
                log.warning("Scraper RSS [%s]: %s", name, exc)
                return []

        url = cfg["url"].format(date=datetime.date.today().isoformat())
        try:
            async with session.get(
                url,
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=SCRAPER_TIMEOUT),
            ) as resp:
                html = await resp.text()
        except Exception as exc:
            log.warning("Scraper fetch [%s]: %s", name, exc)
            return []

        soup = BeautifulSoup(html, "lxml")
        page_og_image = _get_og_image(soup)   # fallback: page-level OG image
        articles: List[RawArticle] = []
        for row in soup.select(cfg["article_sel"])[:50]:
            title_tag = row.select_one(cfg["title_sel"])
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            href  = title_tag.get("href", "")
            if href and not href.startswith("http"):
                from urllib.parse import urlparse, urljoin
                href = urljoin(url, href)
            # row-level image first, then page og, then empty
            row_img = ""
            img_tag = row.find("img")
            if img_tag:
                row_img = img_tag.get("src", "") or img_tag.get("data-src", "")
                if row_img and not row_img.startswith("http"):
                    row_img = ""
            articles.append(RawArticle(
                source=name,
                title=title,
                url=href,
                body="",
                published=datetime.datetime.utcnow(),
                image_url=row_img or page_og_image,
            ))
        log.debug("Scraper [%s] → %d articles", name, len(articles))
        return articles

    async def collect_async(self) -> List[RawArticle]:
        connector = aiohttp.TCPConnector(limit=10, ssl=_SSL)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = [
                self._scrape_generic(session, name, cfg)
                for name, cfg in SCRAPER_TARGETS.items()
            ]
            results = await asyncio.gather(*tasks)
        return [a for batch in results for a in batch]

    def collect(self) -> List[RawArticle]:
        return asyncio.run(self.collect_async())
