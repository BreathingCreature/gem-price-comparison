"""Fallback extraction combining LLM and regex-based methods."""
import re
import json
from typing import Dict, Any, Optional
from datetime import datetime

from gem_price.core.models import ProductIdentity, ProductCategory
from gem_price.extraction.identity import (
    extract_identity, extract_specifications, extract_model_number,
    extract_brand, categorize_product, CRITICAL_SPECS_BY_CATEGORY
)
from gem_price.extraction.llm_extractor import extract_with_llm
from gem_price.core.logging import setup_logging

logger = setup_logging(__name__)


def extract_identity_hybrid(html: str, url: str) -> ProductIdentity:
    """
    Hybrid extraction: Try LLM first, fallback to regex-based extraction.
    Returns the best result based on confidence.
    """
    # Try LLM first
    llm_result = extract_with_llm(html, url)
    
    if llm_result and llm_result.get("confidence", 0) >= 0.7:
        logger.info(f"LLM extraction successful (confidence: {llm_result.get('confidence', 0)})")
        return _convert_llm_to_identity(llm_result, url)
    
    # Fallback to regex-based extraction
    logger.info("Using fallback regex-based extraction")
    fallback_identity = extract_identity(html, url)
    fallback_identity.extraction_method = "fallback"
    fallback_identity.source_url = url
    return fallback_identity


def _convert_llm_to_identity(llm_result: dict, url: str) -> ProductIdentity:
    """Convert LLM extraction result to ProductIdentity."""
    from gem_price.core.models import ProductIdentity, ProductCategory
    
    # Parse category
    try:
        category = ProductCategory(llm_result.get("category", "unknown"))
    except ValueError:
        category = ProductCategory.UNKNOWN
    
    # Clean specifications
    specs = llm_result.get("specifications", {})
    clean_specs = {}
    for k, v in specs.items():
        if v and str(v).strip() and str(v).lower() not in ["unknown", "n/a", "null", "none"]:
            clean_specs[k] = str(v).strip()
    
    identity = ProductIdentity(
        brand=llm_result.get("brand", "") or "",
        model_number=llm_result.get("model_number", "") or "",
        product_type=llm_result.get("product_type", "") or "",
        category=category,
        specifications=clean_specs,
        raw_name="",  # Not provided by LLM directly
        raw_specs="",
        source_url="",  # Set by caller
        confidence=llm_result.get("confidence", 0.8),
        extraction_method="llm"
    )
    
    return identity


def merge_extractions(llm_result: dict, fallback_identity: ProductIdentity) -> ProductIdentity:
    """Merge LLM and fallback results, preferring LLM for high-confidence fields."""
    merged = fallback_identity.model_copy()
    
    # Use LLM values where they have higher confidence
    if llm_result.get("brand") and len(llm_result["brand"]) > len(merged.brand or ""):
        merged.brand = llm_result["brand"]
    
    if llm_result.get("model_number") and len(llm_result["model_number"]) > len(merged.model_number or ""):
        merged.model_number = llm_result["model_number"]
    
    # Merge specifications (LLM takes precedence)
    llm_specs = llm_result.get("specifications", {})
    if llm_specs:
        merged.specifications.update(llm_specs)
    
    # Update confidence
    llm_conf = llm_result.get("confidence", 0)
    if llm_conf > merged.confidence:
        merged.confidence = (merged.confidence + llm_conf) / 2
    
    merged.extraction_method = "hybrid"
    return merged


def extract_identity_hybrid_from_url(url: str) -> ProductIdentity:
    """Extract identity from a GeM URL using hybrid approach."""
    import requests
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    
    return extract_identity_hybrid(resp.text, url)


# Quick test
if __name__ == "__main__":
    import sys
    sys.path.insert(0, r"C:\Users\Creature\Creature Folder\Synced\Project\code\src")
    
    import requests
    url = "https://mkp.gem.gov.in/computer-monitor-v2/samsung-ultrawide-monitor/p-5116877-19154912158-cat.html"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    resp = requests.get(url, headers=headers, timeout=15)
    identity = extract_identity_hybrid(resp.text, url)
    print(f"Brand: {identity.brand}")
    print(f"Model: {identity.model_number}")
    print(f"Category: {identity.category}")
    print(f"Specs: {identity.specifications}")
    print(f"Confidence: {identity.confidence}")
    print(f"Method: {identity.extraction_method}")