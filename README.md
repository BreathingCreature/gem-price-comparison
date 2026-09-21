# GeM Price Comparison

Price comparison pipeline for Government e-Marketplace (GeM) products against Flipkart.

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│  GeM Scraper│────▶│  Raw Data   │────▶│  Per-Product     │────▶│  Matching   │
│  (requests) │     │  (CSV/SQL)  │     │  Lookup (Selenium)   │  Engine     │
└─────────────┘     └─────────────┘     └──────────────────┘     └─────────────┘
                                                                        │
                    ┌─────────────┐     ┌─────────────┐              │
                    │   Flipkart  │────▶│  Raw Data   │──────────────┘
                    │   Scraper   │     │  (CSV/SQL)  │
                    │  (Selenium) │     └─────────────┘
                    └─────────────┘
```

## Features

- **Category-aware matching**: Automatically detects product category (IT peripherals, stationery, furniture, electrical) and applies appropriate matching strategy
- **Soft gates**: Brand, model tokens, pack size, and form factor as weighted signals (not hard filters)
- **LLM attribute extraction**: Uses NVIDIA NIM free tier to extract brand/model/specs for ANY product
- **Real-time API**: `GET /match?gem_url=<URL>` for on-demand price comparison
- **Caching**: LLM results cached in SQLite, keyed by GeM product link

## Quick Start

### Installation

```bash
# Install dependencies
pip install -e ".[dev]"

# Or install production only
pip install -e .

# Install Playwright browsers (for Flipkart scraping)
playwright install chromium
```

### Configuration

Copy `.env.example` to `.env` and fill in your NVIDIA API key:

```bash
cp .env.example .env
# Edit .env and add your NVIDIA_API_KEY
```

Get a free API key at https://build.nvidia.com (no credit card required).

### Usage

```bash
# Scrape GeM products
python -m gem_price scrape-gem --query "office chair" --max-results 20

# Scrape Flipkart products  
python -m gem_price scrape-flipkart --query "office chair" --max-results 30

# Per-product Flipkart lookup (for specific GeM products)
python -m gem_price per-product-lookup --gem-ids 1,2,3

# Load scraped data into database
python -m gem_price load-data

# Run matching engine
python -m gem_price match-engine --gem-id 42

# Start API server
python -m gem_price api
# Then: curl "http://localhost:8000/match?gem_url=https://mkp.gem.gov.in/..."
```

## Project Structure

```
src/gem_price/
├── core/           # Configuration, logging
├── matching/       # Matching engine, LLM attributes
├── scrapers/       # GeM, Flipkart, query builder, per-product lookup
├── api/            # FastAPI endpoint
├── db/             # Schema, data loading
├── utils/          # Text processing utilities
└── cli.py          # Main CLI entry point
```

## Matching Pipeline

1. **Category Detection**: Heuristic keywords → IT peripherals, stationery, furniture, electrical
2. **Query Building**: Category-specific Flipkart search queries from GeM slug + LLM attributes
3. **Embedding**: all-MiniLM-L6-v2 cosine similarity (0-1)
4. **Soft Gates**: Model tokens, pack size, form factor (weighted per category)
5. **Composite Score**: Weighted sum of cosine + gates
6. **Threshold**: Configurable per-category threshold

## Configuration

All settings via environment variables (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `GEM_MAX_RESULTS` | 20 | Max products per GeM query |
| `FLIPKART_MAX_RESULTS` | 30 | Max products per Flipkart query |
| `COSINE_THRESHOLD` | 0.55 | Minimum cosine for match |
| `COMPOSITE_THRESHOLD` | 0.6 | Minimum composite score |
| `NVIDIA_MODEL` | openai/gpt-oss-20b | LLM model for attribute extraction |
| `API_PORT` | 8000 | API server port |

## Testing

```bash
pytest tests/ -v
```

## License

MIT