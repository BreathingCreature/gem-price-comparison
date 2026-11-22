# Accuracy and live run report

This report covers the live runs in `cache/runs/` and the fixes made after
inspecting their traces. Paths are relative to the repo root.

## Runs inspected

- `1568c4648bf54f6a`, CWC PB5020N/W monochrome laser printer, 2026-11-22.
  Zero matches, zero possible, 0 of 4 shops searched cleanly.
- `6327b9d5c5d14d9e`, Acer Aspire Lite AL15-41, 2026-10-20.
  One Flipkart match, one possible, no image or specs in the saved result.

## Fixed in this pass

### GeM product image was corrupted by the redaction filter

The printer run saved
`https://assets-mkpbg.gem.gov.in/[hidden].png.3d4df6158b.999x200x200.jpg`.
`core/errors.py` treated any 32 character alphanumeric run as a possible
secret and replaced part of the hashed CDN filename with `[hidden]`. The
browser then received an invalid URL, so no image loaded.

Fix: `redact()` now splits URLs and data URIs from prose. Explicit key shapes
and `api_key=` query parameters are still hidden in URLs; the broad hex and
base64 rules apply only to prose. Tests in `tests/test_redact.py` cover the
hashed GeM URL and a key inside a query string. A live extraction of both the
printer and the Acer page now returns a valid `https` GeM image.

Old saved runs still carry the broken URL. New runs are correct.

### PDF links were scraped as product pages

The printer run's discovery output included seven document links, for example
`https://cdri.res.in/pdf/tender/Tender_Gem_Cartridge_80.pdf` and
`https://brahmaputraboard.gov.in/storage/policy/sor_edition2.pdf`. One raised an
SSL error and others were fetched as pages. The trace shows
`raw_page_text` of up to 1,620,423 characters and a `structured_fields.title`
of 1,014,119 characters of binary PDF data. Those values were then sent to the
LLM, which explain the warnings about 635,139 token prompts and empty answers.

Fixes:
- `discovery/discover.py` rejects document path suffixes in
  `_looks_like_listing_url`, in both open and strict modes.
- `scrapers/generic.py` refuses responses whose content type or leading bytes
  say PDF or office document, and caps any page title at 200 characters.

This prevents the bad inputs at the source. A second layer that caps
`candidate.structured_fields` before `llm/client.py::verify_or_extract` is
still worth adding; see unresolved items.

### Shop icons for unknown domains

Only 12 domains have bundled PNGs. Other shops used a globe because the icon
route only returned a cached icon after a previous fetch. `api/server.py` now
has `_warm_shop_icons()`, started on a daemon thread after each real run. It
fetches and caches a favicon once per domain through the existing safe icon
code, tries the bare and `www.` hosts, and skips bundled and direct scraper
domains. The next page load reads it from the disk cache. Tests cover the
domain selection and the `www.` fallback.

### LLM card facts

`llm/highlights.py` prompt version is now v2. It asks for buyer-relevant
properties by product type, for example duty cycle and page yield for a
printer, and tells the model to skip BIS codes, tender ids, country of origin,
operating temperature and power supply unless nothing better exists. This
addresses the low value facts seen on the printer card.

## Unresolved, needs a planner decision

### Flipkart missed a listing that exists

Google search for `CWC PB5020NW printer flipkart` finds the exact Flipkart
product: "CWC PB5020N/W Printer with print resolution 2400 x 600dpi, 1 Years
Warranty, Single Function WiFi Monochrome Laser Printer", listed at 30,500 INR
MRP and about 14,424 INR, out of stock. GeM price is 9,400 INR, so even if it
had been found it would have been a dearer match, not a cheaper one.

The run's Flipkart searches used `CWC PB5020N/W laser printer`,
`CWC PB5020NW A4 legal printer` and other variants. Each returned 15 listings,
but the parsed cards were toner cartridges, drum units and laptop chargers.
A direct `curl_cffi` fetch of Flipkart search with the same queries returned
the same kind of noise. The product is indexed by Google but not surfaced by
Flipkart's own bot-facing search, and it is out of stock.

Proposed work:
- Add a search assist step that takes Google or Brave product URLs for known
  model codes and feeds the Flipkart and Amazon product URLs directly to the
  site scrapers, instead of dropping them because they are direct scraper
  domains.
- Normalise model codes before search: strip the slash (`PB5020N/W` becomes
  `PB5020NW`) and keep the original form as an alias.
- Treat out-of-stock listings honestly. The current result schema has
  `in_stock`; a product page with an add to cart block must be reported as a
  possible or unavailable match, never as the cheapest price.

### Acer run

The October run saved `image=None` and no specs because it predates the GeM
image and spec extraction work. A live extraction of the same URL today gives
a valid image and 32 specs, so no code change is needed. That run did find one
Flipkart match, so the reported miss does not apply to it.

### Context size defence

The PDF fixes stop the known source, but a generic scraper can still return a
large JSON-LD blob. Add a size cap in `llm/client.py::verify_or_extract` for
`candidate.structured_fields`, keeping title, price, currency, brand and
model and truncating other string values to a few hundred characters. The
prompt text itself is frozen, but its inputs are not.

### Other accuracy items

- Add a fallback to use the GeM title alone when a model code is very short or
  has punctuation, so `PB5020N/W` still finds `PB5020NW`.
- When a shop returns only accessories for a product query, extend the query
  with the category and brand but exclude accessory words such as toner,
  cartridge, drum, adapter and charger.
- The `similar products` step fails when any provider is busy or used up. It
  is optional, but a fallback that lists the top three relevant scraped
  listings without an LLM call would keep the panel useful.
