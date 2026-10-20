"""Decides which LLM endpoint answers each call.

The rest of the project calls llm.client._call_llm() and never thinks about
providers. This module is the only place that knows there can be several.

An "endpoint" is one provider + one model, for example nvidia/nemotron or
groq/llama. Each endpoint keeps its own little record: when it was last
used, whether it is cooling down after an error, how often it failed.

For every call the router:
  1. skips endpoints that are disabled, cooling down, over their
     provider's daily_limit, used up for the day, or out of tokens for
     this minute (see llm_providers.json),
  2. waits a moment if the provider is already at its max_concurrent,
  3. skips endpoints that were used too recently (their minimum gap),
  4. picks one of the rest, and
  5. if the call fails, puts that endpoint on a cooldown and tries the next.

"Used up for the day" is different from a cooldown: when an endpoint
answers "daily limit" or "quota exceeded" there is nothing to wait for
until tomorrow, so the router writes that endpoint's name to
cache/llm_state.json and skips it on every later call — including after a
restart. Only names and reset times are stored, never keys.

If nothing can answer, the router raises GatewayBusy (cooling down or at a
concurrency cap — worth retrying shortly) or GatewayExhausted (every
allowance for the day is gone). Both subclass RuntimeError.

Adding a provider means adding one entry to llm_providers.json. No code.
See llm_providers.example.json and docs/llm-providers.md.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from core import config as _config
from core import events
from core.config import (
    LLM_MAX_WAIT_SECONDS,
    LLM_PROVIDERS_FILE,
    NVIDIA_BASE_URL,
    NVIDIA_MIN_SECONDS_BETWEEN_CALLS,
    NVIDIA_MODELS,
    NVIDIA_TIMEOUT_SECONDS,
    key_is_set,
    logger,
)
from llm import client as _client

# Cooldown (seconds) before an endpoint is tried again, by kind of failure.
# Repeated failures double the wait, up to MAX_COOLDOWN.
RATE_LIMIT_COOLDOWN = 30.0
OTHER_ERROR_COOLDOWN = 10.0
MAX_COOLDOWN = 300.0

# How far back the per-provider tokens_per_minute count looks (D2).
TOKEN_WINDOW_SECONDS = 60.0

# The kinds of LLM calls in this project (llm/client.py). A provider can
# declare in llm_providers.json which of them it wants ("purposes":
# ["verify"]) — normalize and expand are rare and need a good model, verify
# is ~90% of calls and an easy yes/no question that fast, high-rate-limit
# providers handle well.
KNOWN_PURPOSES = ("normalize", "verify", "expand")

# --- errors that tell the pipeline what to do next --------------------------------

class GatewayError(RuntimeError):
    """No endpoint could answer. Subclasses RuntimeError on purpose: every
    caller written before this distinction existed keeps working."""


class GatewayBusy(GatewayError):
    """Endpoints are cooling down or at their concurrency cap. The listing
    was not checked — retry it later, it did not fail."""


class GatewayExhausted(GatewayError):
    """Every endpoint has used up its allowance for the day. Warning text is
    the whole message: 'LLM limits used up until HH:MM for: name, name'."""


# --- remembering an endpoint whose day is used up ---------------------------------

STATE_FILE_NAME = "llm_state.json"

# Text a provider uses when the day's allowance is gone, not when it merely
# wants a slower pace. "rate limit" alone is deliberately NOT here: that is
# a short rest, and the normal cooldown handles it.
_QUOTA_MARKERS = (
    "daily limit",
    "daily quota",
    "quota exceeded",
    "exceeded your quota",
    "quota exhausted",
    "insufficient_quota",
    "out of credits",
    "credit balance",
)

# "retry after 2 hours", "try again in 30 minutes", "resets in 45s" ...
_RESET_HINT_RE = re.compile(
    r"(?:retry|try again|resets?|reset in|available)\D{0,20}?(\d+(?:\.\d+)?)\s*"
    r"(s|sec(?:ond)?s?|m|min(?:ute)?s?|h|hr|hours?)\b",
    re.IGNORECASE,
)


def _state_path() -> Path:
    """Read at call time, so a changed CACHE_DIR (tests) is respected."""
    return Path(_config.CACHE_DIR) / STATE_FILE_NAME


def _read_used_up() -> dict[str, float]:
    """{endpoint name: epoch seconds until it is worth trying again}."""
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    used = data.get("used_up") if isinstance(data, dict) else None
    if not isinstance(used, dict):
        return {}
    out: dict[str, float] = {}
    for name, until in used.items():
        try:
            out[str(name)] = float(until)
        except (TypeError, ValueError):
            continue
    return out


def _write_used_up(used: dict[str, float]) -> None:
    """One piece or nothing: temp file next to the target, then os.replace."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(prefix=".llm_state_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump({"used_up": used}, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _is_quota_error(status, text: str) -> bool:
    lowered = (text or "").lower()
    return status == 402 or any(marker in lowered for marker in _QUOTA_MARKERS)


def _reset_hint(text: str, now: float) -> Optional[float]:
    """The moment the error text promises, in epoch seconds, or None."""
    match = _RESET_HINT_RE.search(text or "")
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("h"):
        seconds = value * 3600
    elif unit.startswith("m"):
        seconds = value * 60
    else:
        seconds = value
    if seconds <= 0 or seconds > 24 * 3600:
        return None
    return now + seconds


def _next_local_midnight(now: float) -> float:
    local = datetime.fromtimestamp(now)
    return (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _clock_text(until: float) -> str:
    return datetime.fromtimestamp(until).strftime("%H:%M")


@dataclass
class Endpoint:
    provider: str
    model: str
    base_url: str
    api_key_env: str
    style: str = "plain"  # "nvidia" adds the NVIDIA-only request options
    min_gap: float = 0.0  # seconds between the starts of two calls
    timeout: float = 60.0
    enabled: bool = True
    disabled_reason: str = ""
    # Per-provider request options merged into chat.completions.create().
    # Example: {"reasoning_effort": "low"} or {"extra_body": {...}}.
    request_options: Optional[dict] = None
    # Per-provider guards for when several calls run at once (GEM_LLM_WORKERS):
    # how many calls may be in flight at once, and how many calls the
    # provider may get in one day. 0 means no limit (today's behaviour).
    max_concurrent: int = 0
    daily_limit: int = 0
    # Tokens the provider allows per minute (free plans announce this; it is
    # the real 429 limit). 0 = no limit. The router keeps a 60 s moving
    # count per provider and skips it when the next call looks like it
    # would go over.
    tokens_per_minute: int = 0
    # Which call kinds this endpoint takes. None (not set in the file)
    # means every purpose — that is today's behaviour.
    purposes: Optional[tuple[str, ...]] = None
    # live state, changes while the program runs
    cooldown_until: float = 0.0
    last_start: float = -1e9
    fail_streak: int = 0
    calls: int = 0
    errors: int = 0
    total_seconds: float = 0.0
    last_error: str = ""

    @property
    def name(self) -> str:
        return f"{self.provider}/{self.model}"


# --- reading the config ---------------------------------------------------------

def _default_providers() -> list[dict]:
    """No llm_providers.json: behave exactly like before, NVIDIA only."""
    return [
        {
            "name": "nvidia",
            "base_url": NVIDIA_BASE_URL,
            "api_key_env": "NVIDIA_API_KEY",
            "models": list(NVIDIA_MODELS),
            "min_seconds_between_calls": NVIDIA_MIN_SECONDS_BETWEEN_CALLS,
            "timeout_seconds": NVIDIA_TIMEOUT_SECONDS,
            "style": "nvidia",
        }
    ]


def _int_entry(path: Path, entry: dict, key: str) -> int:
    """One of the per-provider number settings, with a clear error message."""
    value = entry.get(key, 0)
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{path}: provider {entry['name']!r} {key} must be a whole number, got {entry.get(key)!r}") from None
    if value < 0:
        raise ValueError(f"{path}: provider {entry['name']!r} {key} cannot be negative")
    return value


def load_endpoints(path: Optional[Path] = None) -> tuple[list[Endpoint], str]:
    """Returns (endpoints in priority order, strategy)."""
    path = Path(path) if path else Path(LLM_PROVIDERS_FILE)
    strategy = "failover"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            strategy = data.get("strategy", "failover")
            providers = data.get("providers", [])
        else:
            providers = data
    else:
        providers = _default_providers()
    if strategy not in ("failover", "spread"):
        raise ValueError(f"{path}: strategy must be 'failover' or 'spread', got {strategy!r}")

    endpoints: list[Endpoint] = []
    for entry in providers:
        for key in ("name", "base_url", "api_key_env", "models"):
            if not entry.get(key):
                raise ValueError(f"{path}: every provider needs '{key}' (problem in {entry.get('name', entry)!r})")
        key_ok = key_is_set(os.getenv(entry["api_key_env"]))
        wanted = entry.get("enabled", True)
        request_options = entry.get("request_options")
        if request_options is not None and not isinstance(request_options, dict):
            raise ValueError(f"{path}: provider {entry['name']!r} request_options must be an object")
        max_concurrent = _int_entry(path, entry, "max_concurrent")
        daily_limit = _int_entry(path, entry, "daily_limit")
        tokens_per_minute = _int_entry(path, entry, "tokens_per_minute")
        purposes_raw = entry.get("purposes")
        purposes: Optional[tuple[str, ...]] = None
        if purposes_raw is not None:
            if not isinstance(purposes_raw, list) or any(p not in KNOWN_PURPOSES for p in purposes_raw):
                raise ValueError(
                    f"{path}: provider {entry['name']!r} purposes must be a list of {KNOWN_PURPOSES}, got {purposes_raw!r}"
                )
            purposes = tuple(purposes_raw)
        for model in entry["models"]:
            ep = Endpoint(
                provider=entry["name"],
                model=model,
                base_url=entry["base_url"],
                api_key_env=entry["api_key_env"],
                style=entry.get("style", "plain"),
                min_gap=float(entry.get("min_seconds_between_calls", 0.0)),
                timeout=float(entry.get("timeout_seconds", 60.0)),
                request_options=request_options,
                max_concurrent=max_concurrent,
                daily_limit=daily_limit,
                tokens_per_minute=tokens_per_minute,
                purposes=purposes,
            )
            if not wanted:
                ep.enabled, ep.disabled_reason = False, "turned off in the config file"
            elif not key_ok:
                ep.enabled, ep.disabled_reason = False, f"{entry['api_key_env']} is not set"
            endpoints.append(ep)
    return endpoints, strategy


# --- the router -----------------------------------------------------------------

def _estimate_call_tokens(messages: list[dict]) -> int:
    """Roughly how many tokens a call will cost: about 4 characters per
    token. Answers are short JSON, so the prompt is most of it — and once
    a call finishes, the provider's real usage replaces this estimate."""
    chars = 0
    for m in messages or []:
        if isinstance(m, dict):
            chars += len(str(m.get("content") or ""))
    return chars // 4


def _today_iso() -> str:
    return date.today().isoformat()


class Router:
    def __init__(
        self,
        endpoints: list[Endpoint],
        *,
        strategy: str = "failover",
        max_wait: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        client_factory: Optional[Callable[[Endpoint], object]] = None,
        today: Optional[Callable[[], str]] = None,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.endpoints = endpoints
        self.strategy = strategy
        self.max_wait = max_wait
        self.clock = clock
        self.sleep = sleep
        # Timestamps about *when in the day* something happened use wall time,
        # not the monotonic clock used for cooldowns. Tests inject a fake.
        self.wall_clock = wall_clock
        self._used_up: dict[str, float] = {
            name: until for name, until in _read_used_up().items() if until > self.wall_clock()
        }
        self._factory = client_factory or self._make_client
        self._clients: dict = {}
        self._lock = threading.Lock()
        # Per-provider live counters for the limits above (both keyed by
        # provider name, so models of one provider share one quota).
        self._today = today or _today_iso
        self._day = self._today()
        self._in_flight: dict = {}
        self._calls_today: dict = {}
        # Per-provider token count for the last TOKEN_WINDOW_SECONDS: a list
        # of {"t": when a call started, "tokens": how many, "final": whether
        # the number is the provider's real usage or still the estimate}.
        self._token_uses: dict = {}

    # -- public ---------------------------------------------------------------

    def complete(self, messages: list[dict], max_tokens: int, purpose: str = "") -> str:
        """Return the model's raw text. Raises GatewayBusy or GatewayExhausted
        (both RuntimeError) if nobody can answer."""
        deadline = self.clock() + self.max_wait
        estimated = _estimate_call_tokens(messages)
        empty_answers = 0
        while True:
            endpoint, wait = self._pick(purpose, estimated)
            if endpoint is None:
                if wait is None:
                    raise self._unusable_error()
                if self.clock() + wait > deadline:
                    raise self._unusable_error()
                self.sleep(wait)
                continue
            raw = self._call(endpoint, messages, max_tokens, purpose)
            if raw is not None:
                return raw
            # An empty answer is a failed call, but the same endpoint will
            # probably answer empty again — try ONE other endpoint, then stop.
            if "empty answer" in endpoint.last_error:
                empty_answers += 1
                if empty_answers >= 2:
                    raise GatewayBusy(
                        "Every LLM endpoint returned an empty answer: "
                        + self._why_unusable(include_cooling=True)
                    )
            else:
                empty_answers = 0

    def status(self) -> list[dict]:
        now = self.clock()
        out = []
        with self._lock:
            self._roll_day()
            for e in self.endpoints:
                reason = ""
                if not e.enabled:
                    state = "disabled"
                    reason = e.disabled_reason
                elif self._over_daily(e):
                    state = "limit"
                    reason = "daily limit reached"
                elif self._is_used_up(e):
                    state = "limit"
                    reason = f"used up until {_clock_text(self._used_up_until(e))}"
                elif e.cooldown_until > now:
                    state = "cooling"
                else:
                    state = "ready"
                out.append(
                    {
                        "endpoint": e.name,
                        "provider": e.provider,
                        "model": e.model,
                        "state": state,
                        "reason": reason,
                        "cooldown_remaining": round(max(e.cooldown_until - now, 0.0), 1),
                        "calls": e.calls,
                        "errors": e.errors,
                        "avg_seconds": round(e.total_seconds / e.calls, 2) if e.calls else None,
                        "last_error": e.last_error,
                        "calls_today": self._calls_today.get(e.provider, 0),
                        "in_flight": self._in_flight.get(e.provider, 0),
                        "max_concurrent": e.max_concurrent,
                        "daily_limit": e.daily_limit,
                        "tokens_per_minute": e.tokens_per_minute,
                        "tokens_last_minute": int(self._tokens_used(e.provider, now)),
                        "purposes": list(e.purposes) if e.purposes else None,
                    }
                )
        return out

    # -- internals ------------------------------------------------------------

    def _roll_day(self) -> None:
        """Forget yesterday's call counts. Call with the lock held."""
        today = self._today()
        if today != self._day:
            self._day = today
            self._calls_today.clear()

    def _over_daily(self, endpoint: Endpoint) -> bool:
        return endpoint.daily_limit > 0 and self._calls_today.get(endpoint.provider, 0) >= endpoint.daily_limit

    def _used_up_until(self, endpoint: Endpoint) -> float:
        return self._used_up.get(endpoint.name, 0.0)

    def _is_used_up(self, endpoint: Endpoint) -> bool:
        return self._used_up_until(endpoint) > self.wall_clock()

    def _mark_used_up(self, endpoint: Endpoint, until: float) -> None:
        """Remember the endpoint's day is gone, on disk too (call with the
        lock held). A state file that cannot be written must not break a
        run — it only costs the restart memory."""
        self._used_up[endpoint.name] = until
        self._used_up = {name: end for name, end in self._used_up.items() if end > self.wall_clock()}
        try:
            _write_used_up(self._used_up)
        except OSError as exc:
            logger.warning("could not save the used-up LLM endpoints (%s); a restart may retry them.", exc)

    def _unusable_error(self) -> GatewayError:
        """Busy or used-up? The pipeline acts differently: busy is retried,
        used-up produces one warning with the time it ends."""
        enabled = [e for e in self.endpoints if e.enabled]
        if enabled and all(self._over_daily(e) or self._is_used_up(e) for e in enabled):
            names = ", ".join(sorted(e.name for e in enabled))
            until = max(
                [self._used_up_until(e) for e in enabled if self._is_used_up(e)]
                + [_next_local_midnight(self.wall_clock())]
            )
            message = f"LLM limits used up until {_clock_text(until)} for: {names}"
            if any(self._over_daily(e) for e in enabled):
                message += " (" + self._why_unusable() + ")"
            return GatewayExhausted(message)
        return GatewayBusy("All LLM endpoints are busy or failing: " + self._why_unusable(include_cooling=True))

    def _release_slot(self, endpoint: Endpoint) -> None:
        """One in-flight call for this provider just finished."""
        with self._lock:
            count = self._in_flight.get(endpoint.provider, 0)
            self._in_flight[endpoint.provider] = max(count - 1, 0)

    def _pick(self, purpose: str = "", estimated: int = 0) -> tuple[Optional[Endpoint], Optional[float]]:
        """(endpoint, 0) if one is ready now, (None, seconds) if one will be
        ready after that wait, (None, None) if none can ever be used."""
        with self._lock:
            self._roll_day()
        now = self.clock()
        chosen, wait = self._choose(purpose, now, estimated)
        if chosen is not None or wait is not None:
            return chosen, wait
        # No endpoint advertises this purpose — anyone may answer, so a
        # restrictive purposes setting can never break a run. (When nobody
        # sets purposes at all, the first _choose already saw everything.)
        return self._choose(None, now, estimated)

    def _choose(self, purpose: Optional[str], now: float, estimated: int = 0) -> tuple[Optional[Endpoint], Optional[float]]:
        """Same pick as before, narrowed to endpoints that take this purpose.
        purpose=None means no filter — every endpoint is a candidate.
        estimated is the about-to-run call's token size: a provider whose
        tokens_per_minute would be blown by it is skipped."""
        with self._lock:
            usable = [
                e for e in self.endpoints
                if e.enabled and (e.purposes is None or purpose is None or purpose in e.purposes)
            ]
            if not usable:
                return None, None
            available = [
                e for e in usable if not self._over_daily(e) and not self._is_used_up(e)
            ]
            if not available:
                return None, None  # every allowance for today is gone
            free = [e for e in available if e.max_concurrent <= 0 or self._in_flight.get(e.provider, 0) < e.max_concurrent]
            ready = [
                e for e in free
                if e.cooldown_until <= now and now - e.last_start >= e.min_gap
                and self._token_ready(e, estimated, now)
            ]
            if ready:
                if self.strategy == "spread":
                    chosen = min(ready, key=lambda e: e.last_start)  # least recently used
                else:
                    chosen = ready[0]  # config order is the priority order
                chosen.last_start = now  # claim the slot before anyone else can
                self._in_flight[chosen.provider] = self._in_flight.get(chosen.provider, 0) + 1
                self._calls_today[chosen.provider] = self._calls_today.get(chosen.provider, 0) + 1
                # Count the call against the provider before it runs, so
                # parallel callers see each other's reservations.
                self._reserve_tokens(chosen.provider, estimated, now)
                return chosen, 0.0
            if free:
                waits = []
                for e in free:
                    wait = max(e.cooldown_until, e.last_start + e.min_gap) - now
                    if not self._token_ready(e, estimated, now):
                        wait = max(wait, self._token_wait(e, estimated, now))
                    waits.append(wait)
                return None, max(min(waits), 0.05)
            # every available endpoint is at its provider's concurrency cap —
            # an in-flight call frees one up shortly.
            return None, 0.05

    # -- token budget helpers (call with the lock held) -------------------------

    def _live_token_uses(self, provider: str, now: float) -> list[dict]:
        uses = self._token_uses.get(provider)
        if not uses:
            return []
        live = [u for u in uses if u["t"] > now - TOKEN_WINDOW_SECONDS]
        self._token_uses[provider] = live
        return live

    def _tokens_used(self, provider: str, now: float) -> float:
        return sum(u["tokens"] for u in self._live_token_uses(provider, now))

    def _token_ready(self, endpoint: Endpoint, estimated: int, now: float) -> bool:
        """False when this call would push the provider over its
        tokens_per_minute. A provider that has sent nothing in the window
        is always allowed one call, however big it looks — a tiny budget
        must throttle the provider, never brick it."""
        if endpoint.tokens_per_minute <= 0:
            return True
        used = self._tokens_used(endpoint.provider, now)
        return used == 0 or used + estimated <= endpoint.tokens_per_minute

    def _token_wait(self, endpoint: Endpoint, estimated: int, now: float) -> float:
        """Seconds until this call would fit: enough old uses must age out
        of the window. Only called when _token_ready said no, so there is
        at least one use to drop."""
        remaining = self._tokens_used(endpoint.provider, now)
        for use in self._token_uses.get(endpoint.provider, []):
            remaining -= use["tokens"]
            if remaining == 0 or remaining + estimated <= endpoint.tokens_per_minute:
                return max(use["t"] + TOKEN_WINDOW_SECONDS - now, 0.05)
        return TOKEN_WINDOW_SECONDS  # unreachable: dropping every use fits

    def _reserve_tokens(self, provider: str, estimated: int, now: float) -> None:
        self._token_uses.setdefault(provider, []).append(
            {"t": now, "tokens": float(estimated), "final": False}
        )

    def _finalize_tokens(self, endpoint: Endpoint, actual: Optional[float] = None) -> None:
        """The call this reservation belonged to is over. The provider's
        real usage replaces the estimate when it reported any; either way
        the entry is final and simply ages out of the window."""
        with self._lock:
            for use in self._token_uses.get(endpoint.provider, []):
                if not use["final"]:
                    use["final"] = True
                    if actual and actual > 0:
                        use["tokens"] = float(actual)
                    return

    def _why_unusable(self, include_cooling: bool = False) -> str:
        parts = []
        for e in self.endpoints:
            if not e.enabled:
                parts.append(f"{e.name}: {e.disabled_reason}")
            elif self._over_daily(e):
                parts.append(f"{e.name}: daily limit reached ({self._calls_today.get(e.provider, 0)}/{e.daily_limit} calls today)")
            elif self._is_used_up(e):
                parts.append(f"{e.name}: used up until {_clock_text(self._used_up_until(e))}")
            elif include_cooling and e.last_error:
                parts.append(f"{e.name}: {e.last_error}")
        return "; ".join(parts) or "no endpoints configured"

    def _make_client(self, endpoint: Endpoint):
        from openai import OpenAI

        key = os.getenv(endpoint.api_key_env, "").strip()
        return OpenAI(base_url=endpoint.base_url, api_key=key, timeout=endpoint.timeout, max_retries=0)

    def _client_for(self, endpoint: Endpoint):
        key = (endpoint.provider, endpoint.base_url)
        with self._lock:
            if key not in self._clients:
                self._clients[key] = self._factory(endpoint)
            return self._clients[key]

    def _call(self, endpoint: Endpoint, messages, max_tokens, purpose) -> Optional[str]:
        """One call, then always free this provider's in-flight slot."""
        try:
            return self._call_endpoint(endpoint, messages, max_tokens, purpose)
        finally:
            self._release_slot(endpoint)

    def _call_endpoint(self, endpoint: Endpoint, messages, max_tokens, purpose) -> Optional[str]:
        client = self._client_for(endpoint)
        started = self.clock()
        try:
            resp = client.chat.completions.create(
                model=endpoint.model,
                messages=messages,
                temperature=0.1,  # above 0 because nemotron's schema rejects exactly 0
                max_tokens=max_tokens,
                **_client._model_kwargs(endpoint.model, endpoint.style, endpoint.request_options),
            )
            raw = (resp.choices[0].message.content or "").strip()
        except _client._endpoint_errors() as exc:
            self._record_failure(endpoint, exc, self.clock() - started, purpose)
            return None
        seconds = self.clock() - started
        if not raw:
            finish_reason = getattr(resp.choices[0], "finish_reason", None) or ""
            reason_text = f"empty answer (finish_reason={finish_reason})" if finish_reason else "empty answer"
            self._record_failure(endpoint, RuntimeError(reason_text), seconds, purpose)
            return None
        usage = getattr(resp, "usage", None)
        actual = getattr(usage, "total_tokens", None) if usage is not None else None
        self._finalize_tokens(endpoint, actual if isinstance(actual, (int, float)) else None)
        with self._lock:
            endpoint.calls += 1
            endpoint.total_seconds += seconds
            endpoint.fail_streak = 0
        events.emit("llm_call", endpoint=endpoint.name, provider=endpoint.provider, model=endpoint.model,
                    purpose=purpose, seconds=round(seconds, 2), ok=True)
        return raw

    def _record_failure(self, endpoint: Endpoint, exc: Exception, seconds: float, purpose: str) -> None:
        status = getattr(exc, "status_code", None)
        text = (f"HTTP {status}: " if status else "") + f"{type(exc).__name__}: {str(exc)[:200]}"
        used_up_until: Optional[float] = None
        with self._lock:
            endpoint.errors += 1
            endpoint.fail_streak += 1
            endpoint.last_error = text
            if status in (401, 404):
                # A wrong key or a wrong model name will not fix itself.
                targets = [e for e in self.endpoints if e.provider == endpoint.provider] if status == 401 else [endpoint]
                reason = "the API key was rejected (401)" if status == 401 else "model not found (404)"
                for e in targets:
                    e.enabled, e.disabled_reason = False, reason
                cooldown = 0.0
            elif _is_quota_error(status, str(exc)):
                # Not "slow down" — the allowance is gone. Nothing to wait
                # for until the reset, so remember it (today and tomorrow).
                used_up_until = _reset_hint(str(exc), self.wall_clock()) or _next_local_midnight(self.wall_clock())
                self._mark_used_up(endpoint, used_up_until)
                cooldown = 0.0
            else:
                base = RATE_LIMIT_COOLDOWN if status == 429 else OTHER_ERROR_COOLDOWN
                retry_after = _retry_after(exc)
                cooldown = retry_after if retry_after is not None else base * min(2 ** (endpoint.fail_streak - 1), 8)
                cooldown = min(cooldown, MAX_COOLDOWN)
                endpoint.cooldown_until = self.clock() + cooldown
        # This call is over (failed or empty) — its reservation stops being
        # "about to run"; with no real usage it just keeps the estimate.
        self._finalize_tokens(endpoint)
        if used_up_until is not None:
            logger.warning("LLM endpoint %s has used up its allowance (%s). Not trying it again until %s.",
                           endpoint.name, text, _clock_text(used_up_until))
        else:
            logger.warning("LLM endpoint %s failed (%s). %s", endpoint.name, text,
                           "Disabled." if cooldown == 0.0 else f"Resting it for {cooldown:.0f}s.")
        events.emit("llm_call", endpoint=endpoint.name, provider=endpoint.provider, model=endpoint.model,
                    purpose=purpose, seconds=round(seconds, 2), ok=False, status=status, error=text)
        if used_up_until is not None:
            events.emit("llm_used_up", endpoint=endpoint.name, until=_clock_text(used_up_until), reason=text)
        elif cooldown:
            events.emit("llm_cooldown", endpoint=endpoint.name, seconds=round(cooldown, 1), reason=text)
        else:
            events.emit("llm_disabled", endpoint=endpoint.name, reason=endpoint.disabled_reason)


def _retry_after(exc: Exception) -> Optional[float]:
    """The Retry-After header of an HTTP error, in seconds, if there is one."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


# --- one shared router for the whole program ------------------------------------

_router: Optional[Router] = None
_router_lock = threading.Lock()


def get_router() -> Router:
    global _router
    with _router_lock:
        if _router is None:
            endpoints, strategy = load_endpoints()
            _router = Router(endpoints, strategy=strategy, max_wait=LLM_MAX_WAIT_SECONDS)
        return _router


def reset_router() -> None:
    """Forget the shared router so the next call re-reads the config. For tests."""
    global _router
    with _router_lock:
        _router = None
