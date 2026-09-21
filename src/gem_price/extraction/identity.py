"""Product identity extraction from GeM pages."""
import re
import json
import os
import time
from typing import Dict, Any, Optional
from datetime import datetime

from bs4 import BeautifulSoup

from gem_price.core.config import settings
from gem_price.core.logging import setup_logging
from gem_price.core.models import ProductIdentity, ProductCategory

logger = setup_logging(__name__)


# Known brands for fallback extraction
KNOWN_BRANDS = {
    "samsung", "lg", "dell", "hp", "lenovo", "asus", "acer", "apple",
    "sony", "philips", "logitech", "razer", "corsair", "steelseries",
    "hyperx", "redragon", "zebronics", "tv", "boat", "noise",
    "realme", "xiaomi", "oneplus", "vivo", "oppo", "motorola",
    "microsoft", "intel", "amd", "nvidia", "amd", "wd", "seagate",
    "sandisk", "kingston", "crucial", "gskill", "adata", "transcend",
    "benq", "viewsonic", "aoc", "msi", "gigabyte", "evga",
    "seasonic", "cooler master", "nzxt", "fractal design",
    "canon", "epson", "brother", "xerox", "ricoh", "kyocera",
    "godrej", "nilkamal", "featherlite", "durian", "urban ladder",
    "pepperfry", "homecentre", "fabindia", "ikea"
}


# Category-specific critical specs
CRITICAL_SPECS_BY_CATEGORY = {
    ProductCategory.MONITOR: [
        "screen_size", "resolution", "refresh_rate", "panel_type",
        "aspect_ratio", "curvature", "response_time"
    ],
    ProductCategory.LAPTOP: [
        "cpu", "ram", "storage", "gpu", "screen_size", "resolution"
    ],
    ProductCategory.DESKTOP: [
        "cpu", "ram", "storage", "gpu", "motherboard"
    ],
    ProductCategory.LAPTOP: [
        "cpu", "ram", "storage", "gpu", "screen_size", "resolution"
    ],
    ProductCategory.MOBILE: [
        "model", "ram", "storage", "color"
    ],
    ProductCategory.KEYBOARD: [
        "switch_type", "layout", "connectivity", "size"
    ],
    ProductCategory.MOUSE: [
        "sensor", "dpi", "connectivity", "buttons"
    ],
}


def categorize_product(name: str, specs: Dict[str, Any]) -> ProductCategory:
    """Categorize product based on name and specs."""
    name_lower = name.lower()
    specs_text = " ".join(str(v).lower() for v in specs.values())
    text = f"{name_lower} {specs_text}"
    
    if any(k in text for k in ["monitor", "display", "screen"]):
        return ProductCategory.MONITOR
    if any(k in text for k in ["laptop", "notebook", "ultrabook"]):
        return ProductCategory.LAPTOP
    if any(k in text for k in ["desktop", "tower", "workstation", "pc "]):
        return ProductCategory.DESKTOP
    if any(k in text for k in ["mobile", "smartphone", "phone"]):
        return ProductCategory.MOBILE
    if any(k in text for k in ["tablet", "ipad"]):
        return ProductCategory.TABLET
    if any(k in text for k in ["keyboard", "keypad"]):
        return ProductCategory.KEYBOARD
    if any(k in text for k in ["mouse", "trackball"]):
        return ProductCategory.MOUSE
    if any(k in text for k in ["headphone", "headset", "earphone", "earbud"]):
        return ProductCategory.HEADPHONE
    if any(k in text for k in ["printer", "scanner"]):
        return ProductCategory.PRINTER
    if any(k in text for k in ["ssd", "hdd", "hard drive", "storage"]):
        return ProductCategory.STORAGE
    if any(k in text for k in ["graphics card", "gpu", "rtx", "gtx"]):
        return ProductCategory.GRAPHICS_CARD
    if any(k in text for k in ["processor", "cpu", "ryzen", "core i"]):
        return ProductCategory.PROCESSOR
    if any(k in text for k in ["motherboard", "mobo"]):
        return ProductCategory.MOTHERBOARD
    if any(k in text for k in ["ram", "memory", "ddr"]):
        return ProductCategory.RAM
    if any(k in text for k in ["power supply", "psu", "smp"]):
        return ProductCategory.POWER_SUPPLY
    
    return ProductCategory.UNKNOWN


def extract_specifications(text: str) -> Dict[str, Any]:
    """Extract specifications from text using regex patterns."""
    specs = {}
    
    # Common patterns
    patterns = {
        "screen_size": r"(\d+(?:\.\d+)?)\s*(?:inch|\"|in)\b",
        "resolution": r"(\d{3,4}\s*[x×]\s*\d{3,4})",
        "refresh_rate": r"(\d{2,3})\s*hz\b",
        "cpu": r"\b(i[3579]\-\d{4,5}[A-Z]?|ryzen\s*\d+\s*\d{3,4}[A-Z]?|amd\s*ryzen\s*\d+|intel\s*core\s*i[3579])\b",
        "ram": r"(\d+)\s*gb\s*(?:ddr\d|ram|memory)",
        "storage": r"(\d+)\s*(?:gb|tb)\s*(?:ssd|hdd|hdd|nvme|emmc)",
        "gpu": r"\b(rtx\s*\d{4}|gtx\s*\d{4}|rx\s*\d{4}|radeon\s*\w+|nvidia\s*\w+)\b",
        "panel_type": r"\b(ips|va|tn|oled|qled|amoled|pva|mva)\b",
        "aspect_ratio": r"(\d{1,2}:\d{1,2})",
        "curvature": r"(\d{3,4})r\b",
        "response_time": r"(\d+(?:\.\d+)?)\s*ms\b",
        "connectivity": r"\b(hdmi\s*\d\.?\d*|displayport\s*\d\.?\d*|usb\s*[-\s]?c|thunderbolt|vga|dvi)\b",
    }
    
    text_lower = " ".join(text.lower().split())
    
    for key, pattern in patterns.items():
        matches = re.findall(pattern, text_lower, re.IGNORECASE)
        if matches:
            # Take the first match, clean it
            val = matches[0] if isinstance(matches[0], str) else matches[0][0]
            specs[key] = val.strip()
    
    return specs


def extract_model_number(text: str) -> Optional[str]:
    """Extract model number from text."""
    # Model number patterns: alphanumeric with dashes, slashes
    patterns = [
        r"\b([A-Z]{1,4}[-/]?\d{2,}[A-Z0-9-/]*)\b",
        r"\b(LS\d{2}[A-Z]\d{2,}[A-Z0-9]*)\b",  # Samsung monitors
        r"\b([A-Z]{2,4}\d{3,5}[A-Z]?)\b",
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            # Return the longest match (most specific)
            return max(matches, key=len).upper()
    return None


def extract_brand(text: str, known_brands: set = None) -> Optional[str]:
    """Extract brand from text."""
    brands = known_brands or KNOWN_BRANDS
    text_lower = text.lower()
    
    # Check for known brands
    for brand in sorted(known_brands, key=len, reverse=True):
        if brand in text_lower:
            return brand.capitalize()
    
    # Try to extract from start of text (first capitalized word)
    words = text.split()
    for word in words[:5]:
        if word[0].isupper() and len(word) > 1:
            return word
    
    return None


def parse_specs_table(html: str) -> Dict[str, Any]:
    """Parse specification table from HTML."""
    from bs4 import BeautifulSoup
    specs = {}
    
    soup = BeautifulSoup(html, "html.parser")
    
    # Find tables with specifications
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) >= 2:
                key = cells[0].get_text(strip=True).lower()
                value = cells[1].get_text(strip=True)
                if key and value:
                    specs[key] = value
    
    # Also check definition lists
    for dl in soup.find_all("dl"):
        dt = dl.find("dt")
        dd = dl.find("dd")
        if dt and dd:
            key = dt.get_text(strip=True).lower()
            value = dd.get_text(strip=True)
            if key and value:
                specs[key] = value
    
    return specs


def extract_identity(html: str, url: str):
    """Main entry point to extract product identity from GeM HTML."""
    from gem_price.core.models import ProductIdentity, ProductCategory
    
    soup = BeautifulSoup(html, "html.parser")
    
    # Extract product name
    name = ""
    for selector in [
        ".variant-title",
        ".variant-desc .variant-title",
        ".product-title",
        "h1.product-title",
        "h1",
        ".product-name",
        "[class*='product'][class*='title']"
    ]:
        elem = soup.select_one(selector)
        if elem:
            name = elem.get_text(strip=True)
            if name and len(name) > 5:
                break
    
    # Extract breadcrumb/category
    breadcrumb = ""
    for selector in [".breadcrumb", ".breadcrumbs", ".category-path", ".nav-breadcrumb"]:
        elem = soup.select_one(selector)
        if elem:
            breadcrumb = elem.get_text(" > ", strip=True)
            break
    
    # Extract price
    price = ""
    for selector in [".variant-final-price", ".price", ".product-price", ".final-price"]:
        elem = soup.select_one(selector)
        if elem:
            price = elem.get_text(strip=True)
            break
    
    # Extract specifications
    specs_text = ""
    specs_dict = {}
    
    # Try to find specification table
    for selector in [".specifications", ".spec-table", ".product-specs", ".variant-specs", "table.specs"]:
        elem = soup.select_one(selector)
        if elem:
            specs_dict.update(parse_specs_table(str(elem)))
            specs_text = elem.get_text(" ", strip=True)
            break
    
    # Also get all text for LLM extraction
    all_text = soup.get_text(" ", strip=True)
    
    # Extract model number from URL
    model_from_url = None
    match = re.search(r"/([^/]+)/p-\d+-\d+-cat\.html", url)
    if match:
        model_from_url = match.group(1).replace("-", " ").upper()
    
    # Combine all text for extraction
    full_text = f"{name} {specs_text} {all_text}"
    
    # Extract components
    brand = extract_brand(full_text)
    model = extract_model_number(full_text)
    if model_from_url and (not model or len(model_from_url) > len(model)):
        model = model_from_url
    
    specs = extract_specifications(full_text)
    specs.update(specs_dict)
    
    # Categorize
    category = categorize_product(name, specs)
    
    # Determine critical specs
    critical_specs = CRITICAL_SPECS_BY_CATEGORY.get(category, [])
    
    # Build identity
    identity = ProductIdentity(
        brand=brand or "",
        model_number=model or "",
        product_type=category.value,
        category=category,
        specifications=specs,
        raw_name=name,
        raw_specs=specs_text,
        source_url="",  # Will be set by caller
        confidence=0.7 if (brand and model) else 0.4,
        extraction_method="fallback"
    )
    
    return identity


def extract_identity_from_gem(url: str) -> dict:
    """Extract identity from GeM URL - main entry point for external use."""
    import requests
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return extract_identity(resp.text, url)


if __name__ == "__main__":
    # Test with the Samsung monitor URL
    import sys
    sys.path.insert(0, r"C:\Users\Creature\Creature Folder\Synced\Project\code\src")
    
    url = "https://mkp.gem.gov.in/computer-monitor-v2/samsung-ultrawide-monitor/p-5116877-19154912158-cat.html"
    import requests
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    resp = requests.get(url, headers=headers, timeout=15)
    identity = extract_identity(resp.text, url)
    print(f"Brand: {identity.brand}")
    print(f"Model: {identity.model_number}")
    print(f"Category: {identity.category}")
    print(f"Specs: {identity.specifications}")
    print(f"Confidence: {identity.confidence}")