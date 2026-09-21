"""FastAPI endpoint for real-time GeM → Flipkart price comparison.

Usage:
    python -m gem_price.api.main
    # Then: GET http://localhost:8000/match?gem_url=<GeM_product_URL>

Returns JSON with top matches including prices, similarity scores, and gates.
"""
import sqlite3
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, HttpUrl

from gem_price.core.config import settings
from gem_price.core.logging import setup_logging
from gem_price.matching.engine import (
    normalize, extract_slug, extract_attributes, detect_category,
    build_queries_for_category, soft_attribute_overlap,
    cos_score, gates_pass, composite_score, get_model,
    ProductCategory, KNOWN_BRANDS
)
from gem_price.matching.llm_attributes import get_product_attributes
from gem_price.utils.text import extract_attributes as extract_attrs_utils

logger = setup_logging(__name__)

DB_PATH = settings.db_path


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup: warm up model
    try:
        get_model()
        logger.info("Embedding model loaded")
    except Exception as e:
        logger.warning(f"Could not load embedding model: {e}")
    yield
    # Shutdown
    logger.info("Shutting down")


app = FastAPI(
    title="GeM Price Comparison API",
    description="Real-time price comparison: GeM product → Flipkart matches",
    version="2.0.0",
    lifespan=lifespan
)


class MatchResult(BaseModel):
    flipkart_id: int
    flipkart_name: str
    flipkart_price: str
    flipkart_link: str
    cosine_similarity: float
    attribute_overlap: float
    composite_score: float
    gates: dict
    category: str
    category_confidence: float


class MatchResponse(BaseModel):
    gem_product: dict
    category: ProductCategory
    matches: List[MatchResult]
    query_used: List[str]


def scrape_gem_product(gem_url: str) -> dict:
    """Scrape a single GeM product page for name, price, link."""
    import requests
    from bs4 import BeautifulSoup
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    resp = requests.get(gem_url, headers=headers, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")
    
    # GeM product page selectors (consistent with gem_scraper)
    name_elem = soup.select_one(".variant-desc .variant-title, .variant-title, .product-title, h1")
    price_elem = soup.select_one(".variant-final-price, .price, .product-price")
    seller_elem = soup.select_one(".sold_as.oem, .seller, .brand")
    
    name = name_elem.get_text(strip=True) if name_elem else ""
    price = price_elem.get_text(strip=True) if price_elem else ""
    seller = seller_elem.get_text(strip=True) if seller_elem else ""
    
    return {
        "name": name,
        "price": price,
        "seller": seller,
        "link": gem_url
    }


def match_gem_url(gem_url: str, top_k: int = 10, threshold: float = 0.55) -> MatchResponse:
    """Core matching logic for a GeM URL."""
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    
    try:
        # Check cache first
        cached = cur.execute(
            "SELECT id, name, price, link FROM raw_products WHERE source='gem' AND link=?", 
            (gem_url,)
        ).fetchone()
        
        if cached:
            gem_id, gem_name, gem_price, gem_link = cached
        else:
            # Scrape fresh
            scraped = scrape_gem_product(gem_url)
            gem_name = scraped["name"]
            gem_price = scraped["price"]
            gem_link = scraped["link"]
            # Insert into DB
            cur.execute(
                "INSERT INTO raw_products (source, name, price, seller, link, scraped_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
                ("gem", gem_name, gem_price, scraped["seller"], gem_link)
            )
            con.commit()
            gem_id = cur.lastrowid
        
        # Get LLM attributes (cached or fresh)
        llm_attrs = get_product_attributes(gem_name, gem_link, con)
        
        # Detect category
        category = detect_category(gem_name, gem_link, llm_attrs)
        
        # Build category-specific queries
        queries = build_queries_for_category(gem_name, gem_link, llm_attrs, category)
        
        # Get all Flipkart products (in production: use search API with queries)
        fk_rows = list(cur.execute(
            "SELECT id, name, price, link FROM raw_products WHERE source='flipkart'"))
        
        if not fk_rows:
            con.close()
            return MatchResponse(
                gem_product={"id": gem_id, "name": gem_name, "price": gem_price, "link": gem_link},
                category=category,
                matches=[],
                query_used=queries
            )
        
        # Build enriched GeM text
        slug = extract_slug(gem_link or "")
        gem_enriched = f"{gem_name or ''} {slug}".strip()
        gem_norm = normalize(gem_enriched)
        
        # Extract GeM attributes for soft filter
        gem_attrs = extract_attributes(gem_enriched, KNOWN_BRANDS)
        if llm_attrs.get("brand"):
            gem_attrs["brand"] = llm_attrs["brand"].lower()
        
        # Prepare candidate texts
        fk_names = [(fid, fname or "", fprice or "", flink or "") for fid, fname, fprice, flink in fk_rows]
        fk_texts = [normalize(fname or "") for _, fname, _, _ in fk_rows]
        
        # Compute cosine similarities
        gem_texts = [gem_norm] * len(fk_texts)
        cos_sims = cos_score(gem_texts, fk_texts)
        
        # Score each candidate
        results = []
        for (fid, fname, fprice, flink), cos in zip(fk_names, cos_sims):
            attr_score = soft_attribute_overlap(gem_attrs, fname)
            gates = gates_pass(gem_enriched, fname, category.category, cos, threshold, gem_link, flink)
            comp = composite_score(cos, gates, category.category)
            
            # Ensure price is string
            fprice_str = "" if fprice is None else str(fprice)
            
            results.append(MatchResult(
                flipkart_id=fid,
                flipkart_name=fname,
                flipkart_price=fprice_str,
                flipkart_link=flink or "",
                cosine_similarity=round(cos, 4),
                attribute_overlap=round(attr_score, 3),
                composite_score=round(comp, 4),
                gates=gates,
                category=category.category,
                category_confidence=category.confidence,
            ))
        
        # Sort by composite score descending
        results.sort(key=lambda r: -r.composite_score)
        results = results[:top_k]
        
        con.close()
        
        return MatchResponse(
            gem_product={"id": gem_id, "name": gem_name, "price": gem_price, "link": gem_link},
            category=category,
            matches=results,
            query_used=queries
        )
    except Exception as e:
        con.close()
        raise


@app.get("/match", response_model=MatchResponse)
async def match_endpoint(
    gem_url: str = Query(..., description="GeM product URL"),
    top_k: int = Query(10, ge=1, le=50),
    threshold: float = Query(0.55, ge=0.0, le=1.0)
):
    """Find Flipkart matches for a GeM product URL."""
    # Validate GeM URL
    if "gem.gov.in" not in gem_url and "mkp.gem.gov.in" not in gem_url:
        raise HTTPException(status_code=400, detail="URL must be a GeM product page")
    
    try:
        return match_gem_url(gem_url, top_k, threshold)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Match error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    try:
        get_model()
        return {"status": "ok", "model_loaded": True}
    except Exception:
        return {"status": "ok", "model_loaded": False}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)