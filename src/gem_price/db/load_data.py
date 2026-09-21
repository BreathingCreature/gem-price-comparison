"""Phase 3 — Load scraper CSVs into SQLite.

- Creates db/gem_project.db from schema if missing
- Reads every *.csv in data/raw/
- Inserts into raw_products, skipping exact duplicates (source+name+link)
- Prints SELECT source, COUNT(*) GROUP BY source
"""
import csv
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from gem_price.core.config import settings
from gem_price.core.logging import setup_logging
from gem_price.db.schema import SCHEMA

logger = setup_logging(__name__)

BASE = Path(__file__).resolve().parent.parent.parent.parent
DB_PATH = BASE / "db" / "gem_project.db"
RAW_DIR = BASE / "data" / "raw"


def init_db() -> sqlite3.Connection:
    """Initialize database with schema and migrations."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    migrate_matched_products(con)
    migrate_raw_products(con)
    return con


def migrate_matched_products(con: sqlite3.Connection) -> None:
    """Add v3+ gate columns to matched_products if the table predates them."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(matched_products)")}
    for col in ("cosine_score", "model_token_match", "pack_match", "form_match", "category"):
        if col not in cols:
            con.execute(f"ALTER TABLE matched_products ADD COLUMN {col}")
    con.commit()


def migrate_raw_products(con: sqlite3.Connection) -> None:
    """Add missing columns to raw_products table if it predates them."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(raw_products)")}
    if "searched_for_gem_link" not in cols:
        con.execute("ALTER TABLE raw_products ADD COLUMN searched_for_gem_link TEXT")
    con.commit()


def parse_price(value: Optional[str]) -> Optional[float]:
    """Parse price string to float, handling various formats."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "")
    # handle ranges like "12000 - 15000" -> take lower bound
    if "-" in s:
        s = s.split("-")[0].strip()
    # strip non-numeric except dot
    cleaned = "".join(ch for ch in s if ch.isdigit() or ch == ".")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def resolve_gem_id(cur: sqlite3.Cursor, gem_link: str) -> Optional[int]:
    """Find current GeM product id from its stable link (ids renumber on DB rebuild)."""
    if not gem_link:
        return None
    gem_link = gem_link.strip()
    cur.execute("SELECT id FROM raw_products WHERE source='gem' AND link=? LIMIT 1", (gem_link,))
    row = cur.fetchone()
    return row[0] if row else None


def load_csvs(con: sqlite3.Connection) -> int:
    """Load all CSV files from raw directory."""
    csvs = sorted(RAW_DIR.glob("*.csv"))
    if not csvs:
        logger.warning(f"No CSVs found in {RAW_DIR}")
        return 0
    # GeM rows MUST exist before resolving per-product flipkart links
    # (searched_for_gem_link -> id). CSV filenames sort alphabetically
    # (flipkart_* before gem_*), so force gems first.
    csvs = sorted(csvs, key=lambda p: (0 if p.name.startswith("gem_") else 1, p.name))
    cur = con.cursor()
    inserted = 0
    for path in csvs:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                source = (row.get("source") or "").strip().lower()
                name = (row.get("name") or "").strip()
                link = (row.get("link") or "").strip()
                if not name or not source:
                    continue
                gem_id = resolve_gem_id(cur, row.get("searched_for_gem_link") or "")
                # skip exact duplicates (same gem lookup context too)
                cur.execute(
                    "SELECT 1 FROM raw_products WHERE source=? AND name=? AND link=? "
                    "AND IFNULL(searched_for_gem_id,0)=? LIMIT 1",
                    (source, name, link, gem_id or 0),
                )
                if cur.fetchone():
                    continue
                cur.execute(
                    """INSERT INTO raw_products
                       (source, name, price, price_type, seller, link, scraped_at, searched_for_gem_id, searched_for_gem_link)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        source,
                        name,
                        parse_price(row.get("price")),
                        (row.get("price_type") or None),
                        (row.get("seller") or None),
                        link or None,
                        (row.get("scraped_at") or None),
                        gem_id,
                        (row.get("searched_for_gem_link") or None),
                    ),
                )
                inserted += 1
        logger.info(f"processed {path.name}")
    con.commit()
    return inserted


def main() -> int:
    con = init_db()
    inserted = load_csvs(con)
    logger.info(f"inserted {inserted} new rows")
    cur = con.cursor()
    for source, count in cur.execute(
        "SELECT source, COUNT(*) FROM raw_products GROUP BY source"
    ):
        logger.info(f"{source}: {count}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())