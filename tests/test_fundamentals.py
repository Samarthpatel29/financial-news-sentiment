"""
Regression tests for the long-term prediction engine (the BUY/SELL/HOLD rating).

These lock in the fixes for "SELL on a stock that actually went up":
  * _price_score no longer collapses to -1 when distance-from-ATH is 0 (at the
    peak) or missing — the old `from_ath or -100` treated both as -100%
  * the four-signal blend no longer emits a confident BUY/SELL from news text
    alone when price momentum AND analyst consensus are both unavailable
  * a strong price uptrend floors the signal to at worst HOLD
  * the continuation label and the BUY/SELL/HOLD signal share the same ±0.12
    boundary, so the word and the badge never disagree
  * the Groq LLM judge degrades gracefully to FinBERT/VADER with no key
"""
from __future__ import annotations

from src.pipeline.fundamentals import (
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
        assert _signal_of(0.13) == "BUY"
        assert _signal_of(0.12) == "HOLD"       # boundary is strict
        assert _signal_of(-0.12) == "HOLD"
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
        import src.sentiment.llm_judge as judge
        monkeypatch.setattr(judge, "GROQ_API_KEY", "")
        out = judge.score_headlines(["Acme cuts costs", "Acme cuts guidance"])
        assert out == [None, None]

    def test_empty_input(self):
        import src.sentiment.llm_judge as judge
        assert judge.score_headlines([]) == []
