"""Universal Price Comparison - Core package."""
from gem_price.core.config import settings
from gem_price.core.models import (
    ProductIdentity,
    MarketplaceResult,
    VerificationResult,
    MarketplaceMatch,
    SearchResult,
    ProductCategory,
    MatchStatus
)

__all__ = [
    "settings",
    "ProductIdentity",
    "MarketplaceResult",
    "VerificationResult",
    "MarketplaceMatch",
    "SearchResult",
    "ProductCategory",
    "MatchStatus",
]