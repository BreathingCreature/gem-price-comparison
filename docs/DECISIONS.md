# Decisions
Choices that still shape the project: what we chose, why, and what we gave up.

## Identity and matching

- One listing key for every caller: `core/keys.py::listing_key`. The pipeline
  and the scorer must agree, or the same URL can be one listing during a run
  and two during scoring. Amazon collapses to the ASIN; Flipkart keeps `pid`
  and `lid`, and two URLs with the same `pid` but different `lid` are
  different variants; other shops keep host, path and real parameters while
  dropping tracking ones. The cost is less deduplication.
- GeM prices carry `{low, high, kind}`. `kind` is fixed, l1, range or unknown,
  and the low end remains the comparison price. This keeps ranges honest
  without pretending the page had a single number.
- The scraper's card price is the price. If the LLM reads a different number,
  the result keeps the scraper value and records the disagreement. An MRP-only
  page cannot be detected yet, so it is treated like a normal price.
- Every judged listing gets one outcome: match, possible, no match or not
  checked. A possible row never feeds cheapest or savings. A same-product
  verdict with no price needs code proof of identity to become a match.
- Verdict reuse is on: the same product plus listing title and price is one
  question, even across shops, and a reused decision keeps the current URL.
- A price outside the GeM price band is rejected after the LLM verdict, not
  trusted because the model was confident. Confidence alone is not evidence.

## Search and scraping

- Flipkart has a plain HTTP fast path before Chrome. It is used only when the
  page looks like real results, which saves the dominant browser start-up cost
  without treating a CAPTCHA shell as an empty shop.
- Chrome is plain Selenium with a CDP mask. undetected-chromedriver is not
  installed by default because it fails headless on the current Chrome.
- Amazon uses curl_cffi first and Selenium only as fallback. The TLS
  fingerprint matters more than the user agent, and one reused session per
  fingerprint avoids paying the handshake for every page.
- Struck-out MRP containers are removed before any Flipkart price read, and
  Amazon reads the selling-price span rather than the list-price span. A
  drifted class must not turn an old price into the current price.
- The generic scraper reads JSON-LD before meta tags, then page headings. A
  title without a price is kept with `price: null`; a soft 404 is marked. Non
  INR prices are kept so the matcher can reject them.
- Generic scraping refuses non-public addresses before the request and after
  redirects. The guard is scheme and IP-literal based. Resolving every host
  would duplicate DNS and still race.
- Discovery asks its engines at the same time and takes the union. One
  deadline covers the call. Engines still running at the deadline are
  cancelled and noted.
- Engine health is in memory only: three failures in a row skip an engine for
  five minutes, and a restart retries everything.
- Discovery is limited to Indian retail: any `.in` or `.co.in` domain passes,
  and known Indian marketplaces on `.com` are listed. A genuine foreign
  listing is not discovered. Flipkart and Amazon are excluded because they
  have direct scrapers; otherwise the same listing arrives twice and the
  second fetch usually gets a block page.

## LLM use

- The router tries endpoints in order and moves on after a failure. A 404
  retires a model on disk; a quota answer marks the provider used up; a plain
  rate limit gets a short cooldown. Status rows say what happened (ready,
  cooling, failing, used up, retired), and a stale error is labelled old.
  An empty answer is retried once on another endpoint and never cached.
- Shared LLM state is merged between processes, so two runs can share one free
  quota without erasing each other's facts.
- The pipeline survives an unavailable LLM. Normalization falls back to the
  GeM title, and affected listings become not checked with one warning.
  A comparison with no verification is less useful, but it is still honest.
- Similar products are one call, made only when nothing matched or came close.
  They never feed cheapest or savings because a similar product is different.

## Pipeline and result

- Lanes are the default. Flipkart, Amazon and discovery search at the same
  time, and each lane verifies its own listings as they arrive. The sequential
  path stays behind `GEM_PARALLEL=0` for tests and debugging.
- The first complete result is emitted before any retry, with state `shown`.
  The page should not wait for a slow shop to show what healthy shops found.
- A shop card must say what happened. Found needs a confirmed match; a crash
  is incomplete; a slow lane with no verdict is timed out; possible-only is
  possible. A listing that was never checked is never reported as not found.
- Retry is per shop, at most once, and only for retryable cards. A blocked
  shop is retried because new words may avoid the block. A crashed lane is not
  retried because the same call would crash again.
- A retry merges with the first pass. One unfinished shop must not erase a
  match another shop already confirmed.
- The result is built in one place, `core/result.py::build_result`, and carries
  `result_version: 2`. Old top-level keys stay for one release so the CLI,
  evaluator and page keep working. Changing the version invalidates old cache
  entries instead of reading a v1 result as v2.
- Run records live in `cache/runs` and store the whole v2 result, so the page
  can reopen a finished run after a restart. Traces are compact JSON because
  nothing reads them by eye; the API trims page text on the way out, not at
  write time, so the saved trace stays useful. The cost is a larger file,
  which trace rotation already bounds.

## Interfaces and operations

- The API binds to localhost by default. It is a personal tool, not a public
  service. CORS is empty unless origins are added explicitly.
- Submitting the same URL twice returns the same queued run. The queue reports
  its position, and each job keeps its event count and error for late readers.
- The demo runs the real pipeline with fake shops and a fake matcher. It has
  the same event order as a real run and never reaches a shop.
- Formatting lives in `frontend/render.js`, which is testable without a
  browser. Candidate URLs only become links through the scheme guard.
- The CLI keeps watching after the first result for a bounded time. `--no-wait`
  stops at the first result. A terminal should not be held open forever.
- A non-GeM URL exits with code 2 before the pipeline starts.
- `tools/doctor.py --live` runs every live check: LLM endpoints, search engines,
  shop canaries, one real Flipkart and Amazon search, and a deliberate
  failover. The narrower `--live-llm`, `--live-engines`, `--live-shops`,
  `--live-scrape` and `--live-failover` switches debug one part.
- `--clean` is opt-in. Normal rotation already bounds traces and run records,
  so deleting files is never part of a routine check.
- Requirements are pinned. The optional `ddgs` engine is commented out
  because a version that was never installed would be a guess.
- Tests hide curl_cffi and the direct scraper seams, so no test can reach a
  live shop through a socket path the network guard cannot see. The evaluation
  reader accepts v1 and v2 matches; new runs always write v2.
