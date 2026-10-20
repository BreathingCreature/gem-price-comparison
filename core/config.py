"""
Central config for the GeM price comparison pipeline.
Loads .env once; every other module imports constants from here instead of
reading os.environ / calling load_dotenv() itself.
"""
import logging
import math
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from core.errors import redact

load_dotenv()

# --- logging -----------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("gem_compare")


class SecretFilter(logging.Filter):
    """Scrub key-shaped text from every log line.

    A provider error can quote the key it rejected. Formatting the record
    first (which applies %-args) and only replacing the text when something
    actually matched keeps ordinary %d/%s records working untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken record must still be logged
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(SecretFilter())

# --- paths ---------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
TRACES_DIR = BASE_DIR / "traces"
CACHE_DIR = BASE_DIR / "cache"
TRACES_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)


def _flag(name: str, default: bool) -> bool:
    """Read an on/off setting from the environment: 1, true, yes or on mean on.
    Empty or whitespace-only means use the default."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float_setting(name: str, default: float, minimum: Optional[float] = None, maximum: Optional[float] = None) -> float:
    """Read a float env var. Returns the default (and logs a warning) when the
    text is not a number, is NaN or infinite, or is outside minimum..maximum."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, using default %r", name, raw, default)
        return default
    if math.isnan(value) or math.isinf(value):
        logger.warning("Invalid %s=%r (not a finite number), using default %r", name, raw, default)
        return default
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        logger.warning("Out-of-range %s=%r (allowed %r to %r), using default %r", name, raw, minimum, maximum, default)
        return default
    return value


def _int_setting(name: str, default: int) -> int:
    """Read an int env var, returning the default on any parse problem."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, using default %r", name, raw, default)
        return default


# --- NVIDIA / LLM ----------------------------------------------------------
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
# Model pinned to nemotron-3-super (user choice). NVIDIA_MODELS env var can
# still override with a comma-separated list if a fallback chain is wanted.
_DEFAULT_MODELS = "nvidia/nemotron-3-super-120b-a12b"
NVIDIA_MODELS = [m.strip() for m in os.getenv("NVIDIA_MODELS", _DEFAULT_MODELS).split(",") if m.strip()]
NVIDIA_MODEL = NVIDIA_MODELS[0]  # back-compat alias
NVIDIA_MAX_TOKENS = _int_setting("NVIDIA_MAX_TOKENS", 2000)
NVIDIA_MIN_SECONDS_BETWEEN_CALLS = _float_setting("NVIDIA_MIN_SECONDS_BETWEEN_CALLS", 1.6)
NVIDIA_TIMEOUT_SECONDS = _float_setting("NVIDIA_TIMEOUT_SECONDS", 90.0)

_PLACEHOLDER_KEYS = {"", "your_key_here", "changeme", "xxx"}


def key_is_set(value) -> bool:
    """True if an API key looks real (not empty and not a template placeholder)."""
    return (value or "").strip().lower() not in _PLACEHOLDER_KEYS


# --- LLM endpoint router (see llm/router.py) ---------------------------------
# Which providers exist is described in llm_providers.json. If that file is
# missing, only NVIDIA (from the settings above) is used.
LLM_PROVIDERS_FILE = Path(os.getenv("LLM_PROVIDERS_FILE", str(BASE_DIR / "llm_providers.json")))
# Longest a single LLM call may wait for a busy endpoint before giving up.
LLM_MAX_WAIT_SECONDS = _float_setting("LLM_MAX_WAIT_SECONDS", 45.0)
# Optional tag mixed into every LLM cache key so different models or experiments
# don't replay each other's answers. Empty string = no tag (old keys unchanged).
LLM_CACHE_TAG = os.getenv("LLM_CACHE_TAG", "")


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
        logger.info("NVIDIA_API_KEY loaded")
    else:
        logger.warning("NVIDIA_API_KEY not set — LLM-dependent parts will fail until it is.")


# --- pipeline behaviour ----------------------------------------------------
CACHE_TTL_SECONDS = _float_setting("CACHE_TTL_SECONDS", 3600.0, minimum=0.0)
MATCH_CONFIDENCE_THRESHOLD = _float_setting("MATCH_CONFIDENCE_THRESHOLD", 0.75, minimum=0.0, maximum=1.0)
# Hard price-sanity gate applied AFTER the LLM verdict: a "match" priced
# outside [GeM * LOW, GeM * HIGH] is rejected regardless of confidence.
# Rationale (live bug 2026-09): a logitech.com/en-us page listing $39.99
# scraped as "39.99 INR" passed LLM verify at 95% and was crowned cheapest
# vs a ₹2,725 GeM listing — prompt-level price-sanity instructions alone
# are not enough.
PRICE_SANITY_LOW_RATIO = _float_setting("PRICE_SANITY_LOW_RATIO", 0.15, minimum=0.0, maximum=1.0)   # below 15% of GeM price -> implausible
PRICE_SANITY_HIGH_RATIO = _float_setting("PRICE_SANITY_HIGH_RATIO", 5.0, minimum=1.0)   # above 5x GeM price -> implausible


# On/off switches. The defaults are the normal behaviour. They exist so the
# evaluation can switch one feature off at a time (see docs/evaluation.md).
PARALLEL_SEARCH = _flag("GEM_PARALLEL", False)  # search Flipkart and Amazon at the same time
RETRY_ENABLED = _flag("GEM_RETRY", True)  # ask the LLM for better search words when nothing matched
GATES_ENABLED = _flag("GEM_GATES", True)  # the currency check and the price-band check after the LLM verdict
LLM_CACHE_ENABLED = _flag("LLM_CACHE", False)  # remember LLM answers on disk (llm/cache.py)
LLM_CACHE_DIR = Path(os.getenv("LLM_CACHE_DIR", str(CACHE_DIR / "llm")))
# Companion gate (matcher): a candidate whose scraper reports an explicit
# non-INR currency is rejected even if the number falls inside that band —
# numeric-band checks alone cannot catch USD/EUR values that happen to
# overlap the rupee range (e.g. $300 vs GeM ₹10,000).
KNOWN_MARKETPLACE_DOMAINS = ["amazon.in", "flipkart.com"]

# Speed and cost knobs. The values below are the normal behaviour: one job at
# a time, nothing dropped, nothing remembered. Each one can be turned up on
# its own, and the evaluation records them with the run (eval/run_eval.py).
LLM_WORKERS = max(1, _int_setting("GEM_LLM_WORKERS", 1))  # listings verified at the same time
PREFILTER_TOP = max(0, _int_setting("GEM_PREFILTER_TOP", 0))  # keep only the best N listings per shop before the LLM (0 = off)
PRICE_PREGATE = _flag("GEM_PRICE_PREGATE", False)  # skip the LLM for listings whose price is already outside the band
REUSE_VERDICTS = _flag("GEM_REUSE_VERDICTS", False)  # on a retry, keep the verdicts already given in the first pass
SLIM_REFERENCE = _flag("GEM_SLIM_REFERENCE", False)  # verify calls send only the reference's identity fields, not the full spec table
DISCOVERY_ENABLED = _flag("GEM_DISCOVERY", True)  # other-shops discovery and the generic scrape
FLIPKART_LIGHT = _flag("GEM_FLIPKART_LIGHT", True)  # plain HTTP search page first, then the light Chrome; 0 turns it off
DISCOVERY_TIME_LIMIT_SECONDS = _float_setting("GEM_DISCOVERY_TIME_LIMIT", 25.0)  # total budget for one discovery call, all engines together

# Max trace files to keep before rotation deletes oldest. Env-overrideable.
TRACE_MAX_FILES = _int_setting("GEM_TRACE_MAX_FILES", 200)
# Traces older than this many days are removed by the same rotation scan.
TRACE_MAX_AGE_DAYS = _float_setting("GEM_TRACE_MAX_AGE_DAYS", 14.0, minimum=0.0)
# Bumped whenever the shape of a result changes; part of every cache key so
# an upgrade never serves an old-shaped result.
RESULT_VERSION = 1

REQUEST_TIMEOUT_SECONDS = 15

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
