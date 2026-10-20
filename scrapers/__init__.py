# Direct submodule exports (no circular import)
from scrapers import amazon
from scrapers import flipkart
from scrapers import generic

# Lazy-load gem exports to avoid a circular import with core
_GEM_EXPORTS = ("extract_gem_product", "GemProduct", "classify_price", "parse_gem_url")


def __getattr__(name):
    if name in _GEM_EXPORTS:
        from scrapers import gem

        return globals().setdefault(name, getattr(gem, name))
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