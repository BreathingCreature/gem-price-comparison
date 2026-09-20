<<<<<<< HEAD
# GeM Price Comparison — PRJ 252

End-to-end pipeline for Review 2: Data Collection (GeM + Flipkart) → Storage (SQLite) → Matching (regex + fuzzy).

## Structure

```
code/
  scrapers/
    flipkart_scraper.py   # Phase 1 — Flipkart search scraper
    gem_scraper.py        # Phase 2 — GeM (gem.gov.in) catalog scraper
  db/
    schema.sql            # Phase 3 — SQLite schema
    load_data.py          # Phase 3 — CSV → SQLite loader
  matching/
    match_engine.py       # Phase 4 — regex hard-filter + rapidfuzz matching
  data/
    raw/                  # scraper CSV output + debug HTML
    processed/            # future: cleaned / matched exports
```

Database: **SQLite** (`db/gem_project.db`) — zero server setup. MySQL migration is a later swap.

## Setup (Phase 0)

```powershell
cd code
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install
```

## Usage

```powershell
# 1. Scrape Flipkart (tested: "office chair", "laptop", "printer")
python -m scrapers.flipkart_scraper --query "office chair" --max-results 30

# 2. Scrape GeM (public catalog only, never bidplus.gem.gov.in)
python -m scrapers.gem_scraper --query "office chair" --max-results 20

# 3. Load CSVs into SQLite
python -m db.load_data

# 4. Run matching engine
python -m matching.match_engine --threshold 75
```

Each scraper saves `data/raw/<source>_<query>_<timestamp>.csv` with columns:
`source,name,price,seller,link,scraped_at` (+ `price_type` for GeM).

## Guardrails

- Flipkart: 1–2s delay between requests. On block/CAPTCHA: stop and report, do not bypass.
- GeM: 2–3s delay between actions. On CAPTCHA/login wall: stop and report.
- GeM price types observed: `fixed` / `range` / `L1_rate` / `unknown` (reverse-auction pricing means a single fixed price may not exist).

## Review mapping

- Review 2 (Sep 26): Phases 1–3 working with real data ← this repo
- Review 3 (Oct 24): Backend API + Frontend (deferred, 80% completion target)

---

# gem-price-comparison

(Root README created at remote repo initialisation — merged in on first push.)
