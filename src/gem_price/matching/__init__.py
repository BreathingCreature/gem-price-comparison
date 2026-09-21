"""Matching package for GeM Price Comparison."""
from gem_price.matching.engine import (
    normalize, extract_slug, extract_attributes, detect_category,
    build_queries_for_category, soft_attribute_overlap,
    cos_score, gates_pass, composite_score, get_model,
    ProductCategory, match_single_gem_product, KNOWN_BRANDS
)
from gem_price.matching.llm_attributes import get_product_attributes
from gem_price.matching.verifier import (
    verify_match, verify_exact_match, brands_match, model_numbers_match,
    extract_brand_from_name, extract_model_from_name, extract_specs_from_name
)

__all__ = [
    "normalize", "extract_slug", "extract_attributes", "detect_category",
    "build_queries_for_category", "soft_attribute_overlap",
    "cos_score", "gates_pass", "composite_score", "get_model",
    "ProductCategory", "match_single_gem_product", "KNOWN_BRANDS",
    "get_product_attributes",
    "verify_match", "verify_exact_match", "brands_match",
    "model_numbers_match", "extract_brand_from_name",
    "extract_model_from_name", "extract_specs_from_name",
]