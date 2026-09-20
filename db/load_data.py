"""Phase 3 — Load scraper CSVs into SQLite.

- Creates db/gem_project.db from schema.sql if missing
- Reads every *.csv in data/raw/
- Inserts into raw_products, skipping exact duplicates (source+name+link)
- Prints SELECT source, COUNT(*) GROUP BY source
"""
import csv
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "db" / "gem_project.db"
SCHEMA_PATH = BASE / "db" / "schema.sql"
RAW_DIR = BASE / "data" / "raw"


def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    con.executescript(schema)
    return con


def parse_price(value):
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


def load_csvs(con: sqlite3.Connection) -> int:
    csvs = sorted(RAW_DIR.glob("*.csv"))
    if not csvs:
        print(f"No CSVs found in {RAW_DIR}")
        return 0
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
                # skip exact duplicates
                cur.execute(
                    "SELECT 1 FROM raw_products WHERE source=? AND name=? AND link=? LIMIT 1",
                    (source, name, link),
                )
                if cur.fetchone():
                    continue
                cur.execute(
                    """INSERT INTO raw_products
                       (source, name, price, price_type, seller, link, scraped_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        source,
                        name,
                        parse_price(row.get("price")),
                        (row.get("price_type") or None),
                        (row.get("seller") or None),
                        link or None,
                        (row.get("scraped_at") or None),
                    ),
                )
                inserted += 1
        print(f"processed {path.name}")
    con.commit()
    return inserted


def main() -> int:
    con = init_db()
    inserted = load_csvs(con)
    print(f"inserted {inserted} new rows")
    cur = con.cursor()
    for source, count in cur.execute(
        "SELECT source, COUNT(*) FROM raw_products GROUP BY source"
    ):
        print(f"{source}: {count}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
