from __future__ import annotations
import datetime
import json
import logging
import os
import time

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from sqlalchemy import create_engine, desc, func
from sqlalchemy.orm import Session

from config.settings import (
    DATABASE_URL, DASHBOARD_REFRESH, DASHBOARD_TOP_N,
    TICKER_WINDOW_HOURS, MIN_ARTICLES_PER_TICKER, SOURCE_TRUST,
    SIGNAL_BUY_THRESHOLD, SIGNAL_SELL_THRESHOLD,
)
from config.sectors import sector_of as _sector_of
from src.storage.models import SentimentResult, TickerSentiment, Base
from src.utils.market_hours import market_status
from src.dashboard import chatbot

log = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates")
# Re-read index.html on every request so edits show up on browser refresh
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# Derived from the live trust table so the badge can't drift away from the
# weighting again (it used to name Reuters/Dow Jones, which deliver nothing).
TIER1 = {s for s, w in SOURCE_TRUST.items() if w >= 0.9}

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
    "access_wires": "ACCESS Wires", "access_wire": "ACCESS Newswire",
    "business_wire": "Business Wire", "finance_wire": "FinanceWire",
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


# One engine for the process. It used to be built inside _make_session(), so
# every single request created a new engine (and re-ran create_all), leaking a
# connection pool per call. SQLAlchemy engines are thread-safe and pool
# internally — sessions are the per-request part.
_engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
Base.metadata.create_all(_engine)


def _make_session() -> Session:
    return Session(_engine)


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
    # Pull a big pool so that, after keeping only stock-linked articles, we still
    # fill the board. General market news ("Wall Street drifts…") is dropped.
    rows = (
        db.query(SentimentResult)
        .filter(SentimentResult.scored_at >= cutoff)
        .filter(SentimentResult.tickers.isnot(None), SentimentResult.tickers != "")
        .order_by(desc(SentimentResult.rank_score))
        .limit(DASHBOARD_TOP_N * 20)
        .all()
    )

    # Keep only articles about a stock actually listed on the site
    tracked = {t[0] for t in db.query(TickerSentiment.ticker).all()}
    if tracked:
        def _has_tracked(r: SentimentResult) -> bool:
            return any(t.strip() in tracked for t in (r.tickers or "").split(",") if t.strip())
        linked = [r for r in rows if _has_tracked(r)]
        rows = linked or rows   # fall back to any-ticker rows if none match

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
    """
    Full tracked-stock board. Stocks with fresh news (>= MIN_ARTICLES_PER_TICKER
    in the last TICKER_WINDOW_HOURS) are ranked on top by
    |composite_score| × article_count × avg_trust, carrying their live sentiment.

    Stocks WITHOUT fresh news are still shown — as neutral "No recent news" —
    so the board never collapses to a handful when the market is closed
    (overnight / weekends, when few articles publish). We deliberately do NOT
    surface a quiet stock's last-known score: an old score served as current is
    misleading (the reason the aggregator stopped doing exactly that), so quiet
    rows read neutral until fresh coverage arrives.
    """
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=TICKER_WINDOW_HOURS)
    all_rows = db.query(TickerSentiment).all()
    if not all_rows:
        return []

    def _is_active(t: TickerSentiment) -> bool:
        return ((t.article_count or 0) >= MIN_ARTICLES_PER_TICKER
                and t.last_updated is not None
                and t.last_updated >= cutoff)

    def _ticker_rank(t: TickerSentiment) -> float:
        return abs(t.composite_score or 0.0) * (t.article_count or 0) * (t.avg_trust or 0.0)

    active = sorted((t for t in all_rows if _is_active(t)), key=_ticker_rank, reverse=True)
    quiet  = sorted((t for t in all_rows if not _is_active(t)), key=lambda t: t.ticker)
    rows_sorted = list(active) + list(quiet)

    out = []
    for i, t in enumerate(rows_sorted, 1):
        fresh = _is_active(t)
        score = (t.composite_score or 0.0) if fresh else 0.0
        if not fresh:
            label = "neutral"
        elif score > 0.05:
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
            # Lets the UI mute quiet stocks and label them "No recent news"
            "has_recent_news": fresh,
            "article_count":  t.article_count if fresh else 0,
            "bullish_count":  t.bullish_count if fresh else 0,
            "bearish_count":  t.bearish_count if fresh else 0,
            "neutral_count":  t.neutral_count if fresh else 0,
            "avg_trust":      round(t.avg_trust or 0.0, 2),
            # Retail chatter, reported alongside but never mixed into the
            # news composite above
            "social_score":   round(t.social_score or 0.0, 4) if fresh else 0.0,
            "social_count":   (t.social_count or 0) if fresh else 0,
            "top_headline":   (t.top_headline or "—") if fresh else "No recent news",
            "top_source":     SOURCE_LABELS.get(t.top_source, (t.top_source or "").title()) if fresh else "—",
            "top_url":        (t.top_url or "#") if fresh else "#",
            # Date included: a time-only stamp made a three-week-old row look
            # like it had refreshed minutes ago.
            "last_updated":   t.last_updated.strftime("%Y-%m-%d %H:%M") if t.last_updated else "—",
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
        signal = ("BUY" if pred > SIGNAL_BUY_THRESHOLD
                  else "SELL" if pred < -SIGNAL_SELL_THRESHOLD else "HOLD")

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
            # ── The four components that now make up the rating ───────────────
            "comp_news":          None if t.composite_score is None else round(t.composite_score, 3),
            "comp_reports":       None if t.reports_signal is None else round(t.reports_signal, 3),
            "comp_momentum":      None if t.momentum_signal is None else round(t.momentum_signal, 3),
            "comp_analysts":      None if t.analyst_signal is None else round(t.analyst_signal, 3),
            "analyst_recom":      t.analyst_recom,
            "return_1y":          t.price_return_1y,
            "return_5y":          t.price_return_5y,
            "pct_from_ath":       t.pct_from_ath,
            "filings":            filings,
        })
    return out


def _signal_accuracy(db: Session) -> dict:
    """
    Honest self-score on two long-term horizons: weekly (7-day check) and
    monthly (30-day check). Each is the % of graded signals that proved right.
    The monthly window is wider (rows are ≥30d old, so a 30-day lookback would
    exclude them). Returns {"weekly": {...}, "monthly": {...}} plus a top-level
    "pct"/"n" mirroring weekly for backward compatibility.
    """
    from src.storage.models import SignalHistory
    import datetime as _dt
    now = _dt.datetime.utcnow()

    def _pct(field, since_days):
        cutoff = now - _dt.timedelta(days=since_days)
        rows = (db.query(SignalHistory)
                .filter(field.isnot(None), SignalHistory.created_at >= cutoff)
                .all())
        if not rows:
            return {"pct": None, "n": 0}
        correct = sum(1 for r in rows if getattr(r, field.key))
        return {"pct": round(correct / len(rows) * 100), "n": len(rows)}

    weekly  = _pct(SignalHistory.correct, 45)
    monthly = _pct(SignalHistory.correct_30d, 120)
    return {"weekly": weekly, "monthly": monthly, **weekly}


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


@app.route("/api/verify/<ticker>")
def api_verify(ticker):
    """Independently cross-check our signal against Finviz (analysts + performance)."""
    from src.collectors.finviz_verify import verify
    our = request.args.get("signal", "")
    data = verify(ticker, our)
    if not data:
        return jsonify({"error": "finviz unavailable", "ticker": ticker.upper()}), 404
    return jsonify(data)


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


def _stock_context(db: Session, ticker: str) -> str:
    """
    Everything the dashboard knows about ONE stock, as plain text, so the
    chatbot can answer specific questions ("why is JPM a buy?", "is NVDA
    risky?") from the same numbers the user sees on screen.
    """
    from src.storage.models import Filing
    sym = ticker.upper().lstrip("$")
    ts = db.query(TickerSentiment).filter(TickerSentiment.ticker == sym).first()
    if ts is None:
        return ""

    pred = ts.continuation_score or 0.0
    signal = ("BUY" if pred > SIGNAL_BUY_THRESHOLD
              else "SELL" if pred < -SIGNAL_SELL_THRESHOLD else "HOLD")
    n_arts = max(1, (ts.bullish_count or 0) + (ts.bearish_count or 0) + (ts.neutral_count or 0))
    n_filings = db.query(Filing).filter(Filing.ticker == sym).count()
    evidence = min(40, n_filings * 3) + min(25, n_arts * 2)
    confidence = min(95, round(35 + evidence * 0.6 + abs(pred) * 30))

    def _fmt(v):
        return "n/a" if v is None else f"{v:+.3f}"

    analyst_txt = "n/a"
    if ts.analyst_signal is not None:
        analyst_txt = f"{ts.analyst_signal:+.3f} (Finviz analyst consensus {ts.analyst_recom}/5, 1=Strong Buy … 5=Strong Sell)"

    L = [
        f"=== STOCK DATA FOR {sym} (this is what the dashboard shows) ===",
        f"Our rating: {signal} (overall score {pred:+.3f}, "
        f"{confidence}% confidence, {100 - confidence}% uncertainty).",
        f"Sector: {_sector_of(sym)}.",
        "The rating blends FOUR signals (each -1 bad to +1 good), weighted:",
        f"  1. News this week (30%): {_fmt(ts.composite_score)} "
        f"from {ts.article_count or 0} articles "
        f"({ts.bullish_count or 0} bullish, {ts.bearish_count or 0} bearish, "
        f"{ts.neutral_count or 0} neutral).",
        f"  2. Price momentum (30%): {_fmt(ts.momentum_signal)} "
        f"(1-year {ts.price_return_1y}%, 5-year {ts.price_return_5y}%).",
        f"  3. Wall-Street analyst view (25%): {analyst_txt}.",
        f"  4. SEC filings (15%): {_fmt(ts.reports_signal)} "
        f"— latest filings read as \"{ts.fundamental_verdict or 'Stable'}\".",
        "IMPORTANT: our BUY/SELL/HOLD is this blend of sentiment + market data, "
        "NOT a guaranteed price forecast. A stock can be up a lot yet get HOLD/SELL "
        "if its recent news is negative, and vice-versa. Explain it that way.",
    ]
    if ts.price_return_1y is not None or ts.price_return_5y is not None:
        L.append(
            f"Price record: 1-year {ts.price_return_1y}%, 5-year {ts.price_return_5y}%, "
            f"currently {ts.pct_from_ath}% from its all-time high, "
            f"volatility {ts.price_volatility}% a year "
            f"({'high — it swings a lot' if (ts.price_volatility or 0) >= 45 else 'relatively steady' if (ts.price_volatility or 0) < 25 else 'moderate'})."
        )
    if ts.top_headline:
        L.append(f"Top headline driving it: \"{ts.top_headline}\" "
                 f"({SOURCE_LABELS.get(ts.top_source or '', ts.top_source or '')}).")

    fs = (db.query(Filing).filter(Filing.ticker == sym)
          .order_by(desc(Filing.filed_at)).limit(3).all())
    if fs:
        L.append("Recent SEC filings we scored:")
        for f in fs:
            when = f.filed_at.strftime("%b %d, %Y") if f.filed_at else "—"
            L.append(f"  - {f.form_type} filed {when} (score {f.fundamental_score or 0:+.2f})"
                     + (f": {f.llm_summary[:220]}" if f.llm_summary else ""))

    week_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    arts = (db.query(SentimentResult)
            .filter(SentimentResult.tickers.like(f"%{sym}%"),
                    SentimentResult.published >= week_ago)
            .order_by(desc(SentimentResult.rank_score)).limit(5).all())
    if arts:
        L.append(f"Recent news about {sym}:")
        for a in arts:
            lbl = ("bullish" if (a.sentiment_score or 0) > 0.05
                   else "bearish" if (a.sentiment_score or 0) < -0.05 else "neutral")
            L.append(f"  - [{lbl}] {a.title} ({SOURCE_LABELS.get(a.source, a.source)})")
    return "\n".join(L)


def _tickers_in_question(text: str, db: Session) -> list[str]:
    """Which tracked stocks is the user asking about?"""
    from src.sentiment.ticker_extractor import extract_tickers
    found = extract_tickers(text or "", max_tickers=3)
    tracked = {t[0] for t in db.query(TickerSentiment.ticker).all()}
    return [t for t in found if t in tracked]


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

    # The stock the user currently has open, sent by the dashboard
    viewing = str(payload.get("ticker", "") or "").upper().lstrip("$")[:10]

    db = _make_session()
    try:
        context = _chat_context(db)

        # Which stock(s) is this question about? Look at the latest user turn,
        # and fall back to whatever they're viewing ("is this a good buy?").
        last_user = next((m["content"] for m in reversed(clean)
                          if m.get("role") == "user"), "")
        syms = _tickers_in_question(last_user, db)
        if not syms and viewing:
            syms = [viewing]
        # Also honour a stock named earlier in the conversation
        if not syms:
            for m in reversed(clean[:-1]):
                syms = _tickers_in_question(m.get("content", ""), db)
                if syms:
                    break

        blocks = [b for b in (_stock_context(db, s) for s in syms[:2]) if b]
        if blocks:
            context += ("\n\n" + "\n\n".join(blocks) +
                        "\n\nAnswer the user's question using these exact numbers. "
                        "Explain what they mean in plain English for a beginner. "
                        "You may explain what the data shows and why the rating is "
                        "what it is, but never tell them to buy or sell — remind them "
                        "it's for learning, not financial advice.")
    finally:
        db.close()

    reply = chatbot.chat(clean, context=context, mode=mode)
    return jsonify({"reply": reply})


@app.route("/stream")
def stream():
    def event_gen():
        db = _make_session()
        # try/finally: without it the session (and its pooled connection) leaked
        # every time a browser tab closed and the generator was torn down.
        try:
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
                    db.rollback()
                    yield f"data: {json.dumps({'error': str(exc)})}\n\n"
                time.sleep(DASHBOARD_REFRESH)
        finally:
            db.close()

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
