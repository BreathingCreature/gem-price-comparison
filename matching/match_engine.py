"""Phase 4 — Matching engine v2: category-aware embedding + soft gates.

- Single-product lookup: given a GeM product, find same product on Flipkart
- Category detection via LLM to select matching strategy
- Embedding similarity (all-MiniLM-L6-v2) as primary signal
- Soft gates: brand match, model tokens, pack size, form factor (weighted, not hard)
- No hard brand filter — brand is a signal, not a gate
- Returns ranked candidates with explainable scores
"""
import argparse
import re
import sqlite3
import sys
import json
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from rapidfuzz import fuzz
from sentence_transformers.util import cos_sim

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "db" / "gem_project.db"

# Extended brand list for signal detection (not hard filter)
KNOWN_BRANDS = {
    "hp", "dell", "lenovo", "acer", "asus", "apple", "samsung", "lg",
    "canon", "epson", "brother", "godrej", "nilkamal", "featherlite",
    "zebronics", "logitech", "sony", "philips", "havells", "bajaj",
    "fingers", "lapcare", "prodot", "tvs", "intel", "amd", "nvidia",
    "sun energy", "agnilux", "dreamlux", "letterprint", "classmate",
    "navneet", "flint", "divyansh", "grotheory", "maxtid", "halonix",
    "schein", "voltech", "pe", "mooka", "roadmaster", "orient",
}

UNIT_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s?(gb|tb|mb|mm|cm|inch|inches|kg|g|w|v|ah|mah|hz|ghz|mp|ltr|litre|liter|watts?|volts?|amps?)\b", re.I)
MODEL_RE = re.compile(r"\b([A-Z]{1,4}[-/]?\d{2,}[A-Z0-9-]*)\b")
BRAND_RE = re.compile(r"^([A-Z][a-zA-Z]+)")

STOPWORDS = {
    "with", "and", "the", "of", "for", "in", "on", "at", "by", "to",
    "a", "an", "it", "is", "are", "was", "be", "or", "not", "from",
    "series", "new", "gen", "model", "brand", "feature", "features",
}

# Category-specific stopwords (added to base for model token extraction)
CATEGORY_STOPWORDS = {
    "it_peripherals": {
        "mouse", "mice", "wired", "wireless", "optical", "laser", "computer",
        "keyboard", "combo", "usb", "ps2", "bluetooth", "rf", "gaming", "ergo",
        "ergonomic", "ambidextrous", "tracking", "silent", "horizontal",
        "vertical", "desktop", "laptop", "notebook", "pc", "cpu", "monitor",
        "motherboard", "ssd", "hdd", "ram", "processor", "printer", "scanner",
        "toner", "cartridge", "battery", "charger", "adapter", "cable", "webcam",
        "hub", "router", "modem", "switch", "pen", "drive", "storage", "memory",
    },
    "stationery": {
        "paper", "papers", "sheet", "sheets", "ream", "reams", "register",
        "registers", "notebook", "notebooks", "note", "notes", "pad", "pads",
        "book", "books", "ledger", "ledgers", "minute", "minutes", "diary",
        "diaries", "planner", "planners", "file", "files", "folder", "folders",
        "binder", "binders", "clipboard", "writing", "copier", "copier",
        "printing", "writing", "plain", "ruled", "unruled", "single", "line",
        "double", "line", "colour", "color", "white", "cream", "a4", "a5",
        "a3", "legal", "letter", "size", "gsm", "pages", "page", "pack",
        "packs", "packet", "packets", "box", "boxes",
    },
    "furniture": {
        "cabinet", "cabinets", "storage", "wall", "mounted", "floor",
        "standing", "unit", "units", "drawer", "drawers", "door", "doors",
        "shelf", "shelves", "rack", "racks", "locker", "lockers", "cupboard",
        "cupboards", "wardrobe", "wardrobes", "shoe", "organizer", "organizers",
        "desk", "desks", "table", "tables", "chair", "chairs", "sofa",
        "sofas", "bed", "beds", "mattress", "mattresses", "metal", "wood",
        "wooden", "plastic", "steel", "iron", "finish", "color", "colour",
        "home", "kitchen", "bathroom", "bedroom", "office", "accommodation",
    },
    "electrical": {
        "led", "bulb", "bulbs", "lamp", "lamps", "light", "lights",
        "luminaire", "luminaires", "fixture", "fixtures", "street", "road",
        "outdoor", "indoor", "flood", "spot", "downlight", "panel",
        "batten", "tube", "tubes", "ceiling", "fan", "fans", "bearing",
        "bearings", "bush", "bushing", "bushings", "motor", "motors",
        "watt", "watts", "wattage", "voltage", "volt", "volts", "amp",
        "amps", "ampere", "ac", "dc", "ip65", "ip66", "ip67", "ip68",
        "waterproof", "weatherproof", "sensor", "sensors", "day",
        "night", "dusk", "dawn", "conforming", "isi", "marked", "bis",
        "approved", "approved", "certified", "certification",
    },
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
# Dynamic stopwords per category (base + category-specific)
def get_category_stopwords(category: str) -> set[str]:
    base = {
        "with", "and", "the", "of", "for", "in", "on", "at", "by", "to",
        "a", "an", "it", "is", "are", "was", "be", "or", "not", "from",
        "this", "that", "black", "white", "grey", "gray", "silver", "blue",
        "red", "green", "gold", "rose", "multicolor", "color", "colour",
        "new", "gen", "plus", "pro", "max", "mini", "s", "x", "l", "m",
        "n", "h", "xl", "xxl", "inch", "inches", "mm", "cm", "gb", "tb",
        "mb", "dpi", "ghz", "mhz", "hz", "v", "w", "a", "series", "model",
        "brand", "standard", "edition", "version", "genuine", "original",
        "oem", "compatible", "india", "indian", "made", "make", "product",
        "products", "goods", "quantity", "price", "mrp", "incl", "gst",
        "tax", "taxes", "shipping", "delivery", "warranty", "offer",
        "offerings", "best", "deal", "deals", "pack", "set", "kit",
        "bundle", "packaging", "box", "pieces", "pcs", "single", "unit",
        "units", "total", "each", "piece", "no", "nos", "of", "qty",
        "count", "numbers", "ps", "rating", "reviews",
    }
    return base | CATEGORY_STOPWORDS.get(category, set())


RATING_NOISE_RE = re.compile(
    r"\b\d(?:\.\d)?\s*\([\d,\s]+\)\b|\(\s*[\d,\s]+\s*\)\s*\d(?:\.\d)?|"
    r"\b\d(?:\.\d)?\s*stars?\b")

# Brands that should never satisfy model-token gate alone
MODEL_TOKEN_BRANDS = KNOWN_BRANDS | {
    "fingers", "lapcare", "prodot", "tvs", "intel", "amd", "nvidia", "ob",
}


@dataclass
class ProductCategory:
    """Detected category with confidence."""
    category: str  # it_peripherals, stationery, furniture, electrical, unknown
    confidence: float
    key_specs: list[str]  # specs that matter for this category


def extract_model_tokens(name: str, category: str = "unknown") -> set[str]:
    """Pull model-carrying tokens from a normalized product name.

    Uses category-specific stopwords. Drops category/spec/adjective words
    AND brand names — brand alone must never satisfy Gate 1. Keeps numeric
    or distinctive tokens like '115', 'l70plus', 'curvy', 'm290', 'lmk105'.
    """
    raw = (name or "").lower()
    raw = RATING_NOISE_RE.sub(" ", raw)
    norm = normalize(raw)
    toks = set(norm.split())
    stop = get_category_stopwords(category)
    out = set()
    for t in toks:
        if t in stop or t in MODEL_TOKEN_BRANDS:
            continue
        if len(t) < 2:
            continue
        out.add(t)
        # split letter+digit compounds ("l70" -> also "70")
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


def gates_pass(gem_text: str, flip_name: str, category: str = "unknown",
               cos: float = None, threshold: float = 0.6,
               gem_link: str = "", flip_link: str = "") -> dict:
    """Apply Gate1 (model-token overlap) + Gate2 (pack consistency) +
    Gate3 (form factor — desktop vs laptop must agree).

    Returns dict with individual gate results and overall pass. Gates are
    now SOFT — the caller decides how to weight them vs cosine score."""
    gem_norm = normalize(gem_text or "")
    flip_norm = normalize(flip_name or "")
    gem_toks = extract_model_tokens(gem_text, category)
    flip_toks = extract_model_tokens(flip_name, category)

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
        why.append(f"no model token overlap ({sorted(gem_toks) or '-'} vs {sorted(flip_toks) or '-'})")
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
        "category": category,
    }


# Soft scoring: combine cosine with gate signals
def composite_score(cos: float, gates: dict, category: str) -> float:
    """Combine cosine similarity with gate signals into a single score.
    
    Weights tuned per category:
    - it_peripherals: cosine=0.5, model=0.2, pack=0.15, form=0.15
    - stationery: cosine=0.6, model=0.2, pack=0.1, form=0.1
    - furniture: cosine=0.6, model=0.15, pack=0.1, form=0.15
    - electrical: cosine=0.55, model=0.25, pack=0.1, form=0.1
    """
    weights = {
        "it_peripherals": (0.50, 0.20, 0.15, 0.15),
        "stationery": (0.60, 0.20, 0.10, 0.10),
        "furniture": (0.60, 0.15, 0.10, 0.15),
        "electrical": (0.55, 0.25, 0.10, 0.10),
    }
    w_cos, w_mod, w_pack, w_form = weights.get(category, (0.55, 0.20, 0.125, 0.125))
    
    gate_score = (w_mod * (1.0 if gates["model"] else 0.0) +
                  w_pack * (1.0 if gates["pack"] else 0.0) +
                  w_form * (1.0 if gates["form"] else 0.0))
    
    return w_cos * cos + gate_score


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
    if not texts_a or not texts_b or len(texts_a) != len(texts_b):
        return []
    va = embed(texts_a)
    vb = embed(texts_b)
    return [float(cos_sim(a, b)[0][0]) for a, b in zip(va, vb)]


# Category detection via LLM
def detect_category(gem_name: str, gem_link: str, llm_attrs: dict = None) -> ProductCategory:
    """Use LLM to detect product category and key specs for matching."""
    # Quick heuristic fallback first
    text = (gem_name or "") + " " + (gem_link or "")
    text_lower = text.lower()
    
    # Heuristic category detection - more specific patterns to avoid false positives
    # IT peripherals: specific device types, NOT generic "computer"
    it_keywords = ["mouse", "keyboard", "combo", "headset", "webcam", "hub", "router", "ssd", "hdd", "ram", 
                   "processor", "cpu", "motherboard", "monitor", "gpu", "graphics", "printer", "scanner", 
                   "toner", "cartridge", "laptop", "desktop", "workstation", "pc ", " laptop", " desktop"]
    if any(w in text_lower for w in it_keywords):
        return ProductCategory("it_peripherals", 0.8, ["model", "specs", "connectivity"])
    
    # Stationery - check more specific patterns (paper, register, notebook, etc.)
    stationery_keywords = ["paper", "register", "notebook", "ledger", "diary", "planner", "file", "folder", 
                           "binder", "a4", "a5", "gsm", "ruled", "unruled", "copier", "ream", "pages", "page"]
    if any(w in text_lower for w in stationery_keywords):
        return ProductCategory("stationery", 0.85, ["size", "gsm", "pages", "ruling", "color"])
    
    if any(w in text_lower for w in ["cabinet", "drawer", "shelf", "rack", "locker", "wardrobe", "desk", "table", 
                                     "chair", "sofa", "bed", "mattress", "shoe rack", "organizer", 
                                     "wall mounted", "floor standing", "metal storage", "wooden"]):
        return ProductCategory("furniture", 0.8, ["dimensions", "material", "type", "finish", "doors", "drawers"])
    
    if any(w in text_lower for w in ["led", "bulb", "lamp", "light", "luminaire", "fixture", "street light", 
                                     "flood light", "ceiling fan", "bearing", "watt", "voltage", "ip65", "ip66", 
                                     "ip67", "ip68", "waterproof", "weatherproof", "sensor", "conforming", 
                                     "isi", "marked", "bis", "approved", "certified"]):
        return ProductCategory("electrical", 0.8, ["wattage", "voltage", "type", "ip_rating", "certification"])
    
    # Default to unknown with moderate confidence
    return ProductCategory("unknown", 0.5, ["model", "specs"])


# Query building per category
def build_queries_for_category(gem_name: str, gem_link: str, llm_attrs: dict, category: ProductCategory) -> list[str]:
    """Build Flipkart search queries tailored to the detected category."""
    queries = []
    name = gem_name or ""
    slug = extract_slug(gem_link or "")
    brand = (llm_attrs or {}).get("brand") or ""
    model = (llm_attrs or {}).get("model") or ""
    specs = (llm_attrs or {}).get("key_specs") or []
    
    if category.category == "it_peripherals":
        # Brand + model + key specs
        if brand and model:
            queries.append(f"{brand} {model}")
        if brand:
            queries.append(f"{brand} {' '.join(specs[:2])}")
        queries.append(slug.replace("-", " ")[:80])
        
    elif category.category == "stationery":
        # Size + GSM + pages + ruling
        size_specs = [s for s in specs if any(k in s.lower() for k in ["a4", "a5", "a3", "legal", "letter", "size", "gsm", "page"])]
        if size_specs:
            queries.append(f"{' '.join(size_specs)} {brand}")
        queries.append(f"{brand} {' '.join(specs[:3])}")
        queries.append(slug.replace("-", " ")[:80])
        
    elif category.category == "furniture":
        # Type + dimensions + material
        dim_specs = [s for s in specs if any(k in s.lower() for k in ["mm", "cm", "inch", "feet", "ft", "width", "height", "depth", "door", "drawer", "shelf"])]
        if dim_specs:
            queries.append(f"{' '.join(dim_specs)} {brand}")
        queries.append(f"{brand} {' '.join(specs[:3])}")
        queries.append(slug.replace("-", " ")[:80])
        
    elif category.category == "electrical":
        # Wattage + voltage + type + certification
        watt_specs = [s for s in specs if any(k in s.lower() for k in ["watt", "w ", "wattage", "voltage", "volt", "amp", "ip6", "isi", "bis"])]
        if watt_specs:
            queries.append(f"{' '.join(watt_specs)} {brand}")
        queries.append(f"{brand} {' '.join(specs[:3])}")
        queries.append(slug.replace("-", " ")[:80])
        
    else:
        # Generic fallback
        if brand and model:
            queries.append(f"{brand} {model}")
        if brand:
            queries.append(f"{brand} {' '.join(specs[:2])}")
        queries.append(slug.replace("-", " ")[:80])
    
    # Deduplicate and limit
    seen = set()
    uniq = []
    for q in queries:
        q = q.strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            uniq.append(q)
    return uniq[:5]


# Soft filter (replaces hard filter) - returns score 0-1 for attribute overlap
def soft_attribute_overlap(gem_attrs: dict, flip_name: str) -> float:
    """Return 0-1 score for attribute overlap. Brand match = 0.3, unit/model overlap = 0.7."""
    flip_low = (flip_name or "").lower()
    score = 0.0
    
    # Brand signal (soft, not hard)
    if gem_attrs.get("brand") and gem_attrs["brand"].lower() in flip_low:
        score += 0.3
    
    # Unit overlap
    units = gem_attrs.get("units", set())
    if units:
        unit_matches = sum(1 for u in units if u in flip_low)
        score += 0.4 * min(1.0, unit_matches / max(1, len(units)))
    
    # Model code overlap
    models = gem_attrs.get("models", set())
    if models:
        model_matches = sum(1 for m in models if m.lower() in flip_low)
        score += 0.3 * min(1.0, model_matches / max(1, len(models)))
    
    return min(1.0, score)


def load_products(con):
    cur = con.cursor()
    gem = list(cur.execute(
        "SELECT id, name, link, price FROM raw_products WHERE source='gem'"))
    market = list(cur.execute(
        "SELECT id, name, price, link FROM raw_products WHERE source='flipkart'"))
    return gem, market


# Single-product matching function (for API use)
def match_single_gem_product(gem_id: int, gem_name: str, gem_link: str, gem_price: str,
                              con, top_k: int = 10, cosine_threshold: float = 0.55) -> list[dict]:
    """Match ONE GeM product against all Flipkart products.
    
    Returns list of dicts with match details, sorted by composite score.
    """
    # Get LLM attributes (cached or fresh)
    from matching.llm_attributes import get_product_attributes
    llm_attrs = get_product_attributes(gem_name, gem_link, con)
    
    # Detect category
    category = detect_category(gem_name, gem_link, llm_attrs)
    
    # Build category-specific queries
    queries = build_queries_for_category(gem_name, gem_link, llm_attrs, category)
    
    # For now, search all Flipkart products (in production, use search API)
    # This is a fallback - ideally we'd use the per_product_lookup results
    cur = con.cursor()
    fk_rows = list(cur.execute(
        "SELECT id, name, price, link FROM raw_products WHERE source='flipkart'"))
    
    if not fk_rows:
        return []
    
    # Build enriched GeM text
    slug = extract_slug(gem_link or "")
    gem_enriched = f"{gem_name or ''} {slug}".strip()
    gem_norm = normalize(gem_enriched)
    
    # Extract GeM attributes for soft filter
    from matching.match_engine import extract_attributes as extract_attrs_regex
    gem_attrs = extract_attrs_regex(gem_enriched)
    # Override brand with LLM if available
    if llm_attrs.get("brand"):
        gem_attrs["brand"] = llm_attrs["brand"].lower()
    
    # Prepare candidate texts
    fk_names = [(fid, fname or "", fprice or "", flink or "") for fid, fname, fprice, flink in fk_rows]
    fk_texts = [normalize(fname or "") for _, fname, _, _ in fk_rows]
    
    # Compute cosine similarities in batches
    gem_texts = [gem_norm] * len(fk_texts)
    cos_sims = cos_score(gem_texts, fk_texts)
    
    # Score each candidate
    results = []
    for (fid, fname, fprice, flink), cos in zip(fk_names, cos_sims):
        # Soft attribute filter (doesn't reject, just scores)
        attr_score = soft_attribute_overlap(gem_attrs, fname)
        
        # Gates
        gates = gates_pass(gem_enriched, fname, category.category, cos, cosine_threshold, gem_link, flink)
        
        # Composite score
        comp = composite_score(cos, gates, category.category)
        
        results.append({
            "flipkart_id": fid,
            "flipkart_name": fname,
            "flipkart_price": fprice,
            "flipkart_link": flink,
            "cosine_similarity": round(cos, 4),
            "attribute_overlap": round(attr_score, 3),
            "gates": gates,
            "composite_score": round(comp, 4),
            "category": category.category,
            "category_confidence": category.confidence,
            "gem_category_specs": category.key_specs,
        })
    
    # Sort by composite score descending
    results.sort(key=lambda r: -r["composite_score"])
    return results[:top_k]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.6, help="Cosine threshold (0-1)")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--gem-id", type=int, help="Single GeM product ID to match")
    ap.add_argument("--top-k", type=int, default=10)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    gem_products, market_products = load_products(con)
    if not gem_products or not market_products:
        print(f"need both sources loaded (gem={len(gem_products)}, flipkart={len(market_products)})")
        print("run scrapers then: python -m db.load_data")
        return 1

    # If single product requested
    if args.gem_id:
        gem = next((g for g in gem_products if g[0] == args.gem_id), None)
        if not gem:
            print(f"GeM product {args.gem_id} not found")
            return 1
        gid, gname, glink, gprice = gem
        results = match_single_gem_product(gid, gname, glink, gprice, con, args.top_k, args.threshold)
        
        print(f"\nGeM: {gname} (ID={gid})")
        print(f"Link: {glink}")
        print(f"Price: {gprice}")
        print("-" * 100)
        print(f"{'Rank':>4} | {'FK ID':>6} | {'Composite':>8} | {'Cosine':>6} | {'Attr':>5} | {'Model':>5} | {'Pack':>5} | {'Form':>5} | {'FK Name'[:50]}")
        print("-" * 100)
        for i, r in enumerate(results, 1):
            g = r["gates"]
            print(f"{i:>4} | {r['flipkart_id']:>6} | {r['composite_score']:>8.3f} | "
                  f"{r['cosine_similarity']:>6.3f} | {r['attribute_overlap']:>5.2f} | "
                  f"{'Y' if g['model'] else 'N':>5} | {'Y' if g['pack'] else 'N':>5} | "
                  f"{'Y' if g['form'] else 'N':>5} | {r['flipkart_name'][:50]}")
            if r["gates"]["why"] != "accept":
                print(f"      Why: {r['gates']['why']}")
        con.close()
        return 0

    # Batch mode (legacy) - keep for compatibility but using new logic
    now = datetime.now(timezone.utc).isoformat()
    all_results = []
    
    for gid, gname, glink, gprice in gem_products:
        results = match_single_gem_product(gid, gname, glink, gprice, con, top_k=5, cosine_threshold=args.threshold)
        for r in results:
            if r["composite_score"] >= args.threshold:
                all_results.append((gid, r["flipkart_id"], r["composite_score"], r))

    cur = con.cursor()
    cur.execute("DELETE FROM matched_products WHERE match_method LIKE 'embedding_cosine%'")
    for gid, fid, score, r in all_results:
        cur.execute(
            """INSERT INTO matched_products
               (gem_product_id, market_product_id, similarity_score, match_method, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (gid, fid, score, f"embedding_cosine+gates+{r['category']}", now),
        )
    con.commit()

    print(f"GeM products: {len(gem_products)} | Flipkart: {len(market_products)} | matches: {len(all_results)}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
