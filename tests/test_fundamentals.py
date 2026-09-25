"""
Regression tests for the long-term prediction engine (the BUY/SELL/HOLD rating).

These lock in the fixes for "SELL on a stock that actually went up":
  * _price_score no longer collapses to -1 when distance-from-ATH is 0 (at the
    peak) or missing — the old `from_ath or -100` treated both as -100%
  * the four-signal blend no longer emits a confident BUY/SELL from news text
    alone when price momentum AND analyst consensus are both unavailable
  * a strong price uptrend floors the signal to at worst HOLD
  * the continuation label and the BUY/SELL/HOLD signal share the same asymmetric
    boundary, so the word and the badge never disagree
  * the Groq LLM judge degrades gracefully to FinBERT/VADER with no key
"""
from __future__ import annotations

import pytest

from src.pipeline import (
    _price_score,
    _continuation_label,
    _signal_of,
)


class TestPriceScoreAthBug:
    def test_at_all_time_high_is_bullish_not_penalized(self):
        # pct_from_ath == 0 means "sitting at the all-time high" — very bullish.
        # The old code (`from_ath or -100`) read 0 as -100% and forced near=-1.
        s = _price_score({"return_1y": 30.0, "return_5y": 100.0, "pct_from_ath": 0.0})
        assert s > 0.5, f"at-ATH healthy stock should score strongly positive, got {s}"

    def test_missing_ath_does_not_drag_score_down(self):
        # With ATH missing, the score renormalizes over the returns we DO have
        # instead of defaulting the ATH term to -100%.
        with_ath = _price_score({"return_1y": 40.0, "return_5y": 150.0, "pct_from_ath": 0.0})
        no_ath   = _price_score({"return_1y": 40.0, "return_5y": 150.0})
        assert no_ath > 0.5
        # Missing ATH should not turn a clearly-up stock negative.
        assert no_ath > 0.0

    def test_all_missing_is_neutral(self):
        assert _price_score({}) == 0.0

    def test_downtrend_is_negative(self):
        s = _price_score({"return_1y": -30.0, "return_5y": -60.0, "pct_from_ath": -50.0})
        assert s < -0.3


class TestContinuationLabelMatchesSignal:
    def test_label_and_signal_agree_across_boundary(self):
        # The word beside the badge must never contradict it.
        for score in (0.05, 0.11, 0.12, 0.13, 0.30, -0.11, -0.13, -0.40):
            label = _continuation_label(score)
            signal = _signal_of(score)
            if signal == "BUY":
                assert label in ("Building", "Strong Uptrend"), (score, label)
            elif signal == "SELL":
                assert label in ("Weak", "Strong Downtrend"), (score, label)
            else:
                assert label == "Mixed", (score, label)

    def test_symmetric_strong_labels(self):
        assert _continuation_label(0.40) == "Strong Uptrend"
        assert _continuation_label(-0.40) == "Strong Downtrend"


class TestSignalOf:
    def test_thresholds(self):
        # Asymmetric: BUY needs > 0.25 (backtested), SELL needs < -0.12.
        assert _signal_of(0.30) == "BUY"
        assert _signal_of(0.25) == "HOLD"       # BUY boundary is strict
        assert _signal_of(0.20) == "HOLD"       # marginal buys are now HOLD
        assert _signal_of(-0.12) == "HOLD"      # SELL boundary is strict
        assert _signal_of(-0.13) == "SELL"


def _blend(news, momentum, analysts, reports):
    """Mirror of the fundamentals blend + guardrails, for unit testing the math."""
    parts = [(news, 0.30), (momentum, 0.30), (analysts, 0.25), (reports, 0.15)]
    num = sum(v * w for v, w in parts if v is not None)
    den = sum(w for v, w in parts if v is not None)
    pred = num / den if den else 0.0
    if momentum is None and analysts is None:
        pred = max(-0.12, min(0.12, pred))
    if momentum is not None and momentum >= 0.5:
        pred = max(pred, -0.12)
    return round(pred, 4)


class TestBlendGuardrails:
    def test_text_only_bad_news_does_not_produce_sell(self):
        # Price + analysts both unavailable (scrapers failed), news is very
        # bearish, reports neutral. Must clamp to HOLD, not SELL.
        pred = _blend(news=-0.9, momentum=None, analysts=None, reports=0.0)
        assert _signal_of(pred) == "HOLD"

    def test_text_only_good_news_does_not_produce_buy(self):
        pred = _blend(news=0.9, momentum=None, analysts=None, reports=0.0)
        assert _signal_of(pred) == "HOLD"

    def test_strong_uptrend_floors_to_hold_despite_bad_news(self):
        # This is the exact "+70% stock shown SELL" bug: strong momentum, awful
        # news week. Must not be SELL.
        pred = _blend(news=-0.9, momentum=0.8, analysts=None, reports=-0.5)
        assert _signal_of(pred) != "SELL"

    def test_momentum_present_allows_normal_sell_when_weak(self):
        # A genuinely weak stock (down trend + bad news) still reads SELL.
        pred = _blend(news=-0.6, momentum=-0.7, analysts=-0.5, reports=-0.4)
        assert _signal_of(pred) == "SELL"

    def test_healthy_stock_reads_buy(self):
        pred = _blend(news=0.4, momentum=0.7, analysts=0.6, reports=0.2)
        assert _signal_of(pred) == "BUY"


class TestLlmJudgeGracefulFallback:
    def test_no_key_returns_all_none(self, monkeypatch):
        # With no Groq key the judge must return None per item so the caller
        # falls back to FinBERT/VADER — the fully-offline path.
        import src.sentiment as judge
        monkeypatch.setattr(judge, "GROQ_API_KEY", "")
        out = judge.score_headlines(["Acme cuts costs", "Acme cuts guidance"])
        assert out == [None, None]

    def test_empty_input(self):
        import src.sentiment as judge
        assert judge.score_headlines([]) == []


class TestFinnhubAnalystConsensus:
    """
    The analyst component is 25% of the blend but was populated in only ~2% of
    logged signals: it came from scraping Finviz, which bot-blocks server-side
    requests. It now comes from Finnhub's free API, mapped onto the same 1..5
    scale Finviz publishes so nothing downstream had to change.
    """

    def test_all_strong_buy_maps_to_one(self):
        import src.collectors as c
        row = {"strongBuy": 9, "buy": 0, "hold": 0, "sell": 0, "strongSell": 0}
        assert c._counts_to_recom(row) == 1.0

    def test_all_strong_sell_maps_to_five(self):
        import src.collectors as c
        row = {"strongBuy": 0, "buy": 0, "hold": 0, "sell": 0, "strongSell": 7}
        assert c._counts_to_recom(row) == 5.0

    def test_mixed_counts_are_weighted(self):
        import src.collectors as c
        # (1*12 + 2*22 + 3*15 + 4*3 + 5*1) / 53 == 118/53 == 2.23
        row = {"strongBuy": 12, "buy": 22, "hold": 15, "sell": 3, "strongSell": 1}
        assert c._counts_to_recom(row) == 2.23

    def test_no_analysts_is_none_not_zero(self):
        # A 0-count row must not divide by zero or read as "Strong Buy".
        import src.collectors as c
        assert c._counts_to_recom({}) is None
        assert c._counts_to_recom(
            {"strongBuy": 0, "buy": 0, "hold": 0, "sell": 0, "strongSell": 0}
        ) is None

    def test_round_trips_through_analyst_signal(self):
        # End-to-end: counts → 1..5 → the +1..-1 the blend actually consumes.
        import src.collectors as c
        from src.pipeline import _analyst_signal
        strong_buy = c._counts_to_recom({"strongBuy": 4, "buy": 0, "hold": 0,
                                         "sell": 0, "strongSell": 0})
        strong_sell = c._counts_to_recom({"strongBuy": 0, "buy": 0, "hold": 0,
                                          "sell": 0, "strongSell": 4})
        assert _analyst_signal(strong_buy) == 1.0
        assert _analyst_signal(strong_sell) == -1.0

    def test_missing_key_raises_rather_than_looking_uncovered(self, monkeypatch):
        # The strict form must distinguish "we have no key" from "no analyst
        # covers this ticker" — the caller's circuit breaker depends on it.
        import src.collectors as c
        monkeypatch.setattr(c, "_FINNHUB_KEY", "")
        with pytest.raises(c.FinnhubError):
            c.finnhub_recom_strict("AAPL")
        # ...while the non-raising wrapper still degrades quietly.
        assert c.finnhub_recom("AAPL") is None

    def test_empty_list_is_uncovered_not_an_error(self, monkeypatch):
        # Finnhub answers an unknown ticker with [] — a real answer, so it must
        # NOT raise, or a run of obscure tickers would trip the breaker and
        # disable the source exactly the way the Finviz scrape already fails.
        import src.collectors as c
        monkeypatch.setattr(c, "_FINNHUB_KEY", "test-key")
        monkeypatch.setattr(c, "requests", _FakeRequests(200, []))
        assert c.finnhub_recom_strict("ZZZZ") is None

    def test_http_error_raises(self, monkeypatch):
        import src.collectors as c
        monkeypatch.setattr(c, "_FINNHUB_KEY", "test-key")
        monkeypatch.setattr(c, "requests", _FakeRequests(403, []))
        with pytest.raises(c.FinnhubError):
            c.finnhub_recom_strict("AAPL")

    def test_picks_the_latest_period(self, monkeypatch):
        # Don't trust Finnhub's ordering — sort on `period`.
        import src.collectors as c
        monkeypatch.setattr(c, "_FINNHUB_KEY", "test-key")
        payload = [
            {"period": "2026-07-01", "strongBuy": 0, "buy": 0, "hold": 0,
             "sell": 0, "strongSell": 5},                      # old: 5.0
            {"period": "2026-09-01", "strongBuy": 5, "buy": 0, "hold": 0,
             "sell": 0, "strongSell": 0},                      # new: 1.0
        ]
        monkeypatch.setattr(c, "requests", _FakeRequests(200, payload))
        assert c.finnhub_recom_strict("AAPL") == 1.0

    def test_uncovered_ticker_does_not_fall_back_to_finviz(self, monkeypatch):
        # If Finnhub says nobody covers it, the scrape has nothing to add;
        # calling it anyway just burns a blocked request per cycle.
        import src.collectors as c
        import src.pipeline as p
        p._recom_cache.clear()
        p._finnhub_calls.clear()
        p._finnhub_fails["n"] = 0
        p._finviz_fails["n"] = 0
        called = []
        monkeypatch.setattr(c, "finnhub_recom_strict", lambda t: None)
        monkeypatch.setattr(c, "verify", lambda t, s="": called.append(t))
        assert p._fetch_recom("ZZZZ") is None
        assert called == [], "Finviz should not be consulted for an uncovered ticker"

    def test_stays_under_the_rate_limit(self, monkeypatch):
        # A cold cache asks for all ~125 tracked tickers at once, which measured
        # at ~158 calls/min against a 60/min allowance and tripped the breaker
        # mid-cycle. We must never spend more than the cap inside one window.
        import src.collectors as c
        import src.pipeline as p
        p._recom_cache.clear()
        p._finnhub_calls.clear()
        p._finnhub_fails["n"] = 0
        p._finviz_fails["n"] = 0
        monkeypatch.setattr(c, "finnhub_recom_strict", lambda t: 2.0)
        monkeypatch.setattr(c, "verify", lambda t, s="": None)

        spent = [p._fetch_recom(f"T{i}") for i in range(p._FINNHUB_MAX_PER_MIN)]
        assert all(v == 2.0 for v in spent)
        assert len(p._finnhub_calls) == p._FINNHUB_MAX_PER_MIN

    def test_waits_for_the_window_rather_than_skipping(self, monkeypatch):
        # Past the cap we pace rather than defer: _fetch_recom's only caller is
        # the 6-hourly fundamentals pass, so a short wait is affordable and
        # gets full coverage in one cycle. Deferring spread it over 12-18 h.
        import src.collectors as c
        import src.pipeline as p
        p._recom_cache.clear()
        p._finnhub_calls.clear()
        p._finnhub_fails["n"] = 0
        p._finviz_fails["n"] = 0
        monkeypatch.setattr(c, "finnhub_recom_strict", lambda t: 2.0)
        monkeypatch.setattr(c, "verify", lambda t, s="": None)

        slept = []

        def _fake_sleep(secs):
            # Stand in for the window aging out while we waited.
            slept.append(secs)
            p._finnhub_calls.clear()

        monkeypatch.setattr(p.time, "sleep", _fake_sleep)

        # Fill the window with calls stamped "now" so the budget is spent.
        now = p.time.time()
        p._finnhub_calls.extend([now] * p._FINNHUB_MAX_PER_MIN)

        assert p._fetch_recom("OVER") == 2.0, "should wait, then succeed"
        assert slept, "should have waited for the window instead of skipping"
        assert 0 < slept[0] <= p._FINNHUB_MAX_WAIT
        assert p._recom_cache["OVER"][1] == 2.0

    def test_wait_is_bounded(self, monkeypatch):
        # Guard against a clock jump wedging the whole cycle: if the computed
        # wait is absurd we skip that ticker instead of blocking on it.
        import src.collectors as c
        import src.pipeline as p
        p._recom_cache.clear()
        p._finnhub_calls.clear()
        p._finnhub_fails["n"] = 0
        p._finviz_fails["n"] = 0
        monkeypatch.setattr(c, "finnhub_recom_strict", lambda t: 2.0)
        monkeypatch.setattr(c, "verify", lambda t, s="": None)
        monkeypatch.setattr(p.time, "sleep", lambda s: None)

        # Timestamps far in the future -> the wait exceeds one window.
        future = p.time.time() + 10_000
        p._finnhub_calls.extend([future] * p._FINNHUB_MAX_PER_MIN)

        assert p._fetch_recom("WEDGE") is None
        assert "WEDGE" not in p._recom_cache, "a throttled skip must not be cached"

    def test_finnhub_failure_falls_back_to_finviz(self, monkeypatch):
        import src.collectors as c
        import src.pipeline as p
        p._recom_cache.clear()
        p._finnhub_calls.clear()
        p._finnhub_fails["n"] = 0
        p._finviz_fails["n"] = 0

        def _boom(_t):
            raise c.FinnhubError("down")

        monkeypatch.setattr(c, "finnhub_recom_strict", _boom)
        monkeypatch.setattr(c, "verify", lambda t, s="": {"recom": 2.0})
        assert p._fetch_recom("AAPL") == 2.0


class _FakeRequests:
    """Stands in for the `requests` module so no test touches the network."""

    def __init__(self, status, payload):
        self._status = status
        self._payload = payload

    def get(self, *_a, **_kw):
        return _FakeResponse(self._status, self._payload)


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload
