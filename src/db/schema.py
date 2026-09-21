"""Database schema for GeM Price Comparison."""
# Schema with proper indexes and constraints

SCHEMA = """
-- Enable foreign keys (SQLite requires this pragma)
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS raw_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,           -- 'gem' or 'flipkart'
    name TEXT NOT NULL,
    price REAL,
    price_type TEXT,                -- only relevant for gem rows
    seller TEXT,
    link TEXT,
    scraped_at TEXT,
    searched_for_gem_id INTEGER,    -- flipkart rows: which GeM product triggered this search
    searched_for_gem_link TEXT      -- stable link for GeM product (for per-product lookup)
);

CREATE TABLE IF NOT EXISTS matched_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gem_product_id INTEGER REFERENCES raw_products(id),
    market_product_id INTEGER REFERENCES raw_products(id),
    similarity_score REAL,           -- cosine (0-1) for embedding method
    cosine_score REAL,               -- official score used for the match
    model_token_match INTEGER,       -- 1 if model-token gate passed
    pack_match INTEGER,              -- 1 if pack-size gate passed
    form_match INTEGER,              -- 1 if form-factor gate passed
    match_method TEXT,
    category TEXT,                   -- product category for this match
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS product_attributes (
    gem_link TEXT PRIMARY KEY,
    brand TEXT,
    model TEXT,
    key_specs TEXT,      -- JSON-encoded list
    search_query TEXT,
    source TEXT,         -- 'llm' or 'fallback_regex'
    created_at TEXT
);

-- Indexes for query performance
CREATE INDEX IF NOT EXISTS idx_raw_source ON raw_products(source);
CREATE INDEX IF NOT EXISTS idx_raw_name ON raw_products(name);
CREATE INDEX IF NOT EXISTS idx_raw_link ON raw_products(link);
CREATE INDEX IF NOT EXISTS idx_raw_searched_gem ON raw_products(searched_for_gem_id);
CREATE INDEX IF NOT EXISTS idx_matched_gem ON matched_products(gem_product_id);
CREATE INDEX IF NOT EXISTS idx_matched_market ON matched_products(market_product_id);
CREATE INDEX IF NOT EXISTS idx_matched_method ON matched_products(match_method);
"""