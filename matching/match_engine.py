"""Phase 4 — Matching engine v1: regex hard-filter + rapidfuzz fuzzy.

- Loads raw_products split by source ('gem' vs 'flipkart')
- Enriches GeM names with the product URL slug (contains the full model
  name, e.g. "hp-280-g9-sff-i3-12100-win11p-32113"), which search-card
  titles truncate.
- Normalizes names: lowercase, punctuation space, stopword removal so
  short GeM titles are comparable to verbose Flipkart titles.
- Regex attribute extraction: brand, number+unit, model codes
- Hard filter: skip pairs with no shared attributes
- Fuzzy: rapidfuzz token_sort_ratio on normalized names, threshold 75
- Writes matched_products with match_method='regex_filter+slug+fuzzy'
"""
import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "db" / "gem_project.db"

KNOWN_BRANDS = {
    "hp", "dell", "lenovo", "acer", "asus", "apple", "samsung", "lg",
    "canon", "epson", "brother", "godrej", "nilkamal", "featherlite",
    "zebronics", "logitech", "sony", "philips", "havells", "bajaj",
}

UNIT_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s?(gb|tb|mb|mm|cm|inch|inches|kg|g|w|v|ah|mah|hz|ghz|mp|ltr|litre|liter)\b", re.I)
MODEL_RE = re.compile(r"\b([A-Z]{1,4}[-/]?\d{2,}[A-Z0-9-]*)\b")
BRAND_RE = re.compile(r"^([A-Z][a-zA-Z]+)")

STOPWORDS = {
    "with", "and", "the", "of", "for", "in", "on", "at", "by", "to",
    "a", "an", "it", "is", "are", "was", "be", "or", "not", "from",
    "series", "new", "gen", "model", "brand", "feature", "features",
}


def normalize(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    tokens = [w for w in t.split() if w not in STOPWORDS]
    return " ".join(tokens)


def extract_slug(link: str) -> str:
    """GeM link pattern: .../<slug>/p-<id>-<vid>-cat.html  -> slug words."""
    m = re.search(r"/([^/]+)/p-[\d-]+-cat\.html", link or "")
    if not m:
        return ""
    return re.sub(r"[^a-z0-9 ]+", " ", m.group(1).replace("-", " "))


def extract_attributes(name: str) -> dict:
    low = name.lower()
    brand = None
    for b in KNOWN_BRANDS:
        if b in low:
            brand = b
            break
    if not brand:
        m = BRAND_RE.search(name.strip())
        if m:
            brand = m.group(1).lower()
    units = {u.lower().replace(" ", "") for (_, u) in UNIT_RE.findall(name)}
    models = {m.upper() for m in MODEL_RE.findall(name)}
    return {"brand": brand, "units": units, "models": models}


def passes_filter(gem_attrs: dict, flip_name: str) -> bool:
    """Hard filter: at least one shared signal, else skip fuzzy scoring."""
    flip_low = flip_name.lower()
    # brand must match if we extracted one
    if gem_attrs["brand"] and gem_attrs["brand"] not in flip_low:
        return False
    # if gem has units/models, at least one must appear in flipkart name
    specifics = set()
    specifics.update(gem_attrs["units"])
    specifics.update(m.lower() for m in gem_attrs["models"])
    if specifics and not any(s in flip_low for s in specifics):
        # allow through if brand matched strongly and no specifics overlap?
        # No — per plan this is a hard filter to avoid false matches.
        return False
    return True


# ---------------------------------------------------------------------------
# v3 gates on top of embeddings: model-token overlap + pack-size consistency.
# Kept as separate, inspectable signals (not merged into one score).
# ---------------------------------------------------------------------------
MODEL_TOKEN_STOP = (
    # category / spec words that never carry model identity
    "mouse", "mice", "wired", "wireless", "optical", "laser", "computer",
    "keyboard", "combo", "usb", "ps2", "bluetooth", "rf", "gaming", "ergo",
    "ergonomic", "ambidextrous", "optical", "tracking", "silent", "horizontal",
    "vertical", "desktop", "laptop", "notebook", "pc", "cpu", "monitor",
    "motherboard", "ssd", "hdd", "ram", "processor", "printer", "scanner",
    "toner", "cartridge", "battery", "charger", "adapter", "cable", "webcam",
    "hub", "router", "modem", "switch", "pen", "drive", "storage", "memory",
    "with", "and", "the", "of", "for", "in", "on", "at", "by", "to", "a",
    "an", "it", "is", "are", "was", "be", "or", "not", "from", "this",
    "that", "black", "white", "grey", "gray", "silver", "blue", "red",
    "green", "gold", "rose", "multicolor", "color", "colour", "new", "gen",
    "plus", "pro", "max", "mini", "s", "x", "l", "m", "n", "h", "xl", "xxl",
    "inch", "inches", "mm", "cm", "gb", "tb", "mb", "dpi", "ghz", "mhz",
    "hz", "v", "w", "a", "series", "model", "brand", "standard", "edition",
    "version", "genuine", "original", "oem", "compatible", "india", "indian",
    "made", "make", "product", "products", "goods", "quantity", "price",
    "mrp", "incl", "gst", "tax", "taxes", "shipping", "delivery", "warranty",
    "offer", "offerings", "best", "deal", "deals", "pack", "set", "kit",
    "bundle", "packaging", "box", "pieces", "pcs", "single", "unit", "units",
    "total", "each", "piece", "no", "nos", "of", "qty", "count", "numbers",
    "ps", "rating", "reviews",
)

RATING_NOISE_RE = re.compile(
    r"\b\d(?:\.\d)?\s*\([\d,\s]+\)\b|\(\s*[\d,\s]+\s*\)\s*\d(?:\.\d)?|"
    r"\b\d(?:\.\d)?\s*stars?\b")

# numeric token -> it must contain a digit; model-carrying tokens almost always
# do ("115", "l70", "m290", "lmk105"). Words without digits still count when
# they are distinctive (e.g. "curvy") — handled by the caller via allowlist?
# We keep words like "curvy" OUT of the stoplist so they survive extraction.


MODEL_TOKEN_BRANDS = KNOWN_BRANDS | {
    "fingers", "lapcare", "prodot", "tvs", "intel", "amd", "nvidia", "ob",
}


def extract_model_tokens(name: str) -> set[str]:
    """Pull model-carrying tokens from a normalized product name.

    Drops category/spec/adjective words (MODEL_TOKEN_STOP) AND brand names —
    brand alone must never satisfy Gate 1 (e.g. 'prodot' on both sides of
    two different ProDot mice). Keeps numeric or distinctive tokens like
    '115', 'l70plus', 'curvy', 'm290', 'lmk105'.
    """
    raw = (name or "").lower()
    raw = RATING_NOISE_RE.sub(" ", raw)
    norm = normalize(raw)
    toks = set(norm.split())
    out = set()
    for t in toks:
        if t in MODEL_TOKEN_STOP or t in MODEL_TOKEN_BRANDS:
            continue
        if len(t) < 2:
            continue
        out.add(t)
        # split letter+digit compounds ("l70" -> also "70") so hyphen/slug
        # variants match ("L-70 Plus" normalizes to "l 70" -> token "70")
        m = re.match(r"(.+?)(\d+)$", t)
        if m and len(m.group(1)) >= 1 and len(m.group(2)) >= 1 and \
                m.group(2)[0] != t[0]:
            out.add(m.group(2))
    return out


def pack_signature(name: str) -> str:
    """Pack/quantity signature from the RAW (un-normalized) name.

    '1' -> single unit (no pack wording)
    'n' -> explicit count (set of 4, 4 pact/pack, x2, 2-in-1, pack of 3)
    'bundle' -> bundle/kit/combo wording (needs matching wording to pass)
    """
    low = (name or "").lower()
    if not low:
        return "1"
    multi = re.search(
        r"(?:set|pack|bundle)\s+of\s+(\d+)|"
        r"\b(\d+)\s*[- ]?(?:pack|pcs?|packs|set|bundle|units?|in1|"
        r"in\s*1|way|x)\b|"
        r"\bx(\d+)\b|"
        r"(\d+)\s*[-/]?\s*in[- ]1\b",
        low)
    if multi:
        n = next((g for g in multi.groups() if g), "1")
        try:
            return str(int(n))
        except ValueError:
            return n
    if re.search(r"\b(bundle|kit|combo|multipack|multi[- ]?pack|twin\s*pack|"
                 r"pack\s*of\s*two)\b", low):
        return "bundle"
    return "1"


FORM_DESKTOP = {
    "desktop", "tower", "sff", "usff", "workstation",
    "mini computer", "compact computer",
}
FORM_LAPTOP = {
    "laptop", "notebook", "ultrabook", "netbook", "chromebook", "aspire",
    "x360", "spectre", "thinkpad", "elitebook", "probook", "vivobook",
    "zenbook", "macbook", "macbookair", "macbookpro", "surface", "tablet",
    "2in1", "2-in-1", "convertible",
}
FORM_LAPTOP_LINK = {
    "thin-light-laptop", "laptop/p", "notebook/p", "-laptop-", "ultrabook",
}


def form_factor(name: str, link: str = "") -> str:
    """Classify a product as 'desktop' or 'laptop' ('' if unknown).

    Uses strong, non-generic signals on name text plus the page/slug when the
    name text is truncated (Flipkart titles are cut before the word 'laptop').
    If signals for BOTH form factors are present (e.g. "compatible with
    desktop and laptop"), returns '' so the gate stays silent. Only used to
    REJECT desktop-vs-laptop mismatches — never to accept."""
    low = (name or "").lower() + " " + (link or "").lower()
    is_desk = any(w in low for w in FORM_DESKTOP)
    link_low = (link or "").lower()
    is_lap = any(w in low for w in FORM_LAPTOP) or any(w in link_low for w in FORM_LAPTOP_LINK)
    if is_desk and is_lap:
        return ""
    if is_lap:
        return "laptop"
    if is_desk:
        return "desktop"
    return ""


def gates_pass(gem_text: str, flip_name: str,
               cos: float = None, threshold: float = 0.6,
               gem_link: str = "", flip_link: str = "") -> dict:
    """Apply Gate1 (model-token overlap) + Gate2 (pack consistency) +
    Gate3 (form factor — desktop vs laptop must agree).

    Returns {'pass': bool, 'model': bool, 'pack': bool, 'why': str} so
    rejections are explainable even when cosine clears the threshold."""
    gem_norm = normalize(gem_text or "")
    flip_norm = normalize(flip_name or "")
    gem_toks = extract_model_tokens(gem_text)
    flip_toks = extract_model_tokens(flip_name)

    model_ok = bool(gem_toks & flip_toks)
    gem_pack = pack_signature(gem_text)
    flip_pack = pack_signature(flip_name)
    pack_ok = (gem_pack == flip_pack)

    gf = form_factor(gem_text, gem_link or "")
    ff = form_factor(flip_name, flip_link or "")
    form_ok = (not gf or not ff) or (gf == ff)

    score_ok = cos is None or cos >= threshold

    why = []
    if not score_ok:
        why.append(f"cosine {cos:.3f} < {threshold}")
    if not model_ok:
        why.append(f"no model token overlap ({gem_toks or '-'} vs {flip_toks or '-'})")
    if not pack_ok:
        why.append(f"pack mismatch (gem={gem_pack} vs fk={flip_pack})")
    if not form_ok:
        why.append(f"form factor mismatch (gem={gf} vs fk={ff})")
    return {
        "pass": score_ok and model_ok and pack_ok and form_ok,
        "model": model_ok,
        "pack": pack_ok,
        "form": form_ok,
        "gem_form": gf or "",
        "fk_form": ff or "",
        "score_ok": score_ok,
        "gem_tokens": sorted(gem_toks),
        "fk_tokens": sorted(flip_toks),
        "gem_pack": gem_pack,
        "fk_pack": flip_pack,
        "why": "; ".join(why) or "accept",
    }


# ---------------------------------------------------------------------------
# v2 embedding scoring (sentence-transformers, all-MiniLM-L6-v2)
# Loaded once at module level per spec; cosine in 0-1 scale, NOT 0-100.
# ---------------------------------------------------------------------------
_MODEL = None


def get_model():
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer("all-MiniLM-L6-v2")
    return _MODEL


def embed(texts: list[str]):
    """Encode a batch of texts -> dense vectors (module-level model reused)."""
    model = get_model()
    return model.encode([t or "" for t in texts], normalize_embeddings=True)


def cos_score(texts_a: list[str], texts_b: list[str]) -> list[float]:
    """Cosine similarity (0-1) between normalized embeddings of two batches.

    Returns one similarity per (texts_a[i], texts_b[i]) pair."""
    from sentence_transformers.util import cos_sim
    if not texts_a or not texts_b or len(texts_a) != len(texts_b):
        return []
    va = embed(texts_a)
    vb = embed(texts_b)
    return [float(cos_sim(a, b)[0][0]) for a, b in zip(va, vb)]


def load_products(con):
    cur = con.cursor()
    gem = list(cur.execute(
        "SELECT id, name, link, price FROM raw_products WHERE source='gem'"))
    market = list(cur.execute(
        "SELECT id, name, price FROM raw_products WHERE source='flipkart'"))
    return gem, market


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=75)
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    gem_products, market_products = load_products(con)
    if not gem_products or not market_products:
        print(f"need both sources loaded (gem={len(gem_products)}, flipkart={len(market_products)})")
        print("run scrapers then: python -m db.load_data")
        return 1

    gem_prep = []
    for gid, gname, glink, gprice in gem_products:
        slug = extract_slug(glink or "")
        enriched = f"{gname or ''} {slug}".strip()
        gem_prep.append((gid, gname, gprice, normalize(enriched), enriched))
    market_prep = [(mid, mname, mprice, normalize(mname or "")) for mid, mname, mprice in market_products]

    scores_seen = []
    matches = []
    now = datetime.now(timezone.utc).isoformat()

    for gid, gname, gprice, gen_norm, gen_enriched in gem_prep:
        g_attrs = extract_attributes(gen_enriched)
        for mid, mname, mprice, m_norm in market_prep:
            if not passes_filter(g_attrs, mname or ""):
                continue
            score = fuzz.token_sort_ratio(gen_norm, m_norm)
            scores_seen.append(score)
            if score >= args.threshold:
                matches.append((gid, gname, gprice, mid, mname, mprice, score))

    cur = con.cursor()
    cur.execute("DELETE FROM matched_products WHERE match_method='regex_filter+slug+fuzzy'")
    for gid, _, _, mid, _, _, score in matches:
        cur.execute(
            """INSERT INTO matched_products
               (gem_product_id, market_product_id, similarity_score, match_method, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (gid, mid, float(score), "regex_filter+slug+fuzzy", now),
        )
    con.commit()

    # summary table
    print(f"GeM products: {len(gem_products)} | Flipkart: {len(market_products)} | matches: {len(matches)}")
    if scores_seen:
        import statistics
        print(f"scored pairs: {len(scores_seen)} max={max(scores_seen):.1f} "
              f"median={statistics.median(scores_seen):.1f} mean={statistics.mean(scores_seen):.1f}")
    else:
        print("no pairs passed the hard filter — check brand/unit extraction")
    print("-" * 120)
    print(f"{'GeM name'[:40]:40} | {'GeM Rs':>10} | {'Flipkart name'[:40]:40} | {'FK Rs':>10} | {'Diff%':>7} | {'sim':>5}")
    print("-" * 120)
    for _, gname, gprice, _, mname, mprice, score in sorted(matches, key=lambda r: -r[6])[:50]:
        try:
            diff = (float(mprice) - float(gprice)) if gprice and mprice else None
            pct = (diff / float(gprice) * 100) if diff is not None and float(gprice) else None
        except (TypeError, ValueError):
            diff, pct = None, None
        pct_s = f"{pct:+.1f}%" if pct is not None else "n/a"
        print(f"{(gname or '')[:40]:40} | {str(gprice or 'n/a'):>10} | "
              f"{(mname or '')[:40]:40} | {str(mprice or 'n/a'):>10} | {pct_s:>7} | {score:5.1f}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
