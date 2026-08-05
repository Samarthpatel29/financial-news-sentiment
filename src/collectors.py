"""collectors — merged from 9 modules for a simpler layout."""
from __future__ import annotations

# ======================================================================
# from collectors/rss_collector.py
# ======================================================================

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


# ======================================================================
# from collectors/scraper_collector.py
# ======================================================================

import asyncio
import datetime
import logging
from typing import List

import aiohttp
import feedparser
from bs4 import BeautifulSoup

from config.settings import SCRAPER_TARGETS, SCRAPER_TIMEOUT, SCRAPER_USER_AGENT

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


# ======================================================================
# from collectors/broker_collector.py
# ======================================================================

"""
Free-tier news collectors replacing the broker API feeds.

  FinnhubCollector  — finnhub.io    (60 calls/min free, no credit card)
  NewsAPICollector  — newsapi.org   (100 req/day free developer plan)

Both are commonly used in student/academic finance projects.
"""
import datetime
import logging
from typing import List

import certifi
import requests

from config.settings import FINNHUB_API_KEY, NEWSAPI_KEY

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


# ======================================================================
# from collectors/stocktwits_collector.py
# ======================================================================

"""
StockTwits trending-stream collector.

Free public API, no key required — the zero-cost alternative to the Twitter/X
API for "tweets' sentiment". Each trending message becomes a RawArticle whose
body is the message text; cashtags ($AAPL) flow straight into the existing
3-pass ticker extractor.

Rate limit: 200 req/hr unauthenticated. We make exactly 1 request per pipeline
cycle (max 60/hr), so we stay well under it.
"""
import datetime
import json
import logging
import subprocess
from typing import List

from config.settings import STOCKTWITS_TRENDING_URL, STOCKTWITS_ENABLED

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


# ======================================================================
# from collectors/edgar_collector.py
# ======================================================================

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


# ======================================================================
# from collectors/edgar_extractor.py
# ======================================================================

"""
SEC filing section extractor — Phase B of the long-term fundamentals engine.

Filings are huge (a 10-K can be 300+ pages), so we download the primary document
and pull only the high-signal sections per form type:

  10-K (annual)   -> Risk Factors (Item 1A) + MD&A (Item 7)
  10-Q (earnings) -> MD&A (Item 2) + results-of-operations text
  8-K  (contract) -> Item 1.01 material agreement / Item 2.02 results / body

Everything is capped to a few thousand characters before scoring so FinBERT and
Groq stay fast and within the free tier. Pure stdlib + BeautifulSoup (free).
"""
import logging
import re

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_HEADERS_EXT = {
    "User-Agent": "SentimentIQ Research (academic project; shp5246@psu.edu)",
    "Accept-Encoding": "gzip, deflate",
}

MAX_CHARS = 6000   # cap fed to scoring per filing

# Anchor phrases that mark the start of high-signal sections, by section kind.
_ANCHORS = {
    "annual": [
        "risk factors",
        "management's discussion and analysis",
        "management’s discussion and analysis",
    ],
    "earnings": [
        "management's discussion and analysis",
        "management’s discussion and analysis",
        "results of operations",
    ],
    "contract": [
        "item 1.01",
        "entry into a material definitive agreement",
        "item 2.02",
        "results of operations and financial condition",
    ],
}


def _clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "table"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _slice_around_anchors(text: str, kind: str) -> str:
    """Return text starting at the first matching section anchor, capped."""
    low = text.lower()
    best = None
    for anchor in _ANCHORS.get(kind, []):
        idx = low.find(anchor)
        # Skip a hit that's only in the table of contents (very early in doc)
        if idx != -1 and idx > 500:
            best = idx if best is None else min(best, idx)
    if best is not None:
        return text[best : best + MAX_CHARS]
    # Fallback: skip the cover page, take the first substantive chunk
    return text[500 : 500 + MAX_CHARS]


def extract_section(url: str, section_kind: str) -> str:
    """
    Download a filing's primary document and return the high-signal section text
    for scoring. Returns "" on any failure (caller skips gracefully).
    """
    try:
        resp = requests.get(url, headers=_HEADERS_EXT, timeout=20)
        if resp.status_code != 200 or not resp.text:
            return ""
    except Exception as exc:
        log.debug("Filing fetch failed [%s]: %s", url, exc)
        return ""

    text = _clean_text(resp.text)
    if len(text) < 200:
        return ""
    return _slice_around_anchors(text, section_kind)


# ======================================================================
# from collectors/finviz_screener.py
# ======================================================================

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


# ======================================================================
# from collectors/finviz_verify.py
# ======================================================================

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
import re
import time

import requests

_UA_FV = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_URL = "https://finviz.com/quote.ashx?t={t}"
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL_FV = 1800   # 30 min

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
    if key in _CACHE and (now - _CACHE[key][0]) < _TTL_FV:
        data = dict(_CACHE[key][1])
    else:
        try:
            resp = requests.get(_URL.format(t=key), headers={"User-Agent": _UA_FV},
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


# ======================================================================
# from collectors/price_history.py
# ======================================================================

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


def get_candles(ticker: str, days: int = 520) -> list[dict]:
    """
    Daily OHLC candles for the candlestick chart (free, via yfinance). Pulls ~2
    years so the chart can zoom across timeframes (1M/3M/6M/1Y/2Y) client-side.
    Returns [{d, o, h, l, c}, ...] oldest→newest, or [] on failure.
    """
    key = ticker.upper().lstrip("$")
    now = time.time()
    if key in _CANDLE_CACHE and (now - _CANDLE_CACHE[key][0]) < _CANDLE_TTL:
        return _CANDLE_CACHE[key][1]
    try:
        import yfinance as yf
        hist = yf.Ticker(key).history(period="2y", interval="1d", auto_adjust=True)
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

