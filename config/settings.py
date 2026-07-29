import os
from dotenv import load_dotenv

load_dotenv()

# ── Pipeline timing ────────────────────────────────────────────────────────────
PIPELINE_INTERVAL = int(os.getenv("PIPELINE_INTERVAL_SECONDS", 60))
E2E_DEADLINE      = int(os.getenv("E2E_DEADLINE_SECONDS", 120))

# ── Sentiment ──────────────────────────────────────────────────────────────────
FINBERT_MODEL    = "ProsusAI/finbert"   # downloads free from HuggingFace
SENTIMENT_BATCH  = int(os.getenv("SENTIMENT_BATCH_SIZE", 16))
FINBERT_MIN_CONF = 0.55                 # fall back to VADER below this

# ── RSS feeds (all free, no key needed) ───────────────────────────────────────
# NOTE: an audit of 80k stored rows found eight configured feeds that had never
# produced a single article (reuters, dow_jones, nasdaq, yahoo_finance,
# access_wires, plus the finviz/tradingview/sec_edgar scrapers). They are
# retained below under DEAD_FEEDS for the record, but are no longer polled —
# advertising sources that yield nothing overstated the pipeline's coverage.
RSS_FEEDS = {
    "cnbc":           "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "marketwatch":    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "pr_newswire":    "https://www.prnewswire.com/rss/news-releases-list.rss",
    "global_newswire":"https://www.globenewswire.com/RssFeed/subjectcode/23-Earnings",
    "seeking_alpha":  "https://seekingalpha.com/market_currents.xml",
    "fda":            "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
    "google_news":    "https://news.google.com/rss/search?q=stock+market+when:12h&hl=en-US&gl=US&ceid=US:en",
    "investing_com":  "https://www.investing.com/rss/news_25.rss",
    "business_insider":"https://markets.businessinsider.com/rss/news",
    "fortune":        "https://fortune.com/feed/fortune-feeds/?id=3230629",
    "reddit_stocks":  "https://www.reddit.com/r/stocks/hot/.rss?limit=40",
    "reddit_wsb":     "https://www.reddit.com/r/wallstreetbets/hot/.rss?limit=40",
    "reddit_investing":"https://www.reddit.com/r/investing/hot/.rss?limit=40",
}

# Feeds that returned nothing usable during the audit (2026-07-21). Re-test one
# of these before moving it back into RSS_FEEDS.
DEAD_FEEDS = {
    "reuters":        "https://news.google.com/rss/search?q=when:24h+allinurl:reuters.com&hl=en-US&gl=US&ceid=US:en",
    "dow_jones":      "https://www.wsj.com/xml/rss/3_7085.xml",
    "nasdaq":         "https://news.google.com/rss/search?q=when:24h+allinurl:nasdaq.com&hl=en-US&gl=US&ceid=US:en",
    "yahoo_finance":  "https://finance.yahoo.com/rss/topstories",
    "access_wires":   "https://www.accesswire.com/rss",
    "finance_wire":   "https://www.financewire.net/feed/",   # last article 2026-06-28
    "benzinga":       "https://www.benzinga.com/feed",       # last article 2026-07-02
    "cnn_business":   "http://rss.cnn.com/rss/money_latest.rss",  # CNN retired RSS; 1 row ever
}

# ── StockTwits — free public API, the closest free alternative to Twitter/X ───
# for "tweets' sentiment" (X API costs $100+/mo, StockTwits is $0, no key)
STOCKTWITS_TRENDING_URL = "https://api.stocktwits.com/api/2/streams/trending.json"
STOCKTWITS_ENABLED = os.getenv("STOCKTWITS_ENABLED", "1") == "1"

MAX_ARTICLES_PER_SOURCE = int(os.getenv("MAX_ARTICLES_PER_SOURCE", 50))

# ── Scraper targets (all free, no key needed) ──────────────────────────────────
# All three scraper targets produced zero stored articles across 80k rows in the
# 2026-07-21 audit — the two sites bot-block the scraper and the SEC entry was
# never a real endpoint (`search-index` with an unsubstituted `{date}`
# placeholder). Polling them every 60s cost requests and yielded nothing, so
# scraping is off by default. SEC filings are already ingested properly via
# src/collectors/edgar_collector.py.
SCRAPER_TARGETS: dict[str, dict] = {}

DEAD_SCRAPERS = {
    "finviz":      {"url": "https://finviz.com/news.ashx",
                    "article_sel": "tr.nn", "title_sel": "a.nn-tab-link"},
    "tradingview": {"url": "https://www.tradingview.com/news/",
                    "article_sel": "article", "title_sel": "a"},
    # broken endpoint — use edgar_collector.py instead
    "sec_edgar":   {"url": "https://efts.sec.gov/LATEST/search-index?q=%228-K%22&forms=8-K&startdt={date}",
                    "article_sel": "div.hit", "title_sel": "a.preview-file"},
}

SCRAPER_TIMEOUT    = 10
# Identify the bot honestly with a contact address (SEC requires this, and it is
# the polite convention for everyone else).
SCRAPER_CONTACT    = os.getenv("SCRAPER_CONTACT", "samarthpatel2908@gmail.com")
SCRAPER_USER_AGENT = f"FinSentimentBot/1.0 (IST495 academic project; {SCRAPER_CONTACT})"

# ── Free API keys ─────────────────────────────────────────────────────────────
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")
FINNHUB_API_KEY  = os.getenv("FINNHUB_API_KEY", "")
NEWSAPI_KEY      = os.getenv("NEWSAPI_KEY", "")

# ── Groq / CrewAI ─────────────────────────────────────────────────────────────
# llama-3.1-8b-instant is fast and free on Groq's free tier
CREW_LLM_MODEL   = "groq/llama-3.1-8b-instant"

# The chatbot is held to a different standard than the crew: it answers open-ended
# questions about a specific stock, so it needs real reasoning rather than raw
# speed. 70b-versatile is also free on Groq, just with a smaller rate limit.
CHAT_LLM_MODEL   = "groq/llama-3.3-70b-versatile"

# Per-headline news sentiment. FinBERT (local, free, offline) is the base; when a
# free Groq key is present we prefer this LLM for the nuance FinBERT misses
# ("cuts costs" bullish vs "cuts guidance" bearish). Batched to stay within the
# free tier. Set USE_LLM_SENTIMENT=0 to force the fully-offline FinBERT/VADER path.
NEWS_LLM_MODEL     = "groq/llama-3.3-70b-versatile"
USE_LLM_SENTIMENT  = os.getenv("USE_LLM_SENTIMENT", "1") == "1"

# ── Storage ────────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/sentiment.db")

# ── Source trust weights ───────────────────────────────────────────────────────
# Tier 1 = high-credibility institutional sources → weight 1.0
# Tier 2 = everything else → weight 0.75
# Only sources that actually deliver articles belong here. The previous list
# gave Reuters/Dow Jones/SEC weight 1.0, but none of those feeds ever produced a
# row — so 0.26% of the corpus was Tier 1 and the weighting did nothing.
SOURCE_TRUST: dict[str, float] = {
    "fda":            1.0,   # official FDA press releases
    "pr_newswire":    0.9,   # primary-source company releases
    "global_newswire":0.9,
    "cnbc":           0.85,
    "marketwatch":    0.85,
    "seeking_alpha":  0.8,
    # Social/aggregated content is explicitly discounted
    "stocktwits":     0.4,
    "reddit_stocks":  0.4,
    "reddit_wsb":     0.3,
    "reddit_investing": 0.4,
}
DEFAULT_TRUST_WEIGHT = 0.75   # Tier 2

# ── Time-decay weighting ───────────────────────────────────────────────────────
# Exponential decay: weight = exp(-ln2 / half_life * hours_old)
# Default half-life = 24 h → 1-day-old article has weight 0.5
TIME_DECAY_HALFLIFE_HOURS = float(os.getenv("TIME_DECAY_HALFLIFE_HOURS", 24))

# ── Ticker aggregation window ─────────────────────────────────────────────────
# Only articles from the last N hours are included in per-ticker aggregation.
# 168h = one week: the prediction engine reads "financial news for the week".
TICKER_WINDOW_HOURS = int(os.getenv("TICKER_WINDOW_HOURS", 168))

# Minimum articles before a ticker's composite score is published. Below this
# a single headline can pin a ticker at ±0.95 and outrank well-sampled names.
MIN_ARTICLES_PER_TICKER = int(os.getenv("MIN_ARTICLES_PER_TICKER", 3))

# Sources that are public chatter rather than reported news. These are scored
# and displayed, but kept OUT of the headline sentiment composite: they supply
# ~90% of all rows, so averaging them together with news drowned the actual
# reporting. PayPal's $53B buyout coverage was 1 row against 287 retail posts
# whose mean was +0.008 — pure noise that flipped the ticker's sign.
SOCIAL_SOURCES: set[str] = {
    "stocktwits", "reddit_stocks", "reddit_wsb", "reddit_investing",
}

# ── Prediction engine (0 external AI calls needed) ────────────────────────────
# Groq summaries are optional flavor text; set to "0" to run fully local
# (FinBERT scores everything on-device, SEC/RSS are plain free downloads).
USE_GROQ_SUMMARIES = os.getenv("USE_GROQ_SUMMARIES", "1") == "1"
# How many historical 10-K/10-Q filings per company to score for the all-time
# report trajectory (16 ≈ 4 years of quarterly+annual reports).
REPORT_HISTORY_MAX_FILINGS = int(os.getenv("REPORT_HISTORY_MAX_FILINGS", 16))

# ── Dashboard ──────────────────────────────────────────────────────────────────
DASHBOARD_REFRESH = PIPELINE_INTERVAL
DASHBOARD_TOP_N   = 25
