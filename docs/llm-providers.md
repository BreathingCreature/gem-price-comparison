# LLM providers

The project asks for an answer with `llm.client._call_llm(...)`. It never
says which provider answers. `llm/router.py` decides, call by call.

## Adding a provider

1. Copy `llm_providers.example.json` to `llm_providers.json` (that file is
   git-ignored, so keys and choices stay on your machine).
2. Add an entry:

```json
{
  "name": "mycloud",
  "base_url": "https://api.mycloud.example/v1",
  "api_key_env": "MYCLOUD_API_KEY",
  "models": ["some-model-name"],
  "min_seconds_between_calls": 2.0,
  "timeout_seconds": 60,
  "style": "plain"
}
```

3. Put `MYCLOUD_API_KEY=...` in `.env`.
4. Run `python tools/doctor.py --live-llm`. Each endpoint should say it answered.

Any provider with an OpenAI-compatible chat endpoint works. `style` is
`plain` for everyone except NVIDIA (`nvidia` adds the NVIDIA-only options).
Check the provider's own site for current model names, they change.

An entry with a missing or placeholder key is listed but switched off, so
you can leave entries in the file and fill in keys later.

## Per-model request options

Some providers need extra request parameters beyond `model`, `messages`,
`temperature`, and `max_tokens`. Add a `request_options` object to a
provider entry and its contents are merged into the `chat.completions.create()`
call for that endpoint:

```json
{
  "name": "groq",
  "base_url": "https://api.groq.com/openai/v1",
  "api_key_env": "GROQ_API_KEY",
  "models": ["openai/gpt-oss-120b"],
  "min_seconds_between_calls": 2.1,
  "timeout_seconds": 60,
  "style": "plain",
  "request_options": {"reasoning_effort": "low"}
}
```

If `request_options` contains an `extra_body` key, it is merged into the
`extra_body` dict; every other key is passed through as a top-level
parameter. `request_options` must be a JSON object; anything else is a
configuration error.

## Limits when several calls run at once

With `GEM_LLM_WORKERS` above 1 (see docs/evaluation.md), several listings
are verified at the same time and can reach one provider in parallel. Three
optional numbers per provider keep that polite; all are off by default:

```json
{
  "name": "groq",
  "base_url": "https://api.groq.com/openai/v1",
  "api_key_env": "GROQ_API_KEY",
  "models": ["openai/gpt-oss-120b"],
  "max_concurrent": 1,
  "daily_limit": 1000,
  "tokens_per_minute": 6000
}
```

- `max_concurrent`: how many calls may be in flight at once for this
  provider. A call that finds the provider at its cap waits for a free slot
  instead of stacking up more requests. `0` or missing = no limit.
- `daily_limit`: stop using the provider after this many calls in one day
  (counted per provider, shared by its models). It is then skipped cleanly
  instead of answering 429 for the rest of the day. `0` or missing = no
  limit. The count starts fresh when the calendar day changes.
- `tokens_per_minute`: the provider's token allowance per minute (free
  plans announce this; it is the real 429 limit). The router keeps a 60 s
  moving count per provider: a call is counted when it starts (estimated
  from the prompt's size) and the count is replaced by the provider's real
  reported usage once the call finishes. A provider whose next call would
  go over is skipped in favour of one with room; if everyone is out of
  tokens the call waits for the oldest use to age out of the window. A
  provider that has sent nothing in the last minute always gets one call,
  so a small value throttles it instead of bricking it. `0` or missing =
  no limit.

## How it chooses

- `"strategy": "failover"` (default): the first endpoint in the file is used
  while it is ready. The next one only takes over when it is resting or
  was used too recently (`min_seconds_between_calls`).
- `"strategy": "spread"`: ready endpoints take turns, the least recently
  used goes first. Use this to share load.

When a call fails the endpoint rests, and the call moves on to the next one
straight away:

| failure | what happens |
|---|---|
| 429 (rate limit) | rests 30 s, or the provider's Retry-After |
| 5xx, timeout, network | rests 10 s |
| same endpoint failing again | rest doubles each time, up to 5 minutes |
| 401 (key rejected) | every endpoint of that provider is switched off |
| 404 (model not found) | that model is switched off, remembered on disk, and the next model is used |
| empty answer | counts as a failure |

A 404 is saved in `cache/llm_state.json`, so a restart goes straight to the
next model instead of spending a call on the dead one. After
`GEM_RETIRED_RECHECK_DAYS` (7 by default) the model is tried once more, in
case the provider brought it back; a working answer deletes the note. The
status rows show `"retired": true` for those models.

Free model names change often. Two tools keep the list current:

- `python tools/check_models.py` asks two NVIDIA models two JSON questions (a
  product match and an EMI price pick). `--write` puts the good ones, fastest
  first, into the NVIDIA entry.
- `python tools/free_models.py` discovers candidates from the provider's own
  model list and asks one price question per model, using the matcher's token
  budget (`NVIDIA_MAX_TOKENS`, 2000) so a model that thinks for a long time is
  measured as the matcher will see it. OpenRouter candidates come from
  `GET /api/v1/models` and must have price 0 in and 0 out with text in and text
  out (audio, guardrail and "agentic harness only" models drop out); NVIDIA
  candidates come from `GET /v1/models`, filtered to chat families (embeddings,
  rerankers, vision-only and guard models are skipped). `--write` keeps the
  fastest correct models per provider (`--keep`, default 5).

OpenRouter's free tier allows about 20 calls a minute and 50 a day, so
`free_models.py` probes at most 10 of them by default (`--limit`), spacing
calls 3.2 s apart. Each probe spends real quota; widening `--limit` to cover
every free model uses most of a day's calls.

If every endpoint is resting, the call waits for the first one to return, up
to `LLM_MAX_WAIT_SECONDS` (45 by default), then fails with a message that
lists each endpoint's last error.

With no `llm_providers.json` at all, NVIDIA is used alone, built from the
`NVIDIA_*` settings in `.env`, as before.

## Seeing what it does

`GET /api/llm/status`, the Advanced tab of the web page, and the
`llm_call` / `llm_cooldown` events all show which endpoint served each call,
how long it took and why an endpoint was skipped. States are `ready`, `failing`
(its last call failed within 15 minutes — it will be retried, but it is not
"ready"), `cooling` (resting; `reason` names the remaining seconds and the
error), `limit` (allowance gone until the reset) and `disabled`. The status rows
also show each provider's `calls_today`, `in_flight`, `max_concurrent`,
`daily_limit`, `last_status` / `last_failure_ago` and the token budget with the
last minute's count (`tokens_per_minute`, `tokens_last_minute`).

`python -m tools.probe_endpoints` sends one tiny message to every endpoint
and prints OK with the round-trip time, or the real error (bad key, wrong
model name, limit, empty answer) — the quickest way to find a broken
provider before a run. Add `--json` for the rows as JSON; the exit code is 1
when any enabled endpoint failed.
