"""Scrapers package for GeM Price Comparison."""
from gem_price.scrapers.gem import scrape_gem_product as scrape_gem, GeMScraper
from gem_price.scrapers.flipkart import scrape_flipkart
from gem_price.scrapers.query_builder import build_queries, raw_slug
from gem_price.scrapers.per_product_lookup import load_gem_products, search_candidates, try_queries

__all__ = [
    "scrape_gem", "GeMScraper",
    "scrape_flipkart",
    "build_queries", "raw_slug",
    "load_gem_products", "search_candidates", "try_queries",
]