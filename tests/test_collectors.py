"""Smoke tests for collectors (uses real network — mark as integration if needed)."""
from __future__ import annotations
import datetime
import pytest

from src.collectors.rss_collector import RSSCollector, RawArticle


class TestRSSCollector:
    def test_returns_list(self, monkeypatch):
        # Patch aiohttp so the test is offline
        import feedparser

        fake_rss = """<?xml version="1.0"?>
        <rss version="2.0"><channel>
          <item><title>Test Headline</title><link>https://example.com/1</link>
                <description>Some body text.</description></item>
        </channel></rss>"""

        async def fake_fetch(self, session, name, url):
            parsed = feedparser.parse(fake_rss)
            return [
                RawArticle(
                    source=name,
                    title=e.get("title", ""),
                    url=e.get("link", ""),
                    body=e.get("summary", ""),
                    published=datetime.datetime.utcnow(),
                )
                for e in parsed.entries
            ]

        monkeypatch.setattr(
            "src.collectors.rss_collector.RSSCollector._fetch_feed",
            fake_fetch,
        )
        collector = RSSCollector(feeds={"test_feed": "https://fake.example.com/rss"})
        articles = collector.collect()
        assert isinstance(articles, list)
        assert len(articles) == 1
        assert articles[0].source == "test_feed"
        assert articles[0].title == "Test Headline"

    def test_raw_article_fields(self):
        a = RawArticle(
            source="reuters",
            title="Fed raises rates",
            url="https://example.com/fed",
            body="The Federal Reserve raised interest rates by 25bps.",
        )
        assert a.source == "reuters"
        assert isinstance(a.published, datetime.datetime)
