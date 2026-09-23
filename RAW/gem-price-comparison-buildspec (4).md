# GeM Price Comparison Engine — Build Spec

Academic mini project (CSE7102 / PRJ 252): given a GeM (gem.gov.in) product URL, find the same product on other e-marketplaces and compare live prices. No paid APIs. Python. LLM = NVIDIA endpoint (env var `NVIDIA_API_KEY`), used as the primary matching/reasoning layer.

This doc is meant to be handed section-by-section to an AI (or read by you) to actually write the code. Each module below has a fixed input/output contract — build to the contract and the pieces will slot together regardless of who writes which part.

---

## 0. Ground truth and constraints (read before building anything)

- **`mkp.gem.gov.in` disallows automated access in its `robots.txt`.** A real GeM product URL was tested (`https://mkp.gem.gov.in/computer-mouse/logitech-m650-mouse-bt-usb/p-5116877-17935427244-cat.html`) and standard fetch tools refuse it outright. Playwright/Selenium browser automation isn't blocked by robots.txt technically, but be aware this is a documented crawl restriction — worth one honest line in the paper's methodology/limitations section (e.g. "the GeM portal was accessed via browser automation for non-commercial academic comparison purposes; robots.txt restrictions are noted as a limitation"). This doesn't change the build, it's a disclosure issue, not a technical blocker.
- **The NVIDIA LLM endpoint has no live internet access.** It's a text-completion API. It cannot literally "go research" a product on the web. Anywhere this doc says the LLM "finds" or "researches" something, what's actually happening is: we feed it text (a query, a raw page, tried-and-failed search terms) and it reasons over that text — it does not fetch URLs itself. The "retry" behavior in Part 6 works by having the LLM suggest *better search terms* based on its training knowledge (e.g. "Logitech mice are commonly sold on reliancedigital.in and croma.com in India too, not just Amazon/Flipkart") — those terms then go back through the real scraping layer (Part 3/4). Say this explicitly in the paper too; don't let it read as "the AI browses the internet," which it doesn't.
- **GeM product URLs encode useful info for free.** Pattern observed: `mkp.gem.gov.in/<category-slug>/<product-slug>/p-<id1>-<id2>-cat.html`. The product slug (`logitech-m650-mouse-bt-usb`) usually already contains brand + model + category hints. Parse this before touching a browser — it's a free, instant first signal and a fallback if the rendered page's DOM is messy.
- **Model number is not guaranteed on GeM listings** (GeM's own buyer documentation states brand/model filtering is optional per category). The extractor and matcher must both work when model number is missing, falling back to brand + product name + spec table.
- **Correction from an earlier draft of this doc: GeM does NOT need a browser.** GeM's product/category pages are server-rendered HTML. A real (even headless, even anti-detection-masked) browser gets redirected/blocked by GeM's bot check; plain `requests` + BeautifulSoup gets clean responses. This is confirmed two ways: your own earlier `gem_scraper.py` documents hitting exactly this wall with Selenium/Playwright and switching to plain requests successfully, and an unrelated 2022 public gist independently shows someone else hitting `mkp.gem.gov.in/home/search?q[]=...&_xhr=1` with a plain AJAX call. **This is the opposite of Amazon/Flipkart**, which block plain requests and need a real (masked) headless browser — don't apply the same tooling assumption to both.
- **Real, previously-tested GeM selectors** (from your own earlier code, reused here — verify against the exact product-detail URL format when building, since these were proven on category/listing pages and the single-product template may differ slightly): product name `.variant-title` (fallback `.variant-desc .variant-title`, then `.product-title`, then `h1`); price `.variant-final-price` (fallback `.price`, `.product-price`); seller/OEM `.sold_as.oem` or `.sold_as_summary` (fallback `.seller`, `.brand`); product/category links match `a[href*="-cat.html"]`.
- **On the old repo:** its overall architecture (permanent SQLite storage, embeddings+gates matching, Flipkart-only, duplicated FastAPI implementations) is not being reused — that's a separate, bigger system than what's being built here. But specific pieces of it are proven and worth porting rather than rewriting: the plain-requests GeM scraping method and selectors above, its price classifier (`fixed`/`range`/`L1_rate`/`unknown`), its robust LLM-JSON-response parser (handles markdown fences and stray prose), and its tested Flipkart Selenium scraper as a starting point. Each is called out in the relevant Part below.

---

## 1. Repo structure

```
gem-price-compare/
├── .env                      # NVIDIA_API_KEY lives here (gitignored)
├── .gitignore
├── requirements.txt
├── config.py                 # Part 0
├── gem_extractor.py          # Part 1
├── llm_client.py             # Part 2
├── discovery.py               # Part 3
├── scrapers/
│   ├── __init__.py
│   ├── base.py                # shared scraper interface
│   ├── amazon.py              # Part 4
│   ├── flipkart.py            # Part 4
│   └── generic.py             # Part 4 — fallback for unknown domains
├── matcher.py                 # Part 5
├── pipeline.py                 # Part 6 — orchestrator + retry + trace + cache
├── cli.py                      # Part 7
├── trace_viewer.html           # Part 8 — static, no server needed
├── traces/                     # JSON trace output per run (gitignored)
└── cache/                       # TTL cache storage (gitignored)
```

---

## 2. Shared data contracts

These shapes are used across multiple modules — defining them once so every part agrees on the same structure.

```python
# Product identity — the "who is this product" record
GemProduct = {
    "url": str,
    "category_slug": str,          # from URL, e.g. "computer-mouse"
    "product_slug": str,           # from URL, e.g. "logitech-m650-mouse-bt-usb"
    "title": str,                  # raw title as shown on GeM page
    "brand": str | None,
    "model": str | None,           # None if not present on listing
    "specs": dict[str, str],       # whatever spec table GeM shows, key: value
    "gem_price": float | None,
    "gem_price_type": "fixed" | "range" | "L1_rate" | "unknown",
    "currency": "INR",
}

NormalizedProduct = {
    "canonical_name": str,         # clean "Brand Model — short descriptor"
    "brand": str,
    "model": str | None,
    "key_identifiers": list[str],  # 2-5 terms that must match for it to be "the same product"
    "search_queries": list[str],   # ready-to-use search strings
}

CandidateResult = {
    "source_domain": str,          # "amazon.in", "flipkart.com", "reliancedigital.in", etc.
    "url": str,
    "scraper_used": str,           # "amazon" | "flipkart" | "generic"
    "structured_fields": {         # None if scraper failed to parse cleanly
        "title": str,
        "price": float | None,
        "currency": str,
        "seller": str | None,
        "image_url": str | None,
        "in_stock": bool | None,
    } | None,
    "raw_page_text": str | None,   # always populated when structured_fields is unreliable/None
    "fetch_error": str | None,
}

MatchDecision = {
    "candidate_url": str,
    "source_domain": str,
    "is_match": bool,
    "confidence": float,           # 0-1
    "reason": str,                 # short LLM-given justification
    "price_used": float | None,    # only set if is_match
    "extraction_source": "scraper" | "llm_raw_read",  # which data the LLM actually trusted
}
```

---

## 3. Part 0 — Skeleton & config *(built)*

**File: `config.py`**

- Loads `.env` (via `python-dotenv`), reads `NVIDIA_API_KEY`. Raise a clear error at startup if missing — don't let it fail silently three modules deep.
- Holds constants: `CACHE_TTL_SECONDS = 3600`, `MATCH_CONFIDENCE_THRESHOLD = 0.75`, `KNOWN_MARKETPLACE_DOMAINS = ["amazon.in", "flipkart.com"]`, `NVIDIA_MODEL_NAME = "<set once you confirm which model your endpoint serves>"`.
- Sets up a single logger (`logging.getLogger("gem_compare")`) that every other module imports rather than configuring its own. Never log the API key value, only whether it loaded.

**`requirements.txt`** (starting point): `requests`, `beautifulsoup4`, `lxml`, `selenium`, `python-dotenv`, `openai` (NVIDIA NIM endpoints are OpenAI-compatible, confirmed by the old repo's working code). No Playwright — it's the one tool proven not to work here (GeM blocks it); Selenium covers the retail scrapers where a real browser is actually needed.

Deliverable: `python -c "import config; print('OK')"` runs clean with the key loaded.

---

## 4. Part 1 — GeM Extractor *(built)*

**File: `gem_extractor.py`** — no browser needed, plain `requests` + BeautifulSoup (see section 0).

**Function:** `extract_gem_product(url: str) -> GemProduct`

Steps:
1. Parse `category_slug` and `product_slug` straight out of the URL path (regex) — free first-pass signal, no network call.
2. `requests.get(url, headers={"User-Agent": "<real browser UA>"}, timeout=15)`, polite ~2-3s delay if called in a loop. Parse with BeautifulSoup.
3. Extract via the selector cascade in section 0: name → `.variant-title` / `.variant-desc .variant-title` / `.product-title` / `h1`; price text → `.variant-final-price` / `.price` / `.product-price`; seller → `.sold_as.oem` / `.sold_as_summary` / `.seller` / `.brand`. Also grab whatever spec table exists — extract generically as key:value pairs rather than hardcoding field names, since structure varies by category.
4. Run the extracted price text through a `classify_price(text) -> (value, price_type)` helper: detect `L1_rate` (text contains "l1"/"lowest quoted"), `range` (two numbers with a separator), `fixed` (one number), else `unknown`. Store both the numeric value and the type — a GeM price is not always one clean number, and Part 5's matcher needs to know which kind it's comparing.
5. Return a `GemProduct`. If price or title can't be found, still return what was found — don't hard-fail, let downstream code decide it's insufficient.

**First real test for this part:** run it against `https://mkp.gem.gov.in/computer-mouse/logitech-m650-mouse-bt-usb/p-5116877-17935427244-cat.html` — the selectors above are proven on GeM's category/listing pages; confirm they hold on this single-product template too (detail pages sometimes diverge from listing-card markup) and adjust if not.

---

## 5. Part 2 — LLM Client Wrapper *(built)*

**File: `llm_client.py`**

Worth porting from the old repo's `llm_attributes.py` rather than rewriting: its OpenAI-compatible client setup (`OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=...)`), its rate-limiter (sleep to keep ≥1.6s between calls, staying under the free-tier request cap), and especially its `_extract_json()` — a brace-counting parser that recovers a JSON object even when the model wraps it in markdown fences or stray prose. That parser is more robust than a naive `json.loads()` + one retry. Model id used previously was `openai/gpt-oss-20b` — unverified as still current, check your NVIDIA catalog before relying on it.

One shared internal helper first:

**Function:** `_call_llm(system_prompt: str, user_content: str) -> dict`
- Calls the NVIDIA endpoint, forces JSON-only output (instruct this hard in the system prompt: "Respond with ONLY valid JSON, no markdown fences, no commentary").
- Parses the JSON with the brace-counting approach above; on parse failure, retries once with a "your last response wasn't valid JSON, return only JSON" correction message before giving up and raising.
- Returns the parsed dict plus logs the raw prompt/response pair to whatever trace object is currently active (see Part 6) — every LLM call must be traceable.

Three public functions built on top of `_call_llm`:

**1. `normalize_product(gem_product: GemProduct) -> NormalizedProduct`**
Input: the `GemProduct` dict. Output: `NormalizedProduct` as defined in Part 2 of the data contracts. This turns messy GeM listing text into a clean identity + ready search queries.

**2. `verify_or_extract(gem_product: NormalizedProduct, candidate: CandidateResult) -> MatchDecision`**
This is the combined "match verifier + fallback reader" you asked for. Input includes **both** `candidate.structured_fields` (what the scraper parsed, possibly `None` or garbage) **and** `candidate.raw_page_text` (the actual page content). Prompt instructs the LLM: "If structured_fields looks complete and trustworthy, verify against it. If structured_fields is missing/incomplete/looks wrong, read raw_page_text yourself and extract the real title/price, then decide if it's the same product." Output is a `MatchDecision`, with `extraction_source` telling you afterward whether the scraper's parse was trusted or the LLM had to self-correct — this field alone is a nice thing to report in the paper (how often did scrapers fail vs. how often the LLM caught it).

**3. `expand_search(gem_product: NormalizedProduct, tried_queries: list[str], tried_domains: list[str]) -> dict`**
Called only when Part 6's first pass finds zero confirmed matches. Returns `{"new_queries": list[str], "suggested_domains": list[str], "notes": str}`. This is the "ask the LLM to help find it elsewhere" step — built on the LLM's general knowledge (e.g. which Indian retailers commonly stock a given brand), not live browsing. `new_queries` feed back into Part 3, `suggested_domains` get added to the marketplace allow-list for that one retry pass only.

---

## 6. Part 3 — Discovery Layer *(built)*

**File: `discovery.py`** — now only responsible for domains *without* a dedicated scraper (brand sites, anything `expand_search` turns up). Amazon and Flipkart bypass this entirely: while building Part 4, it turned out the old repo's proven Flipkart code never visited individual product pages at all — it searched Flipkart's own results page directly and parsed structured data straight off the cards. Amazon (unverified, no prior code, but same reasoning) follows the same pattern. So Part 6's orchestrator calls `scrapers.flipkart.search(query)` and `scrapers.amazon.search(query)` directly with the normalized query, in parallel with this discovery step for everything else — it does not filter Google results down to flipkart.com/amazon.in URLs and hand them to a per-URL scraper.

**Actual functions built** (one query per call, not the batch-list shape originally sketched — matches Part 4's per-query trace pattern):

- `discover_candidates(query: str, allowed_domains: list[str] | None, session=None) -> DiscoveryResult` — one Google Search call. `allowed_domains=None` means open discovery (first pass, brand sites): keep everything except a soft blocklist of common non-retail domains (Wikipedia, Reddit, social media, GeM itself). `allowed_domains=[...]` means strict mode (used on `expand_search`'s retry pass): keep only exact/subdomain matches against that list. `DiscoveryResult` carries `query`, `raw_urls`, `filtered_urls`, `blocked`, `error` — raw and filtered are kept separate on purpose, that split is what Part 8's trace viewer shows.
- `discover_for_queries(queries: list[str], allowed_domains, session=None) -> list[DiscoveryResult]` — what Part 6 actually calls; one `DiscoveryResult` per query with a polite delay between calls.

Handles two Google result-link shapes (older `/url?q=` redirect wrapper, and modern direct `<a>` wrapping an `<h3>`) since either can show up depending on how a given request gets rendered.

**Honesty check, unchanged from the original plan:** this is the least proven part of the whole pipeline. There was no prior tested code for scraping Google Search to build on, unlike GeM and Flipkart. Google also blocks scrapers more aggressively and unpredictably than either of those. If `discover_candidates` comes back `blocked=True` often in practice, that's the known risk from day one, not a surprise — the code's own docstring notes DuckDuckGo's HTML endpoint (`https://html.duckduckgo.com/html/`) as a documented, more scraper-tolerant drop-in alternative if it comes to that; only `_fetch_search_html()` would need to change.

**Tests:** `tests/test_discovery.py` — mock-HTML tests for both link shapes, strict vs. open filtering, always-excluded Google domains, block detection, and error handling (8/8 passing). One of these caught a real bug: the open-discovery blocklist was checking exact domain-string membership instead of subdomain matching, so `en.wikipedia.org` slipped past a blocklist that only listed `wikipedia.org`. Fixed.

---

## 7. Part 4 — Site Scrapers *(built)*

**Files: `scrapers/base.py`, `scrapers/amazon.py`, `scrapers/flipkart.py`, `scrapers/generic.py`**

Two different contracts here, not one — this changed from the original plan once the proven Flipkart code turned out to work by searching directly rather than visiting product URLs:

- **`amazon.py` / `flipkart.py`** — `search(query: str, max_results=15) -> SearchScrapeResult`. Drive headless Chrome (`base.build_stealth_driver()`) to the site's own search page for a query, parse structured (title, price, link) straight off result cards. Flipkart's selectors and scroll/block-detection logic are ported from the old repo's tested `flipkart_scraper.py`. Amazon follows the identical pattern with widely-documented (but unverified-here) selectors — no prior tested code existed for it. Both use plain-function parsers (`parse_cards_from_html`) kept separate from the Selenium driving code specifically so they're unit-testable without a browser.
- **`generic.py`** — `scrape(url: str) -> CandidateResult`. Plain `requests`, not Selenium — for brand sites and anything without a dedicated scraper (including whatever `expand_search`'s retry turns up). Reads `og:title`/`og:price:amount`/`og:image` meta tags when present, always captures cleaned visible page text regardless, since that's Part 2's fallback signal when structured parsing comes up empty.

All three always populate `raw_page_text` alongside `structured_fields` (even when structured parsing succeeds) — Part 2's `verify_or_extract` wants both.

**Tests:** `tests/test_scrapers.py` — pure-function tests against mock HTML for all three (18/18 passing along with Part 1's tests). This sandbox can't install/run real Chrome, so nothing here proves the live selectors still match Flipkart/Amazon's actual current markup — only that the parsing logic is correct against HTML shaped like the real thing. Flipkart's shape is based on tested code; Amazon's is a best guess.

---

## 8. Part 5 — Matcher *(built)*

**File: `matcher.py`**

**Function:** `match_candidates(normalized: dict, candidates: list, *, client=None) -> MatchBatchResult`

For each candidate, calls `llm_client.verify_or_extract(normalized, candidate)`. One addition beyond the original sketch: returns both the full set of decisions (matched and rejected) and the confirmed subset — not just confirmed — since Part 8's trace viewer wants to show what happened with every candidate, and a rejection is as much a logged decision as a confirmation. `MatchBatchResult` carries `all_decisions`, `confirmed_matches` (is_match + confidence ≥ `MATCH_CONFIDENCE_THRESHOLD`, sorted by price ascending, unpriced matches sort last rather than crashing the sort), and `skipped` (candidate URLs where `verify_or_extract` itself raised — one bad LLM call doesn't take down the whole batch). Accepts either `CandidateResult` objects or plain dicts.

**Tests:** `tests/test_matcher.py` — 5/5 passing, covering the filter, the sort (including the no-price-sorts-last edge case), and that one raising candidate doesn't stop the rest of the batch from being scored. This one's thin enough there wasn't much room for a bug to hide — passed clean on the first run.

---

## 9. Part 6 — Orchestrator *(built)*

**File: `pipeline.py`**

**Function:** `run_pipeline(gem_url: str, refresh: bool = False, **overrides) -> dict`

Every collaborator (extract, normalize, flipkart/amazon search, discover, generic scrape, match, expand_search) is an overridable keyword argument, defaulting to the real Part 1-5 functions. That's not incidental — it's what let the orchestrator's own logic (sequencing, the retry trigger, cache, not_found_on computation) get tested in isolation from whether the real scrapers/LLM calls work, same as every other part.

Sequence: extract (1) → normalize (2) → Flipkart + Amazon direct search (4) *and* open-discovery + generic scrape (3+4) → match (5) → if zero confirmed matches, `expand_search` (2) then retry discovery+generic once with the suggested queries/domains, re-match → cache + trace → return `{gem_product, matches, not_found_on, from_cache, trace_id}`.

**Two scope decisions worth knowing about, both deliberate:**
- **The retry only re-runs discovery + generic scraping**, not a second Flipkart/Amazon search. That matches what you actually asked for early on — surfacing sites the dedicated scrapers don't cover — rather than just re-asking the same two marketplaces with different wording. If `expand_search` comes back with no new queries, the retry is skipped outright (logged in the trace as `skipped_reason`), not forced.
- **The trace captures structured input/output per stage** (queries used, candidates found, match decisions with their `reason` field) via the `to_dict()` every Part 1-5 object already has — not the raw LLM prompt/response text. Capturing that would mean reworking Part 2's already-tested functions to expose it, which felt like unnecessary risk on an already-working part for what it'd add. The `reason` field on each match decision is the closest thing to "why" without that deeper plumbing — worth adding later if Part 8's viewer turns out to need more.

**Cache:** a JSON file per GeM URL (SHA-256-keyed) in `cache/`, `{cached_at, result}`, checked against `CACHE_TTL_SECONDS` from config. `refresh=True` bypasses the read (but still overwrites with a fresh result after).

**Trace:** one JSON file per run in `traces/<trace_id>.json` — `extraction`, `normalization`, `flipkart_search`, `amazon_search`, `discovery`, `generic_scrapes`, `matching`, `retry`, `final_result`. This is exactly what Part 8's viewer will read.

**Tests:** `tests/test_pipeline.py` — 6/6 passing: cache hit skips every stage entirely, `refresh=True` bypasses a stale cache, expired cache entries are treated as a miss, a full happy-path run with one marketplace matching and one not, the retry actually triggering and succeeding via expanded discovery (asserted by call count, not just by inspection), and the retry being skipped cleanly when `expand_search` has nothing to offer. All passed on the first run — the dependency-injection design meant there wasn't really anywhere for a subtle bug to hide the way there was in Parts 1/3's parsing logic.

---

## 10. Part 7 — CLI *(built)*

**File: `cli.py`**

`python cli.py <gem_url> [--refresh] [--json]`

- Calls `run_pipeline(gem_url, refresh=...)`.
- Prints a plain table: GeM price vs. each matched marketplace price, cheapest marked with `*`.
- Prints `not_found_on` domains as "not found" lines, per your original spec.
- Notes when a result was served from cache.
- Prints the `trace_id` at the bottom: `"Full decision trail: open trace_viewer.html and load traces/<trace_id>.json"`.
- `--json` prints the raw result dict instead, for scripting/piping.

**Tests:** `tests/test_cli.py` — 7/7 passing, covering price formatting (including the no-price edge case, which doesn't crash or get wrongly marked cheapest), the cheapest-match marker, the no-matches path, and the cache notice.

---

## 11. Part 8 — Trace Viewer

**File: `trace_viewer.html`**

A single static HTML+JS file (no server, no build step — open it directly in a browser). A file-picker input loads a `traces/*.json` file and renders it as collapsible sections matching the trace shape above: Extraction → Normalization → Discovery → Scraping (per URL) → Matching (per URL, showing the LLM prompt/response) → Retry (if triggered) → Final Result. This is your "backend page" — cheap to build since it's just a dumb renderer over data the pipeline already produces in step 7 above.

---

## 12. Deferred — not building now

- **Non-LLM fallback matching** (keyword/fuzzy) for when the LLM is down or rate-limited.
- **Web frontend** (Spring Boot + HTML/CSS/JS) — once the pipeline works end-to-end via CLI, it's a thin REST wrapper (FastAPI is the fastest option) around `run_pipeline()`, no core logic changes needed.
- **Google Shopping** as a discovery source — sticking to plain Search for now per your call.

---

## 13. Recommended build order

Front-loading the riskiest, most-likely-to-break piece first so problems surface early, not after everything else is wired together:

**0 → 1 → 4 (Amazon + Flipkart scrapers, tested standalone against a URL you paste in by hand) → 2 → 3 → 5 → 6 → 7 → 8**

Part 1 is now the cheap, fast one to knock out first — no browser install, no selector guesswork, just `requests` against proven selectors. Part 4 is still where the real time goes (Selenium setup, Amazon has no prior art to start from).

Each part should be independently testable before being wired to the next one. Say which part to start on and I'll write the actual code for it against this contract.
