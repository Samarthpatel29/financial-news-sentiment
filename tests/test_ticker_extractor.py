"""
Regression tests for ticker extraction.

Every case below is a real headline (or a close paraphrase of one) taken from
the stored corpus during the 2026-07-21 audit, which found ~2,500 wrong ticker
attributions caused by naive substring matching of company names.
"""
from __future__ import annotations

import pytest

from config.tickers import TICKER_UNIVERSE, COMPANY_TO_TICKER
from src.sentiment.ticker_extractor import extract_tickers, extract_primary_ticker


class TestSubstringFalsePositives:
    """Company aliases must match whole words, never fragments of other words."""

    @pytest.mark.parametrize("headline,must_not_contain", [
        ("Silvercorp Metals Q4 2026 Earnings Preview", "META"),
        ("Lion Delivers High-Grade Copper Intercepts and Positive Metallurgy", "META"),
        ("Futures: AI Dives Again, These Groups Strong; SpaceX Starship In Focus", "UPS"),
        ("EIB explores credit ratings to help European startups access financing", "UPS"),
        ("PHR Investors Have Opportunity to Lead Phreesia Securities Fraud Lawsuit", "U"),
        ("Tata Elxsi Launches ViTel, a Material Intelligence Solution", "INTC"),
        ("S&P Global Announces Leadership Change for Market Intelligence", "INTC"),
        ("The 5 best affordable hotel brands for your next trip", "F"),
        ("Kathie Lee Gifford lists Connecticut estate for a whopping $100 million", "F"),
        ("Russia tells Marco Rubio U.S. citizens should leave Kyiv", "C"),
        ("US home prices decline as over half of major cities lose ground", "C"),
        ("'The Mandalorian and Grogu' is Disney's lowest-ever Star Wars film opening", "LOW"),
        ("Shake Shack trades at its lowest level in two years", "LOW"),
        ("MINISO Group Non-GAAP EPADS of $0.26 misses, revenue of $824.6M", "MMM"),
        ("Honeywell International Nears June 29 Split, Reaffirms Outlook", "AFRM"),
        ("China and Pakistan reaffirm ties, vow to advance multipolar world", "AFRM"),
    ])
    def test_fragment_does_not_match(self, headline, must_not_contain):
        assert must_not_contain not in extract_tickers(headline), headline


class TestTruePositivesSurvive:
    """The boundary fix must not cost us any genuine mention."""

    @pytest.mark.parametrize("headline,expected", [
        ("Meta Platforms beats on revenue", "META"),
        ("UPS raises full-year guidance", "UPS"),
        ("Intel drops 4% as the chip rout deepens", "INTC"),
        ("Ford recalls 100,000 trucks", "F"),
        ("Citigroup tops estimates", "C"),
        ("Lowe's earnings beat expectations", "LOW"),
        ("3M announces spinoff", "MMM"),
        ("Affirm stock surges on holiday volume", "AFRM"),
        ("Unity Software cuts 25% of staff", "U"),
        ("Oracle stock has crashed 50% since June", "ORCL"),
        ("IBM Plunges on Second-Quarter Warning", "IBM"),
        ("PayPal Surges 17% on $60.50 Takeover Bid", "PYPL"),
    ])
    def test_real_mention_is_found(self, headline, expected):
        assert expected in extract_tickers(headline), headline

    def test_multiple_tickers_in_one_headline(self):
        got = extract_tickers("Semiconductors (NVDA, AMD, AVGO) are down because of Kimi K3")
        assert {"NVDA", "AMD", "AVGO"} <= set(got)


class TestAmbiguousAliases:
    """Aliases that are also plain English words need a capital letter."""

    def test_lowercase_common_word_is_ignored(self):
        assert "SNAP" not in extract_tickers("Traders made a snap decision to sell")
        assert "V" not in extract_tickers("He waited months for his visa application")

    def test_capitalised_company_is_matched(self):
        assert "SNAP" in extract_tickers("Snap beat revenue estimates")
        assert "V" in extract_tickers("Visa reported record payment volume")


class TestCashtags:
    """An explicit $CASHTAG is unambiguous and outranks the stopword list."""

    @pytest.mark.parametrize("sym", ["NOW", "OPEN", "ON", "AI"])
    def test_common_word_cashtag_still_extracts(self, sym):
        if sym not in TICKER_UNIVERSE:
            pytest.skip(f"{sym} not in universe")
        assert sym in extract_tickers(f"${sym} looks ready to break out")

    def test_bare_common_word_is_not_a_ticker(self):
        # Same words WITHOUT the $ must stay filtered by the stopword list
        assert "NOW" not in extract_tickers("NOW is the time to review your portfolio")


class TestUniverseHygiene:
    """Delisted or invented symbols must not be extractable."""

    @pytest.mark.parametrize("dead", [
        "ATVI",   # -> Microsoft, 2023
        "TWTR",   # taken private, 2022
        "SPLK",   # -> Cisco, 2024
        "PXD",    # -> ExxonMobil, 2024
        "ANTM",   # renamed ELV
        "ABC",    # renamed COR
        "PKI",    # renamed RVTY
        "ALTERYX", "PELOTON", "TRANE", "L3H", "BDNCE",  # never valid tickers
    ])
    def test_dead_symbol_not_in_universe(self, dead):
        assert dead not in TICKER_UNIVERSE

    def test_no_alias_points_at_a_dead_symbol(self):
        from config.tickers import _DELISTED
        orphans = {n: t for n, t in COMPANY_TO_TICKER.items() if t in _DELISTED}
        assert not orphans, f"aliases resolve to delisted tickers: {orphans}"


class TestPrimaryTicker:
    def test_cashtag_wins_over_company_name(self):
        assert extract_primary_ticker("$TSLA rally continues as Apple slips") == "TSLA"

    def test_returns_none_when_nothing_matches(self):
        assert extract_primary_ticker("The weather was pleasant on Tuesday") is None

    def test_empty_input(self):
        assert extract_tickers("") == []
        assert extract_primary_ticker("") is None
