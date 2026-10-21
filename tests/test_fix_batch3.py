"""Fix batch 3: the Flipkart EMI price order, and settings that are NaN or out of range."""
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.config as config  # noqa: E402
from scrapers import flipkart  # noqa: E402


def _card(text):
    # A card with no price element, so the text fallback is used.
    return f'<div data-id="X"><a href="/p/itm?pid=P1">Mouse</a><div>{text}</div></div>'


@pytest.mark.parametrize("text, expected", [
    ("No Cost EMI from ₹99/month ₹1,299 ₹2,499 48% off", "1299"),
    ("₹1,299 ₹2,499 48% off", "1299"),
    ("₹99 per month ₹1,299", "1299"),
    ("EMI from ₹1,249/month ₹14,999 ₹19,999", "14999"),
    ("EMI starts ₹1,299", "1299"),
])
def test_emi_text_never_hides_the_real_price(text, expected):
    cards = flipkart.parse_cards_from_html(_card(text))
    assert cards[0]["price"] == expected


def test_card_with_a_price_element_is_unchanged():
    html = '<div class="nZIRY7"><div class="RG5Slk">Mouse</div><div class="hZ3P6w DeU9vF">₹1,499</div><a href="/p/itm?pid=P1">x</a><div>EMI from ₹99/month</div></div>'
    assert flipkart.parse_cards_from_html(html)[0]["price"] == "1499"


@pytest.fixture
def reload_config(monkeypatch):
    def _reload(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(config)
    yield _reload
    monkeypatch.undo()
    importlib.reload(config)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "abc", "-5", "2"])
def test_bad_price_low_ratio_falls_back_to_the_default(reload_config, value):
    assert reload_config(PRICE_SANITY_LOW_RATIO=value).PRICE_SANITY_LOW_RATIO == 0.15


@pytest.mark.parametrize("value", ["1.5", "-0.1", "nan"])
def test_bad_confidence_threshold_falls_back_to_the_default(reload_config, value):
    assert reload_config(MATCH_CONFIDENCE_THRESHOLD=value).MATCH_CONFIDENCE_THRESHOLD == 0.75


def test_high_ratio_below_one_and_negative_ttl_fall_back(reload_config):
    cfg = reload_config(PRICE_SANITY_HIGH_RATIO="0.5", CACHE_TTL_SECONDS="-10")
    assert cfg.PRICE_SANITY_HIGH_RATIO == 5.0 and cfg.CACHE_TTL_SECONDS == 3600.0


def test_normal_values_are_kept(reload_config):
    cfg = reload_config(PRICE_SANITY_LOW_RATIO="0.2", MATCH_CONFIDENCE_THRESHOLD="0.9", PRICE_SANITY_HIGH_RATIO="4", CACHE_TTL_SECONDS="0")
    assert (cfg.PRICE_SANITY_LOW_RATIO, cfg.MATCH_CONFIDENCE_THRESHOLD, cfg.PRICE_SANITY_HIGH_RATIO, cfg.CACHE_TTL_SECONDS) == (0.2, 0.9, 4.0, 0.0)


@pytest.mark.parametrize("value", ["0", "-5", "abc"])
def test_bad_trace_max_files_falls_back_to_the_default(reload_config, value):
    assert reload_config(GEM_TRACE_MAX_FILES=value).TRACE_MAX_FILES == 200


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_bad_max_tokens_falls_back_to_the_default(reload_config, value):
    assert reload_config(NVIDIA_MAX_TOKENS=value).NVIDIA_MAX_TOKENS == 2000


@pytest.mark.parametrize("value", ["0", "-5", "nan", "abc"])
def test_bad_llm_timeout_falls_back_to_the_default(reload_config, value):
    assert reload_config(NVIDIA_TIMEOUT_SECONDS=value).NVIDIA_TIMEOUT_SECONDS == 90.0
