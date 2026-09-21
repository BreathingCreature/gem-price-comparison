"""API request/response schemas."""
from typing import List, Optional
from pydantic import BaseModel, HttpUrl, Field
from gem_price.core.models import ProductIdentity, MarketplaceMatch, VerificationResult, MatchStatus


class PriceComparisonRequest(BaseModel):
    """Request for price comparison."""
    gem_url: HttpUrl
    top_k: int = Field(default=10, ge=1, le=50)
    threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    marketplaces: Optional[List[str]] = None
    force_refresh: bool = False


class MarketplaceMatchResponse(BaseModel):
    """Marketplace match in API response."""
    marketplace: str
    product_name: str
    price: float
    currency: str = "INR"
    seller: str = ""
    url: str
    in_stock: bool = True
    image_url: str = ""
    verification: dict


class PriceComparisonResponse(BaseModel):
    """Response for price comparison."""
    gem_product: dict
    exact_matches: List[MarketplaceMatchResponse] = []
    near_matches: List[MarketplaceMatchResponse] = []
    not_found: List[str] = []
    total_searched: int = 0
    search_duration: float = 0.0
    queries_used: List[str] = []
    cached: bool = False


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "ok"
    model_loaded: bool = False
    version: str = "2.0.0"