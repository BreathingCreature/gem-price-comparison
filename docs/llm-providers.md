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
| 404 (model not found) | that endpoint is switched off |
| empty answer | counts as a failure |

If every endpoint is resting, the call waits for the first one to return, up
to `LLM_MAX_WAIT_SECONDS` (45 by default), then fails with a message that
lists each endpoint's last error.

With no `llm_providers.json` at all, NVIDIA is used alone, built from the
`NVIDIA_*` settings in `.env`, as before.

## Seeing what it does

`GET /api/llm/status`, the Advanced tab of the web page, and the
`llm_call` / `llm_cooldown` events all show which endpoint served each call,
how long it took and why an endpoint was skipped.
