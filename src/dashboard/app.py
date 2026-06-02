from __future__ import annotations
import json
import logging
import os
import time

from flask import Flask, Response, render_template, stream_with_context
from sqlalchemy import create_engine, desc, func
from sqlalchemy.orm import Session

from config.settings import DATABASE_URL, DASHBOARD_REFRESH, DASHBOARD_TOP_N
from src.storage.models import SentimentResult, TickerSentiment, Base

log = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates")

TIER1 = {"reuters", "dow_jones", "sec_edgar", "fda"}

# Domain used to build favicon URL for each source
SOURCE_DOMAINS = {
    "reuters":         "reuters.com",
    "dow_jones":       "wsj.com",
    "cnbc":            "cnbc.com",
    "marketwatch":     "marketwatch.com",
    "pr_newswire":     "prnewswire.com",
    "access_wires":    "accesswire.com",
    "finance_wire":    "financewire.net",
    "global_newswire": "globenewswire.com",
    "yahoo_finance":   "finance.yahoo.com",
    "seeking_alpha":   "seekingalpha.com",
    "fda":             "fda.gov",
    "sec_edgar":       "sec.gov",
    "tradingview":     "tradingview.com",
    "finviz":          "finviz.com",
    "finnhub_general": "finnhub.io",
    "finnhub_merger":  "finnhub.io",
    "newsapi":         "newsapi.org",
}

SOURCE_LABELS = {
    "reuters": "Reuters", "dow_jones": "Dow Jones", "cnbc": "CNBC",
    "marketwatch": "MarketWatch", "pr_newswire": "PR Newswire",
    "access_wires": "ACCESS Wires", "finance_wire": "FinanceWire",
    "global_newswire": "GlobeNewswire", "yahoo_finance": "Yahoo Finance",
    "seeking_alpha": "Seeking Alpha", "fda": "FDA", "sec_edgar": "SEC EDGAR",
    "tradingview": "TradingView", "finviz": "FinViz",
    "finnhub_general": "Finnhub", "finnhub_merger": "Finnhub M&A",
    "newsapi": "NewsAPI",
}


def _make_session() -> Session:
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _row_to_dict(r: SentimentResult, rank: int) -> dict:
    method = "FinBERT" if r.finbert_label else "VADER"
    source = r.source or ""
    domain = SOURCE_DOMAINS.get(source, "")
    favicon = f"https://www.google.com/s2/favicons?domain={domain}&sz=64" if domain else ""
    return {
        "rank":            rank,
        "id":              r.id,
        "source":          source,
        "source_label":    SOURCE_LABELS.get(source, source.title()),
        "source_domain":   domain,
        "source_favicon":  favicon,
        "tier":            1 if source in TIER1 else 2,
        "title":           r.title,
        "url":             r.url or "#",
        "image_url":       getattr(r, "image_url", "") or "",
        "sentiment_score": round(r.sentiment_score, 3),
        "message_density": int(r.message_density),
        "rank_score":      round(r.rank_score, 3),
        "trust_weight":    round(getattr(r, "trust_weight", 1.0) or 1.0, 2),
        "time_weight":     round(getattr(r, "time_weight",  1.0) or 1.0, 3),
        "tickers":         getattr(r, "tickers", "") or "",
        "method":          method,
        "label": r.finbert_label or (
            "positive" if r.sentiment_score > 0.05
            else "negative" if r.sentiment_score < -0.05
            else "neutral"
        ),
        "published": r.published.strftime("%H:%M:%S") if r.published else "—",
        "scored_at": r.scored_at.strftime("%H:%M:%S") if r.scored_at else "—",
    }


def _ranked_rows(db: Session) -> list[dict]:
    rows = (
        db.query(SentimentResult)
        .order_by(desc(SentimentResult.rank_score))
        .limit(DASHBOARD_TOP_N)
        .all()
    )
    return [_row_to_dict(r, i + 1) for i, r in enumerate(rows)]


def _get_stats(db: Session) -> dict:
    total = db.query(func.count(SentimentResult.id)).scalar() or 0
    pos   = db.query(func.count(SentimentResult.id))\
              .filter(SentimentResult.sentiment_score >  0.05).scalar() or 0
    neg   = db.query(func.count(SentimentResult.id))\
              .filter(SentimentResult.sentiment_score < -0.05).scalar() or 0
    neu   = total - pos - neg
    sources = db.query(SentimentResult.source).distinct().count()
    return {"total": total, "bullish": pos, "bearish": neg,
            "neutral": neu, "sources": sources}


def _ranked_tickers(db: Session) -> list[dict]:
    """Return all TickerSentiment rows ordered by |composite_score| × article_count."""
    rows = db.query(TickerSentiment).all()
    if not rows:
        return []

    def _ticker_rank(t: TickerSentiment) -> float:
        return abs(t.composite_score) * t.article_count * t.avg_trust

    rows_sorted = sorted(rows, key=_ticker_rank, reverse=True)

    out = []
    for i, t in enumerate(rows_sorted, 1):
        score = t.composite_score
        if score > 0.05:
            label = "positive"
        elif score < -0.05:
            label = "negative"
        else:
            label = "neutral"
        out.append({
            "rank":           i,
            "ticker":         t.ticker,
            "composite_score": round(score, 4),
            "label":          label,
            "article_count":  t.article_count,
            "bullish_count":  t.bullish_count,
            "bearish_count":  t.bearish_count,
            "neutral_count":  t.neutral_count,
            "avg_trust":      round(t.avg_trust, 2),
            "top_headline":   t.top_headline or "—",
            "top_source":     SOURCE_LABELS.get(t.top_source, (t.top_source or "").title()),
            "top_url":        t.top_url or "#",
            "last_updated":   t.last_updated.strftime("%H:%M:%S") if t.last_updated else "—",
        })
    return out


def _get_narrative() -> str:
    try:
        with open("data/narrative.txt") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "Groq AI narrative will appear here after the first pipeline cycle."


@app.route("/")
def index():
    db      = _make_session()
    rows    = _ranked_rows(db)
    stats   = _get_stats(db)
    tickers = _ranked_tickers(db)
    db.close()
    return render_template(
        "index.html",
        rows=rows, stats=stats, tickers=tickers,
        narrative=_get_narrative(),
        refresh=DASHBOARD_REFRESH,
        top_n=DASHBOARD_TOP_N,
    )


@app.route("/api/ranked")
def api_ranked():
    db   = _make_session()
    rows = _ranked_rows(db)
    db.close()
    return {"data": rows, "count": len(rows)}


@app.route("/api/tickers")
def api_tickers():
    db      = _make_session()
    tickers = _ranked_tickers(db)
    db.close()
    return {"data": tickers, "count": len(tickers)}


@app.route("/api/stats")
def api_stats():
    db    = _make_session()
    stats = _get_stats(db)
    db.close()
    stats["narrative"] = _get_narrative()
    return stats


@app.route("/stream")
def stream():
    def event_gen():
        db = _make_session()
        while True:
            try:
                rows      = _ranked_rows(db)
                tickers   = _ranked_tickers(db)
                stats     = _get_stats(db)
                narrative = _get_narrative()
                db.expire_all()
                payload = json.dumps({
                    "data": rows,
                    "tickers": tickers,
                    "stats": stats,
                    "narrative": narrative,
                })
                yield f"data: {payload}\n\n"
            except Exception as exc:
                log.warning("SSE error: %s", exc)
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            time.sleep(DASHBOARD_REFRESH)

    return Response(
        stream_with_context(event_gen()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.getenv("DASHBOARD_PORT", 5001))
    app.run(debug=False, port=port, threaded=True)
