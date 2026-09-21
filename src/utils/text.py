"""Text processing utilities."""
import re
from typing import Optional


# Base stopwords (always removed)
BASE_STOPWORDS = {
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

# Category-specific stopwords for model token extraction
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
        "binder", "binders", "clipboard", "writing", "copier", "printing",
        "plain", "ruled", "unruled", "single", "line", "double", "colour", "color",
        "white", "cream", "a4", "a5", "a3", "legal", "letter", "size", "gsm",
        "pages", "page", "pack", "packs", "packet", "packets", "box", "boxes",
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
        "approved", "certified", "certification",
    },
}


def get_category_stopwords(category: str) -> set[str]:
    """Get combined stopwords for a category."""
    return BASE_STOPWORDS | CATEGORY_STOPWORDS.get(category, set())


def normalize(text: Optional[str]) -> str:
    """Normalize text for comparison: lowercase, remove punctuation, remove stopwords."""
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    # Use base stopwords only for general normalization
    tokens = [w for w in t.split() if w not in BASE_STOPWORDS]
    return " ".join(tokens)


def extract_slug(link: Optional[str]) -> str:
    """Extract slug words from GeM product URL.
    
    Pattern: .../<slug>/p-<id>-<vid>-cat.html -> slug words
    """
    if not link:
        return ""
    m = re.search(r"/([^/]+)/p-[\d-]+-cat\.html", link)
    if not m:
        return ""
    return re.sub(r"[^a-z0-9 ]+", " ", m.group(1).replace("-", " "))


def extract_attributes(text: str, known_brands: set[str]) -> dict:
    """Extract brand, units, and model codes from product name."""
    low = text.lower()
    brand = None
    for b in known_brands:
        if b in low:
            brand = b
            break
    if not brand:
        m = re.search(r"^([A-Z][a-zA-Z]+)", text.strip())
        if m:
            brand = m.group(1).lower()
    
    # Units: number + unit (GB, TB, MB, mm, cm, inch, kg, g, W, V, etc.)
    unit_re = re.compile(
        r"\b(\d+(?:\.\d+)?)\s?(gb|tb|mb|mm|cm|inch|inches|kg|g|w|v|ah|mah|hz|ghz|mp|ltr|litre|liter|watts?|volts?|amps?)\b", 
        re.I
    )
    units = {u.lower().replace(" ", "") for (_, u) in unit_re.findall(text)}
    
    # Model codes: alphanumeric patterns like "280-G9", "L70", "M290"
    model_re = re.compile(r"\b([A-Z]{1,4}[-/]?\d{2,}[A-Z0-9-]*)\b")
    models = {m.upper() for m in model_re.findall(text)}
    
    return {"brand": brand, "units": units, "models": models}


# Known brands for signal detection (NOT hard filter)
KNOWN_BRANDS = {
    "hp", "dell", "lenovo", "acer", "asus", "apple", "samsung", "lg",
    "canon", "epson", "brother", "godrej", "nilkamal", "featherlite",
    "zebronics", "logitech", "sony", "philips", "havells", "bajaj",
    "fingers", "lapcare", "prodot", "tvs", "intel", "amd", "nvidia",
    "sun energy", "agnilux", "dreamlux", "letterprint", "classmate",
    "navneet", "flint", "divyansh", "grotheory", "maxtid", "halonix",
    "schein", "voltech", "pe", "mooka", "roadmaster", "orient",
}