"""Core data models for Universal Price Comparison."""
from typing import Dict, List, Optional, Any
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field, HttpUrl


class ProductCategory(str, Enum):
    """Product categories for exact matching rules."""
    MONITOR = "monitor"
    LAPTOP = "laptop"
    DESKTOP = "desktop"
    MOBILE = "mobile"
    TABLET = "tablet"
    KEYBOARD = "keyboard"
    MOUSE = "mouse"
    HEADPHONE = "headphone"
    PRINTER = "printer"
    STORAGE = "storage"
    GRAPHICS_CARD = "graphics_card"
    PROCESSOR = "processor"
    MOTHERBOARD = "motherboard"
    RAM = "ram"
    POWER_SUPPLY = "power_supply"
    UNKNOWN = "unknown"


class MatchStatus(str, Enum):
    """Result of exact match verification."""
    EXACT_MATCH = "exact_match"
    NEAR_MATCH = "near_match"
    SPEC_MISMATCH = "spec_mismatch"
    MODEL_MISMATCH = "model_mismatch"
    NOT_FOUND = "not_found"


class ProductIdentity(BaseModel):
    """Canonical product identity extracted from GeM."""
    brand: str
    model_number: str
    product_type: str
    category: ProductCategory = ProductCategory.UNKNOWN
    specifications: Dict[str, Any] = Field(default_factory=dict)
    raw_name: str = ""
    raw_specs: str = ""
    source_url: str = ""
    price: str = ""
    confidence: float = 0.0
    extraction_method: str = "unknown"
    extracted_at: datetime = Field(default_factory=datetime.utcnow)


class MarketplaceResult(BaseModel):
    """A single product listing from a marketplace."""
    marketplace: str
    product_name: str
    price: float
    currency: str = "INR"
    seller: str = ""
    url: str = ""
    in_stock: bool = True
    specs: Dict[str, Any] = Field(default_factory=dict)
    image_url: str = ""
    rating: Optional[float] = None
    review_count: Optional[int] = None
    raw_data: Dict[str, Any] = Field(default_factory=dict)
    scraped_at: datetime = Field(default_factory=datetime.utcnow)


class VerificationResult(BaseModel):
    """Result of exact match verification."""
    is_exact_match: bool
    match_status: MatchStatus
    confidence: float
    matched_specs: List[str] = Field(default_factory=list)
    mismatched_specs: List[str] = Field(default_factory=list)
    missing_specs: List[str] = Field(default_factory=list)
    model_match: bool = False
    brand_match: bool = False
    details: Dict[str, Any] = Field(default_factory=dict)


class MarketplaceMatch(BaseModel):
    """A verified match from a marketplace."""
    marketplace: str
    product_name: str
    price: float
    currency: str = "INR"
    seller: str = ""
    url: str = ""
    in_stock: bool = True
    image_url: str = ""
    verification: "VerificationResult"
    raw_result: "MarketplaceResult"


class SearchQuery(BaseModel):
    """A search query built from product identity."""
    query: str
    source: str
    priority: int = 1


class SearchResult(BaseModel):
    """Complete search result for a GeM product."""
    gem_product: ProductIdentity
    matches: List["MarketplaceMatch"] = []
    near_matches: List["MarketplaceMatch"] = []
    total_searched: int = 0
    exact_matches_count: int = 0
    search_duration: float = 0.0
    queries_used: List[str] = Field(default_factory=list)
    searched_at: datetime = Field(default_factory=datetime.utcnow)
    cache_hit: bool = False


class PriceComparisonRequest(BaseModel):
    """Request for price comparison."""
    gem_url: str
    top_k: int = 10
    threshold: float = 0.9
    marketplaces: Optional[List[str]] = None
    force_refresh: bool = False


class PriceComparisonResponse(BaseModel):
    """Response for price comparison."""
    gem_product: ProductIdentity
    exact_matches: List["MarketplaceMatch"] = []
    near_matches: List["MarketplaceMatch"] = []
    not_found: List[str] = []
    total_searched: int = 0
    search_duration: float = 0.0
    queries_used: List[str] = []
    cached: bool = False


# Update forward references
MarketplaceMatch.model_rebuild()
SearchResult.model_rebuild()
PriceComparisonResponse.model_rebuild()