# Direct submodule exports (no circular import)
from scrapers import amazon
from scrapers import flipkart
from scrapers import generic

# Lazy-load gem exports to avoid circular import with core
def __getattr__(name):
    if name in ("extract_gem_product", "GemProduct", "classify_price", "parse_gem_url"):
        from scrapers.gem import extract_gem_product, GemProduct, classify_price, parse_gem_url
        return globals().setdefault(name, eval(name))
    raise AttributeError(f"module 'scrapers' has no attribute '{name}'")

__all__ = [
    "amazon",
    "flipkart",
    "generic",
    "extract_gem_product",
    "GemProduct",
    "classify_price",
    "parse_gem_url",
]