"""
Central config for the GeM price comparison pipeline.
Loads .env once; every other module imports constants from here instead of
reading os.environ / calling load_dotenv() itself.
"""
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- logging -----------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("gem_compare")

# --- paths ---------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
TRACES_DIR = BASE_DIR / "traces"
CACHE_DIR = BASE_DIR / "cache"
TRACES_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

# --- NVIDIA / LLM ----------------------------------------------------------
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
# Model pinned to nemotron-3-super (user choice). NVIDIA_MODELS env var can
# still override with a comma-separated list if a fallback chain is wanted.
_DEFAULT_MODELS = "nvidia/nemotron-3-super-120b-a12b"
NVIDIA_MODELS = [m.strip() for m in os.getenv("NVIDIA_MODELS", _DEFAULT_MODELS).split(",") if m.strip()]
NVIDIA_MODEL = NVIDIA_MODELS[0]  # back-compat alias
NVIDIA_MAX_TOKENS = int(os.getenv("NVIDIA_MAX_TOKENS", "2000"))
NVIDIA_MIN_SECONDS_BETWEEN_CALLS = float(os.getenv("NVIDIA_MIN_SECONDS_BETWEEN_CALLS", "1.6"))
NVIDIA_TIMEOUT_SECONDS = float(os.getenv("NVIDIA_TIMEOUT_SECONDS", "90.0"))

_PLACEHOLDER_KEYS = {"", "your_key_here", "changeme", "xxx"}


def require_nvidia_key() -> str:
    """Call this from anything that's about to make an LLM call — NOT at
    import time. That way parts that don't need the LLM (like the GeM
    extractor in Part 1) still work fine without a key configured yet.
    Obvious template placeholders count as unset so you get this clear
    error instead of a confusing API 401 later."""
    key = (NVIDIA_API_KEY or "").strip()
    if key.lower() in _PLACEHOLDER_KEYS:
        raise RuntimeError(
            "NVIDIA_API_KEY is not set. Put it in a .env file (see .env) "
            "or export it in your shell before running anything that calls the LLM."
        )
    return key


def log_startup_check() -> None:
    """Call at the top of cli.py to surface config problems early instead of
    three modules deep in a confusing stack trace."""
    if NVIDIA_API_KEY:
        logger.info("NVIDIA_API_KEY loaded (length=%d)", len(NVIDIA_API_KEY))
    else:
        logger.warning("NVIDIA_API_KEY not set — LLM-dependent parts will fail until it is.")


# --- pipeline behaviour ----------------------------------------------------
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "3600"))
MATCH_CONFIDENCE_THRESHOLD = float(os.getenv("MATCH_CONFIDENCE_THRESHOLD", "0.75"))
# Hard price-sanity gate applied AFTER the LLM verdict: a "match" priced
# outside [GeM * LOW, GeM * HIGH] is rejected regardless of confidence.
# Rationale (live bug 2026-09): a logitech.com/en-us page listing $39.99
# scraped as "39.99 INR" passed LLM verify at 95% and was crowned cheapest
# vs a ₹2,725 GeM listing — prompt-level price-sanity instructions alone
# are not enough.
PRICE_SANITY_LOW_RATIO = 0.15   # below 15% of GeM price → implausible
PRICE_SANITY_HIGH_RATIO = 5.0   # above 5x GeM price → implausible
KNOWN_MARKETPLACE_DOMAINS = ["amazon.in", "flipkart.com"]

REQUEST_TIMEOUT_SECONDS = 15

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
