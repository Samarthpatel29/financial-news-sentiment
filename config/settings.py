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
RSS_FEEDS = {
    "reuters":        "https://feeds.reuters.com/reuters/businessNews",
    "cnbc":           "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "marketwatch":    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "dow_jones":      "https://www.wsj.com/xml/rss/3_7085.xml",
    "pr_newswire":    "https://www.prnewswire.com/rss/news-releases-list.rss",
    "access_wires":   "https://www.accesswire.com/rss",
    "finance_wire":   "https://www.financewire.net/feed/",
    "global_newswire":"https://www.globenewswire.com/RssFeed/subjectcode/23-Earnings",
    "yahoo_finance":  "https://finance.yahoo.com/rss/topstories",
    "seeking_alpha":  "https://seekingalpha.com/market_currents.xml",
    "fda":            "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
}

MAX_ARTICLES_PER_SOURCE = int(os.getenv("MAX_ARTICLES_PER_SOURCE", 50))

# ── Scraper targets (all free, no key needed) ──────────────────────────────────
SCRAPER_TARGETS = {
    "finviz": {
        "url":         "https://finviz.com/news.ashx",
        "article_sel": "tr.nn",
        "title_sel":   "a.nn-tab-link",
    },
    "tradingview": {
        "url":         "https://www.tradingview.com/news/",
        "article_sel": "article",
        "title_sel":   "a",
    },
    "sec_edgar": {
        "url":         "https://efts.sec.gov/LATEST/search-index?q=%228-K%22&forms=8-K&dateRange=custom&startdt={date}",
        "article_sel": "div.hit",
        "title_sel":   "a.preview-file",
    },
}

SCRAPER_TIMEOUT    = 10
SCRAPER_USER_AGENT = "Mozilla/5.0 (compatible; FinSentimentBot/1.0)"

# ── Free API keys ─────────────────────────────────────────────────────────────
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")
FINNHUB_API_KEY  = os.getenv("FINNHUB_API_KEY", "")
NEWSAPI_KEY      = os.getenv("NEWSAPI_KEY", "")

# ── Groq / CrewAI ─────────────────────────────────────────────────────────────
# llama-3.1-8b-instant is fast and free on Groq's free tier
CREW_LLM_MODEL   = "groq/llama-3.1-8b-instant"

# ── Storage ────────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/sentiment.db")

# ── Source trust weights ───────────────────────────────────────────────────────
# Tier 1 = high-credibility institutional sources → weight 1.0
# Tier 2 = everything else → weight 0.75
SOURCE_TRUST: dict[str, float] = {
    "reuters":   1.0,
    "dow_jones": 1.0,
    "sec_edgar": 1.0,
    "fda":       1.0,
}
DEFAULT_TRUST_WEIGHT = 0.75   # Tier 2

# ── Time-decay weighting ───────────────────────────────────────────────────────
# Exponential decay: weight = exp(-ln2 / half_life * hours_old)
# Default half-life = 24 h → 1-day-old article has weight 0.5
TIME_DECAY_HALFLIFE_HOURS = float(os.getenv("TIME_DECAY_HALFLIFE_HOURS", 24))

# ── Ticker aggregation window ─────────────────────────────────────────────────
# Only articles from the last N hours are included in per-ticker aggregation
TICKER_WINDOW_HOURS = int(os.getenv("TICKER_WINDOW_HOURS", 48))

# ── Dashboard ──────────────────────────────────────────────────────────────────
DASHBOARD_REFRESH = PIPELINE_INTERVAL
DASHBOARD_TOP_N   = 25
