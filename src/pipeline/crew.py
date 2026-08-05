from __future__ import annotations
import datetime
import logging
import os
import re
import time
import concurrent.futures
from typing import List

from config.settings import GROQ_API_KEY, CREW_LLM_MODEL, E2E_DEADLINE, PIPELINE_INTERVAL
from src.collectors import RSSCollector, ScraperCollector, BrokerCollector, StockTwitsCollector
from src.sentiment import SentimentScorer
from src.storage.models import Article, SentimentResult, init_db
from src.pipeline.aggregator import aggregate_tickers

log = logging.getLogger(__name__)

# How far back to look when de-duplicating by headline text
_TITLE_DEDUP_HOURS = 48

_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE    = re.compile(r"\s+")


def _title_key(title: str) -> str:
    """
    Normalised headline used for cross-source de-duplication.

    The same story arrives via Google News, Yahoo and the publisher's own feed
    under three different URLs, so URL-only dedup let it through three times —
    inflating message_density and every affected ticker's article_count.
    """
    return _WS_RE.sub(" ", _PUNCT_RE.sub("", (title or "").lower())).strip()


def _make_crew():
    """
    Build a CrewAI crew that uses Groq's free tier (llama-3.1-8b-instant).
    Returns None gracefully if crewai or groq are not installed / key missing.
    """
    if not GROQ_API_KEY or GROQ_API_KEY == "PASTE_YOUR_GROQ_KEY_HERE":
        log.info("GROQ_API_KEY not set — narrative summaries disabled")
        return None
    try:
        from crewai import Agent, Crew, Task, LLM
    except ImportError:
        log.warning("crewai not installed — narrative summaries disabled")
        return None

    llm = LLM(
        model=CREW_LLM_MODEL,       # "groq/llama-3.1-8b-instant"
        api_key=GROQ_API_KEY,
    )

    analyst = Agent(
        role="Financial News Analyst",
        goal=(
            "Synthesize the top-ranked financial news items into a concise "
            "market narrative. Highlight the strongest bullish and bearish signals."
        ),
        backstory=(
            "You are a CFA-level analyst who distils real-time news into "
            "actionable market intelligence in under 80 words."
        ),
        llm=llm,
        verbose=False,
    )

    summarize_task = Task(
        description=(
            "Given these ranked news items (title | source | sentiment | rank), "
            "write a market summary under 80 words:\n\n{ranked_items}"
        ),
        expected_output="A market narrative under 80 words.",
        agent=analyst,
    )

    return Crew(agents=[analyst], tasks=[summarize_task], verbose=False)


class SentimentCrew:
    """
    Runs the full pipeline every PIPELINE_INTERVAL seconds:
      collect  →  deduplicate  →  score  →  persist  →  (optional) Groq narrative
    """

    def __init__(self):
        self._rss        = RSSCollector()
        self._scraper    = ScraperCollector()
        self._broker     = BrokerCollector()
        self._stocktwits = StockTwitsCollector()
        self._scorer     = SentimentScorer()
        self._db         = init_db()
        self._crew       = _make_crew()

    def run_cycle(self) -> List[SentimentResult]:
        t0 = time.monotonic()
        log.info("── Pipeline cycle started ──")

        # 1. Collect from all sources in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            rss_f     = pool.submit(self._rss.collect)
            scraper_f = pool.submit(self._scraper.collect)
            broker_f  = pool.submit(self._broker.collect)
            st_f      = pool.submit(self._stocktwits.collect)
            rss_arts     = rss_f.result()
            scraper_arts = scraper_f.result()
            broker_arts  = broker_f.result()
            st_arts      = st_f.result()

        all_articles = rss_arts + scraper_arts + broker_arts + st_arts
        log.info("Collected %d articles  (%.1fs)", len(all_articles), time.monotonic() - t0)

        if not all_articles:
            log.warning("No articles — skipping cycle")
            return []

        # 2. Deduplicate by URL *and* headline within this batch
        seen: set[str] = set()
        seen_titles: set[str] = set()
        unique = []
        for a in all_articles:
            tkey = _title_key(a.title)
            if not a.url or a.url in seen:
                continue
            if tkey and tkey in seen_titles:
                continue
            seen.add(a.url)
            if tkey:
                seen_titles.add(tkey)
            unique.append(a)
        log.info("After batch dedup: %d unique articles", len(unique))

        # 3. Filter out URLs already in the database (cross-cycle dedup)
        existing_urls: set[str] = {
            row[0] for row in
            self._db.query(SentimentResult.url)
            .filter(SentimentResult.url.in_([a.url for a in unique]))
            .all()
        }
        new_articles = [a for a in unique if a.url not in existing_urls]

        # 3a. Drop stories already stored under a different URL (same headline
        #     re-syndicated by another feed) within the recent window.
        if new_articles:
            since = datetime.datetime.utcnow() - datetime.timedelta(hours=_TITLE_DEDUP_HOURS)
            recent_titles = {
                _title_key(t)
                for (t,) in self._db.query(SentimentResult.title)
                                    .filter(SentimentResult.scored_at >= since)
                                    .all()
            }
            recent_titles.discard("")
            before = len(new_articles)
            new_articles = [a for a in new_articles
                            if _title_key(a.title) not in recent_titles]
            if before != len(new_articles):
                log.info("Dropped %d re-syndicated duplicates", before - len(new_articles))

        log.info("New articles not yet in DB: %d", len(new_articles))

        # 3b. Backfill images for existing rows that have none —
        #     feeds re-serve the same articles, so the fresh fetch often has
        #     an image the older DB row is missing.
        fresh_imgs = {a.url: a.image_url for a in unique if a.image_url}
        if existing_urls and fresh_imgs:
            rows_missing_img = (
                self._db.query(SentimentResult)
                .filter(SentimentResult.url.in_(list(existing_urls)))
                .filter((SentimentResult.image_url == "") | (SentimentResult.image_url.is_(None)))
                .all()
            )
            filled = 0
            for row in rows_missing_img:
                img = fresh_imgs.get(row.url)
                if img:
                    row.image_url = img
                    filled += 1
            if filled:
                self._db.commit()
                log.info("Backfilled images on %d existing articles", filled)

        if not new_articles:
            # Still re-aggregate: composites carry live time decay and the
            # stale-ticker sweep, both of which must keep moving even on a
            # quiet cycle. Returning early here froze the board whenever the
            # feeds had nothing new.
            log.info("No new articles this cycle — re-aggregating only")
            aggregate_tickers(self._db)
            return []

        # 4. Score sentiment
        results = self._scorer.score_articles(new_articles, window_articles=all_articles)
        log.info("Scored %d articles  (%.1fs)", len(results), time.monotonic() - t0)

        # 5. Persist to SQLite.
        #    The articles table was never written, so every SentimentResult
        #    carried article_id = 0 and the article bodies were discarded after
        #    scoring. Store the article first, then point the score at its id.
        article_rows = [
            Article(
                source     = a.source,
                title      = a.title,
                url        = a.url,
                published  = a.published,
                body       = getattr(a, "body", "") or "",
            )
            for a in new_articles
        ]
        try:
            self._db.add_all(article_rows)
            self._db.flush()          # assigns primary keys without committing
            for result, row in zip(results, article_rows):
                result.article_id = row.id
        except Exception as exc:
            # Never let article bookkeeping cost us the sentiment scores
            self._db.rollback()
            log.warning("Article persistence failed (scores still saved): %s", exc)

        self._db.add_all(results)
        self._db.commit()

        # 5b. Per-ticker aggregation (fast — pure DB read/upsert)
        n_tickers = aggregate_tickers(self._db)
        log.info("Ticker aggregation: %d tickers updated  (%.1fs)", n_tickers, time.monotonic() - t0)

        # 6. Optional Groq narrative (only if budget allows)
        elapsed     = time.monotonic() - t0
        budget_left = E2E_DEADLINE - elapsed - 10
        if self._crew and budget_left > 15:
            top = sorted(results, key=lambda r: r.rank_score, reverse=True)[:10]
            ranked_str = "\n".join(
                f"{r.title[:70]} | {r.source} | {r.sentiment_score:.2f} | {r.rank_score:.2f}"
                for r in top
            )
            try:
                narrative = str(self._crew.kickoff(inputs={"ranked_items": ranked_str}))
                log.info("Groq narrative: %s", narrative[:200])
                os.makedirs("data", exist_ok=True)
                with open("data/narrative.txt", "w") as f:
                    f.write(narrative)
            except Exception as exc:
                log.warning("Groq narrative failed: %s", exc)

        total = time.monotonic() - t0
        log.info("Cycle done in %.1fs  (budget: %ds)", total, E2E_DEADLINE)
        if total > E2E_DEADLINE:
            log.error("E2E deadline exceeded: %.1fs > %ds", total, E2E_DEADLINE)

        return results
