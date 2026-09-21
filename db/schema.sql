CREATE TABLE IF NOT EXISTS raw_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,           -- 'gem' or 'flipkart'
    name TEXT NOT NULL,
    price REAL,
    price_type TEXT,                -- only relevant for gem rows
    seller TEXT,
    link TEXT,
    scraped_at TEXT,
    searched_for_gem_id INTEGER     -- flipkart rows: which GeM product triggered this search
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
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_raw_source ON raw_products(source);
CREATE INDEX IF NOT EXISTS idx_raw_name ON raw_products(name);
