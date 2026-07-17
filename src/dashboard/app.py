from __future__ import annotations
import datetime
import json
import logging
import os
import time

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from sqlalchemy import create_engine, desc, func
from sqlalchemy.orm import Session

from config.settings import DATABASE_URL, DASHBOARD_REFRESH, DASHBOARD_TOP_N
from config.sectors import sector_of as _sector_of
from src.storage.models import SentimentResult, TickerSentiment, Base
from src.utils.market_hours import market_status
from src.dashboard import chatbot

log = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates")
# Re-read index.html on every request so edits show up on browser refresh
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

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
    "google_news":     "news.google.com",
    "nasdaq":          "nasdaq.com",
    "investing_com":   "investing.com",
    "benzinga":        "benzinga.com",
    "business_insider":"businessinsider.com",
    "cnn_business":    "cnn.com",
    "fortune":         "fortune.com",
    "reddit_stocks":   "reddit.com",
    "reddit_wsb":      "reddit.com",
    "reddit_investing":"reddit.com",
    "stocktwits":      "stocktwits.com",
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
    "google_news": "Google News", "nasdaq": "Nasdaq",
    "investing_com": "Investing.com", "benzinga": "Benzinga",
    "business_insider": "Business Insider", "cnn_business": "CNN Business",
    "fortune": "Fortune", "reddit_stocks": "r/stocks",
    "reddit_wsb": "r/wallstreetbets", "reddit_investing": "r/investing",
    "stocktwits": "StockTwits",
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
    import datetime
    import math
    from config.settings import TICKER_WINDOW_HOURS, TIME_DECAY_HALFLIFE_HOURS

    # Only the freshness window — rank_score is frozen at scoring time, so
    # without this cutoff stale articles would dominate the board forever.
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=TICKER_WINDOW_HOURS)
    rows = (
        db.query(SentimentResult)
        .filter(SentimentResult.scored_at >= cutoff)
        .order_by(desc(SentimentResult.rank_score))
        .limit(DASHBOARD_TOP_N * 4)
        .all()
    )

    # Re-apply time decay live so newer articles outrank equally-scored old ones
    now = datetime.datetime.utcnow()
    k = math.log(2) / TIME_DECAY_HALFLIFE_HOURS

    def _live_rank(r: SentimentResult) -> float:
        ref = r.published or r.scored_at or now
        hours_old = max((now - ref).total_seconds() / 3600, 0)
        return (r.rank_score or 0) * math.exp(-k * hours_old)

    rows = sorted(rows, key=_live_rank, reverse=True)[:DASHBOARD_TOP_N]
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
            "sector":         _sector_of(t.ticker),
        })
    return out


def _fundamental_rows(db: Session) -> list[dict]:
    """
    Long-term fundamentals screener: tickers ranked by their 7-day SEC-filing
    signal, each with the recent filings (form, verdict, Groq summary, link).
    """
    from src.storage.models import Filing
    rows = (db.query(TickerSentiment)
            .filter(TickerSentiment.filing_count_7d > 0)
            .all())
    if not rows:
        return []

    # Rank by the blended Continuation score (news + filings + price record)
    rows_sorted = sorted(rows, key=lambda t: (t.continuation_score or 0.0), reverse=True)
    out = []
    for i, t in enumerate(rows_sorted, 1):
        fs = (db.query(Filing)
              .filter(Filing.ticker == t.ticker)
              .order_by(Filing.filed_at.desc())
              .limit(6).all())
        filings = [{
            "form":      f.form_type,
            "kind":      f.section_kind,
            "filed":     f.filed_at.strftime("%b %d") if f.filed_at else "—",
            "verdict":   f.llm_verdict or "Stable",
            "score":     round(f.fundamental_score or 0.0, 3),
            "summary":   f.llm_summary or "",
            "url":       f.url or "#",
        } for f in fs]
        # Bull/bear strength from this week's news mix
        n_arts = max(1, (t.bullish_count or 0) + (t.bearish_count or 0) + (t.neutral_count or 0))
        bull_pct = round((t.bullish_count or 0) / n_arts * 100)
        bear_pct = round((t.bearish_count or 0) / n_arts * 100)

        # Confidence: how much evidence backs this prediction (data volume + agreement)
        n_filings = db.query(Filing).filter(Filing.ticker == t.ticker).count()
        evidence = min(40, n_filings * 3) + min(25, n_arts * 2)
        agreement = abs(t.continuation_score or 0.0) * 30
        confidence = min(95, round(35 + evidence * 0.6 + agreement))

        pred = t.continuation_score or 0.0
        signal = "BUY" if pred > 0.12 else "SELL" if pred < -0.12 else "HOLD"

        # Event types driving this signal (from the filing kinds present)
        kinds = {f.section_kind for f in fs}
        event_types = sorted(k for k in kinds if k)

        # Volatility tag from the all-time price record
        vol = t.price_volatility
        vol_tag = ("VOLATILE" if vol and vol >= 45 else
                   "STABLE" if vol and vol < 25 else
                   "MODERATE" if vol else "")

        out.append({
            "rank":     i,
            "ticker":   t.ticker,
            "score":    round(t.fundamental_score or 0.0, 3),
            "verdict":  t.fundamental_verdict or "Stable",
            "count":    t.filing_count_7d,
            "filing_total": n_filings,
            "last":     t.last_filing_at.strftime("%b %d") if t.last_filing_at else "—",
            # ── Prediction signal ──────────────────────────────────────────────
            "continuation":       round(pred, 3),
            "continuation_label": t.continuation_label or "Mixed",
            "signal":             signal,
            "confidence":         confidence,
            "uncertainty":        100 - confidence,
            "sector":             _sector_of(t.ticker),
            "event_types":        event_types,
            "vol_tag":            vol_tag,
            "volatility":         vol,
            "bull_pct":           bull_pct,
            "bear_pct":           bear_pct,
            "article_count":      t.article_count or 0,
            "headline":           t.top_headline or "",
            "headline_source":    SOURCE_LABELS.get(t.top_source or "", t.top_source or ""),
            "headline_url":       t.top_url or "",
            "news_score":         round(t.composite_score or 0.0, 3),
            "price_score":        round(t.price_score or 0.0, 3),
            "return_1y":          t.price_return_1y,
            "return_5y":          t.price_return_5y,
            "pct_from_ath":       t.pct_from_ath,
            "filings":            filings,
        })
    return out


def _signal_accuracy(db: Session) -> dict:
    """Honest self-score: % of graded signals (last 30d) that proved correct."""
    from src.storage.models import SignalHistory
    import datetime as _dt
    cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=30)
    scored = (db.query(SignalHistory)
              .filter(SignalHistory.correct.isnot(None),
                      SignalHistory.created_at >= cutoff)
              .all())
    if not scored:
        return {"pct": None, "n": 0}
    correct = sum(1 for s in scored if s.correct)
    return {"pct": round(correct / len(scored) * 100), "n": len(scored)}


@app.route("/api/fundamentals")
def api_fundamentals():
    db = _make_session()
    try:
        return jsonify({
            "data": _fundamental_rows(db),
            "accuracy": _signal_accuracy(db),
        })
    finally:
        db.close()


@app.route("/api/ticker-events/<ticker>")
def api_ticker_events(ticker):
    """Chronological timeline: SEC filings + this week's news for one ticker."""
    from src.storage.models import Filing
    sym = ticker.upper().lstrip("$")
    db = _make_session()
    try:
        events = []
        for f in (db.query(Filing).filter(Filing.ticker == sym)
                  .order_by(desc(Filing.filed_at)).limit(20).all()):
            events.append({
                "kind":    "filing",
                "date":    f.filed_at.strftime("%Y-%m-%d") if f.filed_at else "",
                "title":   f"{f.form_type} — {f.section_kind or 'filing'}",
                "detail":  f.llm_summary or "",
                "score":   round(f.fundamental_score or 0.0, 3),
                "url":     f.url or "",
            })
        week_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        arts = (db.query(SentimentResult)
                .filter(SentimentResult.tickers.like(f"%{sym}%"),
                        SentimentResult.published >= week_ago)
                .order_by(desc(SentimentResult.rank_score))
                .limit(15).all())
        for a in arts:
            events.append({
                "kind":    "news",
                "date":    a.published.strftime("%Y-%m-%d") if a.published else "",
                "title":   a.title,
                "detail":  SOURCE_LABELS.get(a.source, a.source),
                "score":   round(a.sentiment_score or 0.0, 3),
                "url":     a.url or "",
            })
        events.sort(key=lambda e: e["date"], reverse=True)
        return jsonify({"ticker": sym, "events": events})
    finally:
        db.close()


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


@app.route("/api/market-status")
def api_market_status():
    return jsonify(market_status())


@app.route("/api/price/<ticker>")
def api_price(ticker):
    """All-time price history + reliability stats (free, via yfinance)."""
    from src.collectors.price_history import get_price_stats
    stats = get_price_stats(ticker)
    if not stats:
        return jsonify({"error": "no price history", "ticker": ticker.upper()}), 404
    return jsonify(stats)


@app.route("/api/candles/<ticker>")
def api_candles(ticker):
    """Recent daily OHLC candles for the candlestick chart (free, via yfinance)."""
    from src.collectors.price_history import get_candles
    candles = get_candles(ticker)
    if not candles:
        return jsonify({"error": "no candles", "ticker": ticker.upper()}), 404
    return jsonify({"ticker": ticker.upper(), "candles": candles})


def _chat_context(db: Session) -> str:
    """Compact live snapshot to ground the chatbot in real dashboard data."""
    stats   = _get_stats(db)
    tickers = _ranked_tickers(db)[:6]
    rows    = _ranked_rows(db)[:5]
    ms      = market_status()

    mood = "neutral"
    if stats["total"]:
        if stats["bullish"] > stats["bearish"] * 1.2:
            mood = "mostly bullish (optimistic)"
        elif stats["bearish"] > stats["bullish"] * 1.2:
            mood = "mostly bearish (pessimistic)"

    lines = [
        f"Market status: {ms['label']}.",
        f"Overall mood: {mood}.",
        f"Articles analyzed: {stats['total']} "
        f"({stats['bullish']} bullish, {stats['bearish']} bearish, {stats['neutral']} neutral) "
        f"from {stats['sources']} sources.",
    ]
    if tickers:
        lines.append("Top tickers by sentiment right now: " + ", ".join(
            f"{t['ticker']} ({t['label']}, score {t['composite_score']})" for t in tickers
        ))
    if rows:
        lines.append("Top headlines right now:")
        for r in rows:
            lines.append(f"  - [{r['label']}] {r['title']} ({r['source_label']})")
    return "\n".join(lines)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    payload  = request.get_json(silent=True) or {}
    messages = payload.get("messages", [])
    mode     = payload.get("mode", "tutor")
    if mode not in ("tutor", "support"):
        mode = "tutor"
    if not isinstance(messages, list) or not messages:
        return jsonify({"reply": "Ask me anything about the dashboard or investing basics!"})

    # Sanitize to the fields the model needs
    clean = [
        {"role": m.get("role", "user"), "content": str(m.get("content", ""))[:2000]}
        for m in messages if m.get("content")
    ]

    db = _make_session()
    try:
        context = _chat_context(db)
    finally:
        db.close()

    reply = chatbot.chat(clean, context=context, mode=mode)
    return jsonify({"reply": reply})


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
                    "market": market_status(),
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
