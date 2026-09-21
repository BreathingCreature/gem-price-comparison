"""Query generation for per-product Flipkart lookup.

Takes ONE GeM product (from raw_products) and produces clean Flipkart
search queries from its URL slug + name:
- primary: full cleaned slug (brand + model + consumer specs)
- fallback: shorter brand + model pass, tried only if primary returns nothing

Strips GeM-specific technical suffixes (procurement tiers, warranty/compliance
codes, internal SKU fragments) while KEEPING consumer-facing specs (RAM, SSD,
screen size, model codes).

Exported:
    build_queries(name, link) -> list[str]  (primary first, fallback second)
"""
import re
from typing import List

# OS/edition region inside GeM slugs ("win11p", "win-11-pro", "dos") — the
# slug is cut at the first of these; everything after is internal encoding.
OS_MARK = re.compile(r"^(win|windows|dos|win11p|win10p|winl1p)$", re.I)

# tokens that are GeM-internal, not consumer-searchable — dropped from the
# preserved head even before the OS cut.
TECH_SUFFIX = {
    "warranty", "warranties", "general", "information", "procurement",
    "tender", "bid", "biddable", "gst", "madeinindia",
    "pcie", "pcie5", "pclex", "pciex", "afull", "rea", "d3",
    "1year", "1years", "year", "years",
}

# retail-irrelevant tail noise (colors/configuration) dropped from the end
# NOTE: "gb", "tb" REMOVED from here - these are important specs for IT products
TAIL_NOISE = {
    "black", "white", "grey", "gray", "silver", "blue", "red", "green",
    "multicolor", "pearl", "matte", "glossy", "india", "indian", "stack",
    "tray", "rack", "stand", "foam", "fabric",
}


def raw_slug(link: str) -> str:
    """Return the raw slug segment (dashes intact) from a GeM product URL."""
    m = re.search(r"/([^/]+)/p-[\d-]+-cat\.html", link or "")
    return m.group(1) if m else ""


def _cut_os(segments: List[str]) -> List[str]:
    """Cut the token list at the first OS-edition marker."""
    out = []
    for tok in segments:
        if OS_MARK.match(tok):
            break
        out.append(tok)
    return out


def _clean_segments(segments: List[str]) -> List[str]:
    """Drop tech tokens and empty/dup segments; strip trailing noise."""
    kept = []
    for tok in segments:
        t = tok.lower().strip()
        if not t or t in TECH_SUFFIX:
            continue
        kept.append(t)
    while kept and kept[-1] in TAIL_NOISE:
        kept.pop()
    # dedupe consecutive copies (repeat runs from noisy slugs)
    out = []
    for t in kept:
        if not out or out[-1] != t:
            out.append(t)
    return out


def _brand_from(segments: List[str], name: str, known_brands: set = None) -> str:
    """First known-brand token (case-insensitive) in slug or display name.
    
    If known_brands is provided, use that; otherwise fall back to name search.
    """
    if known_brands:
        for tok in segments:
            if tok.lower() in known_brands:
                return tok.lower()
    low = (name or "").lower()
    # Use a reasonable default brand list if none provided
    default_brands = {"hp", "dell", "lenovo", "acer", "asus", "apple", "samsung",
                      "lg", "canon", "epson", "brother", "godrej", "nilkamal",
                      "featherlite", "zebronics", "logitech", "sony", "philips",
                      "havells", "bajaj", "fingers", "lapcare", "prodot", "tvs",
                      "intel", "amd", "nvidia", "ob"}
    for b in sorted(default_brands, key=len, reverse=True):
        if re.search(rf"\b{b}\b", low):
            return b
    return ""


def build_queries(name: str, link: str, known_brands: set = None) -> List[str]:
    """Return [primary_query, fallback_query] for one GeM product."""
    slug = raw_slug(link)
    segs = (slug or "").replace("-", " ").split()
    if not segs:
        return []

    kept = _clean_segments(_cut_os(segs))
    brand = _brand_from(segs, name, known_brands)
    browse = [t for t in kept if t != brand]

    primary = " ".join([brand, *browse] if brand else browse)
    # fallback: brand + first 2 model/spec tokens
    short = browse[:2]
    fallback = " ".join([brand, *short] if brand else short)

    out = []
    for q in (primary, fallback):
        q = re.sub(r"\s+", " ", q).strip()
        if q and q not in out:
            out.append(q)
    return out