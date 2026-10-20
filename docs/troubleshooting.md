# Troubleshooting

Start with `python tools/doctor.py`. Every line says PASS, WARN or FAIL; the
exit code is 1 when something FAILED. `--live` adds the real calls (LLM
endpoints, search engines, one canary per shop) and says exactly which part is
broken.

## A shop card says "blocked" or "not_searched"

Amazon and Flipkart answer a bot check (CAPTCHA) for some networks, some days.
The card says `not_searched` with the reason, and the run retries that shop
once with fresh search words. If the retry also hits the wall, the card stays
as it is; other shops still show.

What to do:
- Wait an hour. Blocks are usually temporary.
- `python tools/doctor.py --live-shops` saves the page it saw under
  `debug_html/`. Open that file: a CAPTCHA page and a changed layout look very
  different.
- Run from a normal home network. Data-centre and VPN addresses are blocked
  far more often.

## "CAPTCHA" or "parser_empty"

`parser_empty` means the page loaded but no product cards were found by the
selectors. That is a layout change, not a block.

- Check the saved page from `--live-shops`.
- The parsers are `scrapers/flipkart.py::parse_cards_from_html` and
  `scrapers/amazon.py::parse_cards_from_html`; a fixture test makes a fix
  permanent. Save the page with
  `python tools/save_page.py flipkart "usb c cable"` and add a test with it.

## The LLM is exhausted / many 429s

The router remembers when an endpoint used up its allowance for the day and
skips it until tomorrow (`GET /api/llm/status` shows `limit`). It also cools an
endpoint after errors: `cooling` carries the remaining seconds and the error,
and `failing` means its last call failed within the last 15 minutes (the router
will retry it, but it is not "ready"). A 5xx such as Gemini's "currently
experiencing high demand" is rested for a minute, not ten seconds.

- `python tools/probe_endpoints.py` prints one line per endpoint, including the
  finish reason and the *names* of rate-limit headers.
- Add a second provider in `llm_providers.json` (see `llm-providers.md`).
- Lower `GEM_LLM_WORKERS` to 1 or 2, or set `max_concurrent` /
  `tokens_per_minute` on the provider.
- Strict mode costs many more calls; use normal mode while an allowance is
  tight.

## "model not found (404)" / the model stopped working

NVIDIA adds and removes free models often; nothing in the project changed.
The router already handled it: that one model is switched off, the note is
saved in `cache/llm_state.json`, and the next model in the list answers.

- `python tools/check_models.py` asks every candidate two small JSON questions
  and prints who answered, how fast, and whether the answers were right.
- `python tools/check_models.py --write` puts the good models (fastest first)
  into the NVIDIA entry of `llm_providers.json` and forgets their 404 notes.
- The retired models show as `disabled` with `"retired": true` in
  `GET /api/llm/status`. After `GEM_RETIRED_RECHECK_DAYS` (7) one is tried
  again by itself.
- If every NVIDIA model is gone, the other providers in `llm_providers.json`
  keep working; add one there if you have a second key.

## Chrome not found

Flipkart needs Chrome; Amazon tries plain HTTP (`curl_cffi`) first and uses
Chrome only as a fallback.

- Install Google Chrome (or Chromium). Selenium Manager finds it by itself.
- If it cannot, set the driver by hand: `scrapers/_base.py::resolve_chromedriver`
  looks for a `chromedriver` on `PATH` as its fallback.
- `python tools/doctor.py` imports every scraper module and warms the driver
  check; a missing driver shows up there before a run.

## No price, or a price that looks wrong

- The page may show a range. The low end is reported and the range is kept in
  the trace; a range is never turned into a single made-up price.
- A foreign price (USD, EUR) is kept as-is so the matcher rejects it. It is
  never converted at a guessed rate.
- "No Cost EMI from ₹99/month" is stripped before the real price is read
  (`scrapers/flipkart.py::strip_emi_text`).

## The CLI stops after the first result

By default it keeps watching the retry for up to `GEM_RETRY_CLI_WAIT` seconds
(120) and prints `Updated:` lines. If it says "Stopped waiting", the retry took
longer than that; the web page keeps watching. Use `--no-wait` to exit at once.

## "not a GeM product link"

The link must be an `http`/`https` URL on `gem.gov.in` or a subdomain. The CLI
exits 2 before fetching anything. Copy the address from the product page, not
from a search result.

## The web page shows nothing / EventSource errors

- The server binds `127.0.0.1`. Open `http://127.0.0.1:8000`, not another
  machine's address, unless you passed `--host 0.0.0.0` on purpose.
- If the page is served from another origin, set `CORS_ORIGINS` and
  `window.GEM_API_BASE`.
- `GET /api/health` should answer `{"ok": true, "demo": false}`.

## An old run cannot be opened any more

Traces and run records are rotated by `GEM_TRACE_MAX_FILES` and
`GEM_TRACE_MAX_AGE_DAYS`. `python tools/doctor.py --clean` removes older ones
on purpose. Everything else about the run is gone by design.

## "Cache" surprises

A result younger than `CACHE_TTL_SECONDS` (1 hour) is reused; the output says
"served from cache". Use `--refresh` (CLI) or `{"refresh": true}` (API) to
force a live run. The cache key includes the product and `RESULT_VERSION`, so
an upgrade never serves an old shape.
