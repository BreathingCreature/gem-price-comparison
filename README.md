# GeM Price Comparison

Pipeline that takes a GeM (Government e-Marketplace) product URL, extracts the reference product, then finds cheaper alternatives on Indian retail marketplaces (Amazon.in, Flipkart, brand sites) using scraping + LLM matching.

## Quick Start

```bash
# 1. Create virtual environment
python -m venv .venv
.venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure API key
copy .env.example .env
# Edit .env and add your NVIDIA_API_KEY

# 4. Run
python cli.py "https://mkp.gem.gov.in/.../p-12345-67890-cat.html"
```

## Commands

| Command | Description |
|---|---|
| `python cli.py "<gem-url>"` | Compare GeM price vs marketplaces |
| `python cli.py "<gem-url>" --refresh` | Bypass cache, force live check |
| `python cli.py "<gem-url>" --json` | Output raw JSON |
| `python tests\test_X.py` | Run individual test file (7 files) |

## Architecture

```
cli.py (entry)
  └── core/
      ├── config.py       # All settings, env loading, paths, logger
      └── pipeline.py     # Orchestrator: extract → discover → scrape → LLM → match → retry
  ├── scrapers/           # All web scraping
  │   ├── _base.py        # Contracts + browser factory + price parser
  │   ├── gem.py          # GeM page scraper (Part 1)
  │   ├── discover.py     # Search engines → candidate URLs (Part 2)
  │   ├── amazon.py       # Amazon.in (curl_cffi + Selenium fallback)
  │   ├── flipkart.py     # Flipkart (Selenium only)
  │   └── generic.py      # Any URL: og: tags + raw text
  ├── discovery/          # URL finding (search engines)
  │   └── discover.py     # DDG → SearXNG → Google chain
  └── llm/                # LLM intelligence + gates
      ├── client.py       # NVIDIA API, retries, prompts
      └── match.py        # Sanity gates (currency, confidence, price)
```

## Key Design Decisions

- **No LLM browsing** — LLM only verifies/expands; all fetching is plain code
- **Currency honesty** — Non-INR prices rejected before LLM call
- **Never crash on one bad URL** — Catch → log → return partial with `fetch_error`
- **Test isolation** — All tests inject `_FakeSession` via `session=` kwarg; zero network/LLM calls
- **Cache + Trace** — Per-URL JSON cache (TTL 1h), full decision trail in `traces/`

## Requirements

- Python 3.10+
- NVIDIA API key (free tier at https://integrate.api.nvidia.com)
- Chrome/Chromium for Selenium fallback (Amazon/Flipkart)

## Tests

```bash
# Run each test file individually (77 tests total across 7 files)
python tests\test_cli.py
python tests\test_discovery.py
python tests\test_gem_extractor.py
python tests\test_llm_client.py
python tests\test_matcher.py
python tests\test_pipeline.py
python tests\test_scrapers.py
```

> **Note:** `python -m unittest discover` runs 0 tests — use per-file runners above.

## Web app

Run the server and open http://localhost:8000:

```
python -m uvicorn api.server:app --port 8000
```

Paste a GeM product link and the page shows each step as it happens: the GeM
product, the Flipkart and Amazon results, the other shops, and the verdict on
every listing, with a running clock. The Advanced tab shows the LLM endpoints,
time per step, every LLM call and the raw event log.

To work on the page without scraping anything, start the server in demo mode
(`GEM_DEMO=1`, fake data, no LLM calls). Check that everything is wired up with
`python tools/doctor.py`.

More: `docs/architecture.md`, `docs/api.md`, `docs/llm-providers.md`.

## Evaluation

How to measure recall, precision and speed against a hand-made answer key: see [docs/evaluation.md](docs/evaluation.md).
