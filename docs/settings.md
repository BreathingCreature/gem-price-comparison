# Settings

Every knob is read once from the environment (`.env`) in `core/config.py`, or
from `llm_providers.json` for the provider limits. Defaults are the normal
behaviour; the switches exist so an evaluation can turn one feature off at a
time. On/off values: `1`, `true`, `yes`, `on` mean on; anything else means off.
An empty value means "use the default".

## Running

| Setting | Default | What it does | Risk |
|---|---|---|---|
| `NVIDIA_API_KEY` | (empty) | Key for NVIDIA's OpenAI-compatible API | Without it no LLM call can run; the page still reads the GeM product |
| `NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | NVIDIA endpoint | Change only if NVIDIA moves it |
| `NVIDIA_MODELS` | `nvidia/nemotron-3-super-120b-a12b` | Comma-separated model list | A wrong name gives a clear 404/400 |
| `NVIDIA_MAX_TOKENS` | `2000` | Answer budget per call | Too low cuts JSON answers; a reasoning model needs more |
| `NVIDIA_MIN_SECONDS_BETWEEN_CALLS` | `1.6` | Politeness gap for the old single-provider path | Lower may get 429s |
| `NVIDIA_TIMEOUT_SECONDS` | `90.0` | One HTTP call timeout | Too low = timeouts on a slow free tier |
| `LLM_PROVIDERS_FILE` | `llm_providers.json` | Provider list | Missing file = NVIDIA only |
| `LLM_MAX_WAIT_SECONDS` | `45.0` | Longest a call waits for a busy endpoint | Higher = slower, fewer skipped calls |
| `LOG_LEVEL` | `INFO` | Log verbosity | `DEBUG` is noisy and may include page text |

## LLM cache

| Setting | Default | What it does | Risk |
|---|---|---|---|
| `LLM_CACHE` | `0` | Remember LLM answers on disk | Replayed answers may be stale after a prompt change |
| `LLM_CACHE_DIR` | `cache/llm` | Where those answers live | — |
| `LLM_CACHE_TAG` | (empty) | Extra tag in the cache key | Use one per model when comparing models |

## Result and cache

| Setting | Default | What it does | Risk |
|---|---|---|---|
| `CACHE_TTL_SECONDS` | `3600` | How long a finished result is reused | A long TTL serves old prices |
| `MATCH_CONFIDENCE_THRESHOLD` | `0.75` | Below this an LLM match becomes "possible" | Lower = more matches, more wrong ones |
| `PRICE_SANITY_LOW_RATIO` | `0.15` | A "match" below 15% of the GeM price is rejected | Lower accepts impossible prices |
| `PRICE_SANITY_HIGH_RATIO` | `5.0` | A "match" above 5x the GeM price is rejected | Higher accepts wrong products |
| `RESULT_VERSION` | `2` | Shape of the result; part of the cache key | Do not change by hand |

## Pipeline behaviour

| Setting | Default | What it does | Risk |
|---|---|---|---|
| `GEM_PARALLEL` | `1` | Search the three lanes at the same time | `0` restores the old one-after-another path |
| `GEM_STRICT` | `0` | No filter, every listing to the LLM, one at a time | Slow and expensive; more recall |
| `GEM_RETRY` | `1` | Ask the LLM for better search words when nothing matched | Off = one pass only |
| `GEM_GATES` | `1` | Currency, price-band and confidence gates | Off = trust the model blindly |
| `GEM_DISCOVERY` | `1` | Search engines and other shops | Off = Amazon/Flipkart only |
| `GEM_DISCOVERY_TIME_LIMIT` | `25.0` | Total budget for one discovery call | Too low = other shops rarely finish |
| `GEM_FLIPKART_LIGHT` | `1` | Plain HTTP search page first, then light Chrome | Off may change what Flipkart returns |
| `GEM_LANE_TIME_LIMIT` | `150.0` | A lane over this is reported `timed_out` and retryable | The search cannot be killed; the scraper's own timeouts stop it |
| `GEM_LLM_WORKERS` | `4` | Listings verified at the same time | Provider limits; lower it on many 429s |
| `GEM_PREFILTER_TOP` | `10` | Best listings per shop sent to the LLM (`0` = no filter) | Too low can cut a real match |
| `GEM_PRICE_PREGATE` | `1` | Skip the LLM when the price is already impossible | Off = wasted calls |
| `GEM_REUSE_VERDICTS` | `1` | Reuse a verdict for the same listing and product | Off = ask twice |
| `GEM_SLIM_REFERENCE` | `0` | Send only the reference's identity fields to the verifier | Shorter prompt; answers can change |

## Files and traces

| Setting | Default | What it does | Risk |
|---|---|---|---|
| `GEM_TRACE_MAX_FILES` | `200` | Trace files kept before the oldest are deleted | Rotation runs every 25 writes |
| `GEM_TRACE_MAX_AGE_DAYS` | `14` | Traces older than this are deleted | `tools/doctor.py --clean` uses the same number |
| `GEM_DEBUG` | `0` | Keep full page text in API trace replies | Big responses; the trace file itself always keeps it |

## Server and tools

| Setting | Default | What it does | Risk |
|---|---|---|---|
| `GEM_DEMO` | `0` | Fake pipeline in the API (no scraping, no LLM) | Never use it for real numbers |
| `GEM_JOBS` | `1` | Runs executed at the same time | Two is fine; five starves Chrome |
| `CORS_ORIGINS` | (empty) | Comma-separated extra origins allowed to call the API | Empty = same origin only |
| `GEM_RETRY_CLI_WAIT` | `120` | Seconds the CLI keeps printing updates | `--no-wait` ignores it |

## Provider limits (`llm_providers.json`)

| Key | Default | What it does |
|---|---|---|
| `max_concurrent` | none | Calls in flight at once for a provider |
| `daily_limit` | none | Calls per provider per day, then skipped until the day changes |
| `tokens_per_minute` | none | 60 s moving token budget; a call is held until it fits |
| `tokens_used` / `used_up` | (state) | Learned state saved under `cache/`, survives a restart |

The "fast profile" page that used to live here has been merged into this table:
everything it recommended is now the default.
