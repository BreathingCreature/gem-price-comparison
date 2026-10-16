"""Decides which LLM endpoint answers each call.

The rest of the project calls llm.client._call_llm() and never thinks about
providers. This module is the only place that knows there can be several.

An "endpoint" is one provider + one model, for example nvidia/nemotron or
groq/llama. Each endpoint keeps its own little record: when it was last
used, whether it is cooling down after an error, how often it failed.

For every call the router:
  1. skips endpoints that are disabled or cooling down,
  2. skips endpoints that were used too recently (their minimum gap),
  3. picks one of the rest, and
  4. if the call fails, puts that endpoint on a cooldown and tries the next.
If everything is cooling down it waits (up to LLM_MAX_WAIT_SECONDS), then
gives up with an error that says why.

Adding a provider means adding one entry to llm_providers.json. No code.
See llm_providers.example.json and docs/llm-providers.md.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

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
        for model in entry["models"]:
            ep = Endpoint(
                provider=entry["name"],
                model=model,
                base_url=entry["base_url"],
                api_key_env=entry["api_key_env"],
                style=entry.get("style", "plain"),
                min_gap=float(entry.get("min_seconds_between_calls", 0.0)),
                timeout=float(entry.get("timeout_seconds", 60.0)),
            )
            if not wanted:
                ep.enabled, ep.disabled_reason = False, "turned off in the config file"
            elif not key_ok:
                ep.enabled, ep.disabled_reason = False, f"{entry['api_key_env']} is not set"
            endpoints.append(ep)
    return endpoints, strategy


# --- the router -----------------------------------------------------------------

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
    ):
        self.endpoints = endpoints
        self.strategy = strategy
        self.max_wait = max_wait
        self.clock = clock
        self.sleep = sleep
        self._factory = client_factory or self._make_client
        self._clients: dict = {}
        self._lock = threading.Lock()

    # -- public ---------------------------------------------------------------

    def complete(self, messages: list[dict], max_tokens: int, purpose: str = "") -> str:
        """Return the model's raw text. Raises RuntimeError if nobody can answer."""
        deadline = self.clock() + self.max_wait
        while True:
            endpoint, wait = self._pick()
            if endpoint is None:
                if wait is None:
                    raise RuntimeError("No LLM endpoint is usable: " + self._why_unusable())
                if self.clock() + wait > deadline:
                    raise RuntimeError(
                        "All LLM endpoints are busy or failing: " + self._why_unusable(include_cooling=True)
                    )
                self.sleep(wait)
                continue
            raw = self._call(endpoint, messages, max_tokens, purpose)
            if raw is not None:
                return raw

    def status(self) -> list[dict]:
        now = self.clock()
        out = []
        with self._lock:
            for e in self.endpoints:
                if not e.enabled:
                    state = "disabled"
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
                        "reason": e.disabled_reason if state == "disabled" else "",
                        "cooldown_remaining": round(max(e.cooldown_until - now, 0.0), 1),
                        "calls": e.calls,
                        "errors": e.errors,
                        "avg_seconds": round(e.total_seconds / e.calls, 2) if e.calls else None,
                        "last_error": e.last_error,
                    }
                )
        return out

    # -- internals ------------------------------------------------------------

    def _pick(self) -> tuple[Optional[Endpoint], Optional[float]]:
        """(endpoint, 0) if one is ready now, (None, seconds) if one will be
        ready after that wait, (None, None) if none can ever be used."""
        with self._lock:
            now = self.clock()
            usable = [e for e in self.endpoints if e.enabled]
            if not usable:
                return None, None
            ready = [e for e in usable if e.cooldown_until <= now and now - e.last_start >= e.min_gap]
            if ready:
                if self.strategy == "spread":
                    chosen = min(ready, key=lambda e: e.last_start)  # least recently used
                else:
                    chosen = ready[0]  # config order is the priority order
                chosen.last_start = now  # claim the slot before anyone else can
                return chosen, 0.0
            wait = min(max(e.cooldown_until, e.last_start + e.min_gap) - now for e in usable)
            return None, max(wait, 0.05)

    def _why_unusable(self, include_cooling: bool = False) -> str:
        parts = []
        for e in self.endpoints:
            if not e.enabled:
                parts.append(f"{e.name}: {e.disabled_reason}")
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
        client = self._client_for(endpoint)
        started = self.clock()
        try:
            resp = client.chat.completions.create(
                model=endpoint.model,
                messages=messages,
                temperature=0.1,  # above 0 because nemotron's schema rejects exactly 0
                max_tokens=max_tokens,
                **_client._model_kwargs(endpoint.model, endpoint.style),
            )
            raw = (resp.choices[0].message.content or "").strip()
        except _client._endpoint_errors() as exc:
            self._record_failure(endpoint, exc, self.clock() - started, purpose)
            return None
        seconds = self.clock() - started
        if not raw:
            self._record_failure(endpoint, RuntimeError("empty answer"), seconds, purpose)
            return None
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
            else:
                base = RATE_LIMIT_COOLDOWN if status == 429 else OTHER_ERROR_COOLDOWN
                retry_after = _retry_after(exc)
                cooldown = retry_after if retry_after is not None else base * min(2 ** (endpoint.fail_streak - 1), 8)
                cooldown = min(cooldown, MAX_COOLDOWN)
                endpoint.cooldown_until = self.clock() + cooldown
        logger.warning("LLM endpoint %s failed (%s). %s", endpoint.name, text,
                       "Disabled." if cooldown == 0.0 else f"Resting it for {cooldown:.0f}s.")
        events.emit("llm_call", endpoint=endpoint.name, provider=endpoint.provider, model=endpoint.model,
                    purpose=purpose, seconds=round(seconds, 2), ok=False, status=status, error=text)
        if cooldown:
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
