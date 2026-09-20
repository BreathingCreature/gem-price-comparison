CREATE TABLE IF NOT EXISTS raw_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,           -- 'gem' or 'flipkart'
    name TEXT NOT NULL,
    price REAL,
    price_type TEXT,                -- only relevant for gem rows
    seller TEXT,
    link TEXT,
    scraped_at TEXT
);

CREATE TABLE IF NOT EXISTS matched_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gem_product_id INTEGER REFERENCES raw_products(id),
    market_product_id INTEGER REFERENCES raw_products(id),
    similarity_score REAL,
    match_method TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_raw_source ON raw_products(source);
CREATE INDEX IF NOT EXISTS idx_raw_name ON raw_products(name);
