import csv
from pathlib import Path
import random

raw = Path("data/raw")


def show(path, label, n=12):
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    print(f"--- {label} ({len(rows)} rows) ---")
    for r in random.Random(1).sample(rows, min(n, len(rows))):
        slug = r.get("link", "").rsplit("/", 2)[-2] if "/p-" in r.get("link", "") else ""
        name = (r.get("name") or "").replace("\n", " ")
        print(f"  {name[:80]:82} | {r.get('price')}")
    print()


show(raw / "gem_desktop_computer_20260920T123119Z.csv", "GEM desktop computer")
show(raw / "flipkart_desktop_computer_20260920T123356Z.csv", "FLIPKART desktop computer")
show(raw / "gem_computer_mouse_20260920T123221Z.csv", "GEM computer mouse + combo")
show(raw / "flipkart_wireless_mouse_20260920T123516Z.csv", "FLIPKART wireless mouse")
show(raw / "flipkart_keyboard_mouse_combo_20260920T123628Z.csv", "FLIPKART keyboard mouse combo")