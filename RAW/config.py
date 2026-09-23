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
# Unverified as still current — check your NVIDIA catalog before relying on it.
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "openai/gpt-oss-20b")
NVIDIA_MIN_SECONDS_BETWEEN_CALLS = float(os.getenv("NVIDIA_MIN_SECONDS_BETWEEN_CALLS", "1.6"))


def require_nvidia_key() -> str:
    """Call this from anything that's about to make an LLM call — NOT at
    import time. That way parts that don't need the LLM (like the GeM
    extractor in Part 1) still work fine without a key configured yet."""
    if not NVIDIA_API_KEY:
        raise RuntimeError(
            "NVIDIA_API_KEY is not set. Put it in a .env file (see .env.example) "
            "or export it in your shell before running anything that calls the LLM."
        )
    return NVIDIA_API_KEY


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
KNOWN_MARKETPLACE_DOMAINS = ["amazon.in", "flipkart.com"]

REQUEST_TIMEOUT_SECONDS = 15
POLITE_DELAY_RANGE = (2.0, 3.0)  # seconds — used when hitting gem.gov.in in a loop

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
