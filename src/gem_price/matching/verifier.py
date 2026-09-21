"""Exact match verification for product matching."""
import re
from typing import Dict, List, Any, Optional, Tuple
from gem_price.core.models import ProductIdentity, MarketplaceResult, VerificationResult, MatchStatus, ProductCategory

# Critical specifications that MUST match exactly for each category
CRITICAL_SPECS = {
    "monitor": [
        "model_number", "screen_size", "resolution", "refresh_rate",
        "panel_type", "aspect_ratio"
    ],
    "laptop": [
        "model_number", "cpu", "ram", "storage", "gpu", "screen_size"
    ],
    "desktop": [
        "model_number", "cpu", "ram", "storage", "gpu"
    ],
    "mobile": [
        "model_number", "ram", "storage", "color"
    ],
    "keyboard": [
        "model_number", "switch_type", "layout", "connectivity"
    ],
    "mouse": [
        "model_number", "sensor", "dpi", "connectivity"
    ],
    "headphone": [
        "model_number", "connectivity", "driver_size"
    ],
    "unknown": ["model_number"]
}


def normalize_spec_value(key: str, value: str) -> str:
    """Normalize specification values for comparison."""
    if not value:
        return ""
    
    val = value.strip().lower()
    
    # Normalize units
    val = val.replace(" ", "")
    val = val.replace("inch", "in").replace("inches", "in")
    val = val.replace("hertz", "hz").replace("hertz", "hz")
    val = val.replace("gigahertz", "ghz").replace("megahertz", "mhz")
    val = val.replace("gigabyte", "gb").replace("gbytes", "gb")
    val = val.replace("terabyte", "tb").replace("tbytes", "tb")
    val = val.replace("megabyte", "mb").replace("mbytes", "mb")
    val = val.replace("watts", "w").replace("watt", "w")
    val = val.replace("volts", "v").replace("volt", "v")
    val = val.replace("amperes", "a").replace("ampere", "a").replace("amp", "a")
    
    # Normalize resolution
    val = val.replace("x", "x").replace("×", "x")
    
    # Remove common suffixes
    val = val.replace("max", "").replace("upto", "").replace("up to", "")
    
    return val


def specs_match(spec1: str, spec2: str) -> bool:
    """Check if two spec values match after normalization."""
    if not spec1 or not spec2:
        return False
    return normalize_spec_value("", spec1) == normalize_spec_value("", spec2)


def model_numbers_match(model1: str, model2: str) -> Tuple[bool, float]:
    """
    Compare two model numbers.
    Returns (is_match, confidence).
    """
    if not model1 or not model2:
        return False, 0.0
    
    m1 = model1.upper().strip()
    m2 = model2.upper().strip()
    
    # Exact match
    if m1 == m2:
        return True, 1.0
    
    # Check if one contains the other (partial match)
    if m1 in m2 or m2 in m1:
        longer = max(len(m1), len(m2))
        shorter = min(len(m1), len(m2))
        ratio = shorter / longer
        if ratio >= 0.7:
            return True, 0.8 * ratio
    
    # Check alphanumeric similarity
    alphanum1 = re.sub(r'[^A-Z0-9]', '', m1)
    alphanum2 = re.sub(r'[^A-Z0-9]', '', m2)
    
    if alphanum1 == alphanum2:
        return True, 0.9
    
    # Check if one is substring of other (alphanumeric only)
    if alphanum1 in alphanum2 or alphanum2 in alphanum1:
        longer = max(len(alphanum1), len(alphanum2))
        shorter = min(len(alphanum1), len(alphanum2))
        if longer > 0:
            ratio = shorter / longer
            if ratio >= 0.8:
                return True, 0.7 * ratio
    
    return False, 0.0


def brands_match(brand1: str, brand2: str) -> bool:
    """Check if brands match (case-insensitive)."""
    if not brand1 or not brand2:
        return False
    return brand1.lower().strip() == brand2.lower().strip()


def extract_brand_from_name(name: str) -> str:
    """Extract brand from product name."""
    known_brands = [
        "samsung", "lg", "dell", "hp", "lenovo", "asus", "acer", "apple",
        "sony", "philips", "logitech", "razer", "corsair", "steelseries",
        "hyperx", "redragon", "zebronics", "boat", "noise", "realme",
        "xiaomi", "oneplus", "vivo", "oppo", "motorola", "microsoft",
        "intel", "amd", "nvidia", "wd", "seagate", "sandisk", "kingston",
        "crucial", "gskill", "adata", "transcend", "benq", "viewsonic",
        "aoc", "msi", "gigabyte", "evga", "seasonic", "cooler master",
        "nzxt", "fractal design", "canon", "epson", "brother", "xerox",
        "godrej", "nilkamal", "zebronics", "tv", "boat", "noise"
    ]
    
    name_lower = name.lower()
    for brand in known_brands:
        if brand in name.lower():
            return brand
    return ""


def extract_model_from_name(name: str) -> str:
    """Extract model number from product name."""
    patterns = [
        r"\b([A-Z]{1,4}[-/]?\d{2,}[A-Z0-9-/]*)\b",
        r"\b(LS\d{2}[A-Z]\d{2,}[A-Z0-9]*)\b",
        r"\b([A-Z]{2,4}\d{3,5}[A-Z]?)\b",
        r"\b(i\d-\d{4,5}[A-Z]?)\b",
        r"\b(ryzen\s*\d+\s*\d{3,4}[A-Z]?)\b",
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, name, re.IGNORECASE)
        if matches:
            return max(matches, key=len).upper()
    
    return ""


def extract_specs_from_name(name: str) -> Dict[str, str]:
    """Extract specifications from product name."""
    specs = {}
    name_lower = name.lower()
    
    patterns = {
        "screen_size": r"(\d+(?:\.\d+)?)\s*(?:inch|\"|in)\b",
        "resolution": r"(\d{3,4}\s*[x×]\s*\d{3,4})",
        "refresh_rate": r"(\d{2,3})\s*hz\b",
        "cpu": r"\b(i[3579]\-\d{4,5}[A-Z]?|ryzen\s*\d+\s*\d{3,4}[A-Z]?|amd\s*ryzen\s*\d+)\b",
        "ram": r"(\d+)\s*gb\s*(?:ddr\d|ram|memory)",
        "storage": r"(\d+)\s*(?:gb|tb)\s*(?:ssd|hdd|nvme|emmc)",
        "gpu": r"\b(rtx\s*\d{4}|gtx\s*\d{4}|rx\s*\d{4}|radeon\s*\w+)\b",
        "panel_type": r"\b(ips|va|tn|oled|qled|amoled)\b",
        "aspect_ratio": r"(\d{1,2}:\d{1,2})",
        "curvature": r"(\d{3,4})r\b",
    }
    
    for key, pattern in patterns.items():
        matches = re.findall(pattern, name.lower(), re.IGNORECASE)
        if matches:
            specs[key] = matches[0] if isinstance(matches[0], str) else matches[0][0]
    
    return specs


def verify_exact_match(
    gem_identity: 'ProductIdentity',
    marketplace_result: 'MarketplaceResult'
) -> Tuple[bool, float, List[str], List[str], List[str]]:
    """
    Verify if a marketplace result is an exact match for the GeM product.
    Returns (is_exact_match, confidence, matched_specs, mismatched_specs, missing_specs)
    """
    from gem_price.core.models import ProductIdentity, MarketplaceResult
    
    matched = []
    mismatched = []
    missing = []
    
    # 1. Brand check
    result_brand = extract_brand_from_name(marketplace_result.product_name)
    if brands_match(gem_identity.brand, result_brand):
        matched.append("brand")
    else:
        mismatched.append("brand")
    
    # 2. Model number check
    result_model = extract_model_from_name(marketplace_result.product_name)
    model_match, model_conf = model_numbers_match(
        gem_identity.model_number,
        result_model
    )
    if model_match:
        matched.append("model_number")
    else:
        mismatched.append("model_number")
    
    # 3. Critical specifications check
    category = gem_identity.category.value if hasattr(gem_identity.category, 'value') else str(gem_identity.category)
    critical = CRITICAL_SPECS.get(category.lower(), ["model_number"])
    
    gem_specs = gem_identity.specifications
    result_specs = extract_specs_from_name(marketplace_result.product_name)
    
    for spec in critical:
        gem_val = gem_specs.get(spec, "")
        result_val = result_specs.get(spec, "")
        
        if not gem_val:
            continue
        
        if not result_val:
            missing.append(spec)
        elif specs_match(gem_val, result_val):
            matched.append(spec)
        else:
            mismatched.append(spec)
    
    # Calculate overall confidence
    total_checks = len(matched) + len(mismatched) + len(missing)
    if total_checks == 0:
        confidence = 0.0
    else:
        confidence = len(matched) / total_checks
    
    # Exact match requires: brand match AND model match AND no mismatched critical specs
    is_exact = (
        brands_match(gem_identity.brand, extract_brand_from_name(marketplace_result.product_name)) and
        model_match and
        len(mismatched) == 0
    )
    
    return is_exact, confidence, matched, mismatched, missing


def brands_match(brand1: str, brand2: str) -> bool:
    """Check if brands match (case-insensitive)."""
    if not brand1 or not brand2:
        return False
    return brand1.lower().strip() == brand2.lower().strip()


def model_numbers_match(model1: str, model2: str) -> Tuple[bool, float]:
    """
    Compare two model numbers.
    Returns (is_match, confidence).
    """
    if not model1 or not model2:
        return False, 0.0
    
    m1 = model1.upper().strip()
    m2 = model2.upper().strip()
    
    # Exact match
    if m1 == m2:
        return True, 1.0
    
    # Check if one contains the other (partial match)
    if m1 in m2 or m2 in m1:
        longer = max(len(m1), len(m2))
        shorter = min(len(m1), len(m2))
        ratio = shorter / longer
        if ratio >= 0.7:
            return True, 0.8 * ratio
    
    # Check alphanumeric similarity
    alphanum1 = re.sub(r'[^A-Z0-9]', '', m1)
    alphanum2 = re.sub(r'[^A-Z0-9]', '', m2)
    
    if alphanum1 == alphanum2:
        return True, 0.9
    
    # Check if one is substring of other (alphanumeric only)
    if alphanum1 in alphanum2 or alphanum2 in alphanum1:
        longer = max(len(alphanum1), len(alphanum2))
        shorter = min(len(alphanum1), len(alphanum2))
        if longer > 0:
            ratio = shorter / longer
            if ratio >= 0.8:
                return True, 0.7 * ratio
    
    return False, 0.0


def brands_match(brand1: str, brand2: str) -> bool:
    """Check if brands match (case-insensitive)."""
    if not brand1 or not brand2:
        return False
    return brand1.lower().strip() == brand2.lower().strip()


def extract_brand_from_name(name: str) -> str:
    """Extract brand from product name."""
    known_brands = [
        "samsung", "lg", "dell", "hp", "lenovo", "asus", "acer", "apple",
        "sony", "philips", "logitech", "razer", "corsair", "steelseries",
        "hyperx", "redragon", "zebronics", "boat", "noise", "realme",
        "xiaomi", "oneplus", "vivo", "oppo", "motorola", "microsoft",
        "intel", "amd", "nvidia", "wd", "seagate", "sandisk", "kingston",
        "crucial", "gskill", "adata", "transcend", "benq", "viewsonic",
        "aoc", "msi", "gigabyte", "evga", "seasonic", "cooler master",
        "nzxt", "fractal design", "canon", "epson", "brother", "xerox",
        "godrej", "nilkamal", "zebronics", "tv", "boat", "noise"
    ]
    
    name_lower = name.lower()
    for brand in known_brands:
        if brand in name.lower():
            return brand
    return ""


def extract_model_from_name(name: str) -> str:
    """Extract model number from product name."""
    patterns = [
        r"\b([A-Z]{1,4}[-/]?\d{2,}[A-Z0-9-/]*)\b",
        r"\b(LS\d{2}[A-Z]\d{2,}[A-Z0-9]*)\b",
        r"\b([A-Z]{2,4}\d{3,5}[A-Z]?)\b",
        r"\b(i\d-\d{4,5}[A-Z]?)\b",
        r"\b(ryzen\s*\d+\s*\d{3,4}[A-Z]?)\b",
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, name, re.IGNORECASE)
        if matches:
            return max(matches, key=len).upper()
    
    return ""


def extract_specs_from_name(name: str) -> Dict[str, str]:
    """Extract specifications from product name."""
    specs = {}
    name_lower = name.lower()
    
    patterns = {
        "screen_size": r"(\d+(?:\.\d+)?)\s*(?:inch|\"|in)\b",
        "resolution": r"(\d{3,4}\s*[x×]\s*\d{3,4})",
        "refresh_rate": r"(\d{2,3})\s*hz\b",
        "cpu": r"\b(i[3579]\-\d{4,5}[A-Z]?|ryzen\s*\d+\s*\d{3,4}[A-Z]?|amd\s*ryzen\s*\d+)\b",
        "ram": r"(\d+)\s*gb\s*(?:ddr\d|ram|memory)",
        "storage": r"(\d+)\s*(?:gb|tb)\s*(?:ssd|hdd|nvme|emmc)",
        "gpu": r"\b(rtx\s*\d{4}|gtx\s*\d{4}|rx\s*\d{4}|radeon\s*\w+)\b",
        "panel_type": r"\b(ips|va|tn|oled|qled|amoled)\b",
        "aspect_ratio": r"(\d{1,2}:\d{1,2})",
        "curvature": r"(\d{3,4})r\b",
    }
    
    for key, pattern in patterns.items():
        matches = re.findall(pattern, name.lower(), re.IGNORECASE)
        if matches:
            specs[key] = matches[0] if isinstance(matches[0], str) else matches[0][0]
    
    return specs


def verify_match(gem_identity: ProductIdentity, marketplace_result: MarketplaceResult) -> VerificationResult:
    """Main verification entry point that returns a VerificationResult."""
    is_exact, confidence, matched, mismatched, missing = verify_exact_match(
        gem_identity, marketplace_result
    )
    
    # Model match check
    result_model = extract_model_from_name(marketplace_result.product_name)
    model_match, _ = model_numbers_match(gem_identity.model_number, result_model)
    
    # Brand match
    brand_match = brands_match(gem_identity.brand, extract_brand_from_name(marketplace_result.product_name))
    
    if is_exact:
        status = MatchStatus.EXACT_MATCH
    elif confidence > 0.7:
        status = MatchStatus.NEAR_MATCH
    elif len(mismatched) > 0:
        if any("model" in m for m in mismatched):
            status = MatchStatus.MODEL_MISMATCH
        else:
            status = MatchStatus.SPEC_MISMATCH
    else:
        status = MatchStatus.NOT_FOUND
    
    return VerificationResult(
        is_exact_match=is_exact,
        match_status=status,
        confidence=confidence,
        matched_specs=matched,
        mismatched_specs=mismatched,
        missing_specs=missing,
        model_match=model_match,
        brand_match=brand_match,
        details={
            "gem_model": gem_identity.model_number,
            "result_model": extract_model_from_name(marketplace_result.product_name),
            "gem_brand": gem_identity.brand,
            "result_brand": extract_brand_from_name(marketplace_result.product_name),
        }
    )


# Quick test
if __name__ == "__main__":
    import sys
    sys.path.insert(0, r"C:\Users\Creature\Creature Folder\Synced\Project\code\src")
    
    from gem_price.core.models import ProductIdentity, MarketplaceResult, ProductCategory
    
    # Test Samsung monitor
    gem = ProductIdentity(
        brand="Samsung",
        model_number="LS49C950UAWXXL",
        product_type="Monitor",
        category="monitor",
        specifications={
            "screen_size": "49 inch",
            "resolution": "5120x1440",
            "refresh_rate": "240Hz",
            "panel_type": "QLED",
            "aspect_ratio": "32:9"
        }
    )
    
    # Test exact match
    result = MarketplaceResult(
        marketplace="Amazon.in",
        product_name="Samsung LS49C950UAWXXL 49 inch 5120x1440 240Hz QLED Monitor",
        price=129999,
        url="https://amazon.in/dp/B09V3KJ2LK"
    )
    
    result2 = MarketplaceResult(
        marketplace="Flipkart",
        product_name="Samsung 49 inch Odyssey G9 Neo QLED 240Hz",
        price=134999,
        url="https://flipkart.com/p/itm123"
    )
    
    result3 = MarketplaceResult(
        marketplace="Amazon.in",
        product_name="Samsung 27 inch Monitor Full HD",
        price=15999,
        url="https://amazon.in/dp/B09XYZ"
    )
    
    for i, r in enumerate([result, result2, result3], 1):
        # Simple verification
        brand_match = r.brand.lower() == "samsung"
        model_match = "LS49C950UAWXXL" in r.product_name.upper()
        print(f"Result {i}: {r.product_name[:60]}")
        print(f"  Brand match: {brand_match}, Model match: {model_match}")