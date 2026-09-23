# Assembly Log — CCode

Log of everything done to turn the flat `Files/` dump into the assembled,
working project in `CCode\`. Written 2026-09-23.

## Starting point

- `Files/` contained a flat dump of Claude's generated project:
  - Code: `config.py`, `gem_extractor.py`, `llm_client.py`, `discovery.py`,
    `matcher.py`, `pipeline.py`, `cli.py`, and `base.py`, `amazon.py`,
    `flipkart.py`, `generic.py` (which belong inside a `scrapers/` package).
  - Tests: `test_cli.py`, `test_discovery.py`, `test_gem_extractor.py`,
    `test_llm_client.py`, `test_matcher.py`, `test_pipeline.py`, `test_scrapers.py`.
  - `requirements.txt`, `env.example`, `gitignore` (no dot).
  - Build plans: `gem-price-comparison-buildspec.md` + `(1)`..`(7)`.
    **Latest = `gem-price-comparison-buildspec (7).md`.**
  - Full chat export: `Claude_export_Building a price comparison marketplace
    scraper_....md`.
- Status per latest plan: **Parts 0–7 built by Claude, 57 tests, all passing.**
  **Part 8 (`trace_viewer.html`) was never built** (conversation ended at Part 7).
  Skipped by design — the CLI's "open trace_viewer.html" line is non-functional.

## Decisions (confirmed with user)

1. Assemble directly in `CCode\` (NO `gem-price-compare/` wrapper folder).
2. Rename `Files\` → `RAW\` as the untouched backup.
3. Skip Part 8 / `trace_viewer.html`.

## Steps performed

1. Created folders `CCode\scrapers\` and `CCode\tests\`.
2. Renamed `Files\` → `RAW\`.
3. Created empty `scrapers\__init__.py` (required for `from scrapers import ...`).
4. Copied into `scrapers\`: `base.py`, `amazon.py`, `flipkart.py`, `generic.py`.
5. Copied into `tests\`: all 7 `test_*.py` files (tests expect to live in a
   `tests/` subfolder — they insert `parent.parent` into `sys.path`).
6. Copied to `CCode\` root: `config.py`, `gem_extractor.py`, `llm_client.py`,
   `discovery.py`, `matcher.py`, `pipeline.py`, `cli.py`, `requirements.txt`.
7. `gitignore` renamed → `.gitignore`.
8. `env.example` copied → `.env` (template only; the real `NVIDIA_API_KEY` is
   read from the user's shell env and `load_dotenv()` won't override it).
9. Created venv: `python -m venv .venv` (Python 3.14.7).
10. Installed requirements: requests 2.34.2, beautifulsoup4 4.15.0,
    lxml 6.1.3, selenium 4.49.0, python-dotenv 1.2.3, openai 3.19.0.
11. Ran all 7 test files → **57/57 passed, 0 failed.** (Warnings shown during
    tests are expected — they're the mock tests simulating failures/edge cases.)
    - test_cli: 7, test_discovery: 8, test_gem_extractor: 10,
      test_llm_client: 13, test_matcher: 5, test_pipeline: 6, test_scrapers: 8.

`traces\` and `cache\` are created automatically at runtime by `config.py`
(paths are relative to the project root).

## How to run

```powershell
# venv python lives at CCode\.venv\Scripts\python.exe

# tests (each file has its own runner — NOT pytest, NOT unittest discover)
& ".\.venv\Scripts\python.exe" tests\test_cli.py
& ".\.venv\Scripts\python.exe" tests\test_discovery.py
# ... etc for each test_*.py file

# CLI (needs NVIDIA_API_KEY + live network + Chrome for Amazon/Flipkart)
& ".\.venv\Scripts\python.exe" cli.py "<gem_url>"
& ".\.venv\Scripts\python.exe" cli.py "<gem_url>" --refresh --json
```

## Notes / gotchas

- `python -m unittest discover` runs **0 tests** — the test files use plain
  `def test_*()` functions with their own built-in `if __name__ == "__main__"`
  runners. No pytest in requirements (correct per plan).
- `.env` is a template — put the real NVIDIA key there OR rely on the existing
  shell env var `NVIDIA_API_KEY`.
- GeM, Google, Amazon, Flipkart live scraping is UNVERIFIED so far — the tests
  prove parsing logic against mock HTML, not the live sites. First real run is
  the milestone (per Claude's notes in the plan).
- Remarks from tests are expected:
  - GeM selectors proven on listing pages; single-product template may differ.
  - Google search scraping is the shakiest part (blocked/CAPTCHA risk;
    DuckDuckGo HTML fallback is documented in `discovery.py`).
  - Amazon selectors are unverified (no prior tested code existed).
  - Flipkart selectors from prior tested code; may drift.

## File map (CCode\)

```
CCode\
├── RAW\                       <- untouched original backup (this log lives here)
├── .env                       <- copied from RAW\env.example
├── .gitignore                 <- renamed from RAW\gitignore
├── requirements.txt
├── config.py
├── gem_extractor.py
├── llm_client.py
├── discovery.py
├── matcher.py
├── pipeline.py
├── cli.py
├── scrapers\
│   ├── __init__.py            <- created
│   ├── base.py
│   ├── amazon.py
│   ├── flipkart.py
│   └── generic.py
├── tests\                     <- all 7 test_*.py
├── .venv\                     <- created, deps installed
├── traces\                    <- runtime, auto-created
└── cache\                     <- runtime, auto-created
```