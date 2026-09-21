"""FastAPI endpoint for real-time GeM → Flipkart price comparison.

Usage:
    python -m gem_price.api.main
    # Then: GET http://localhost:8000/match?gem_url=<GeM_product_URL>

Given ANY GeM product URL, this will:
1. Scrape the GeM product page (on-demand)
2. Extract attributes via LLM (cached) or regex fallback
3. Build category-specific search queries
3. Search Flipkart IN REAL-TIME for that specific product
4. Return ranked matches with scores and gates
"""
import sqlite3
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

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
from gem_price.scrapers.flipkart import scrape_flipkart
from gem_price.scrapers.gem import scrape_gem as scrape_gem_batch

logger = setup_logging(__name__)

DB_PATH = settings.db_path


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    try:
        get_model()
        logger.info("Embedding model loaded")
    except Exception as e:
        logger.warning(f"Could not load embedding model: {e}")
    yield
    logger.info("Shutting down")


app = FastAPI(
    title="GeM Price Comparison API",
    description="Real-time price comparison: GeM product URL → Flipkart matches (no pre-loading required)",
    version="2.0.0",
    lifespan=lifespan
)


class MatchResult(BaseModel):
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
    queries_used: List[str]
    flipkart_searched: int


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
    """Core matching logic: GeM URL → real-time Flipkart search → ranked matches."""
    # 1. Scrape GeM product (with optional DB cache)
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    
    try:
        cached = cur.execute(
            "SELECT id, name, price, link FROM raw_products WHERE source='gem' AND link=?", 
            (gem_url,)
        ).fetchone()
        
        if cached:
            gem_id, gem_name, gem_price, gem_link = cached
            logger.info(f"Cache hit for GeM product: {gem_name[:50]}")
        else:
            logger.info(f"Scraping GeM product: {gem_url}")
            scraped = scrape_gem_product(gem_url)
            gem_name = scraped["name"]
            gem_price = scraped["price"]
            gem_link = scraped["link"]
            if not gem_name:
                raise HTTPException(status_code=400, detail="Could not extract product name from GeM page")
            cur.execute(
                "INSERT INTO raw_products (source, name, price, seller, link, scraped_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
                ("gem", gem_name, gem_price, scraped["seller"], gem_link)
            )
            con.commit()
            gem_id = cur.lastrowid
            logger.info(f"Scraped new GeM product: {gem_name[:50]}")
        
        # 2. Get LLM attributes (cached or fresh)
        llm_attrs = get_product_attributes(gem_name, gem_link, con)
        
        # 3. Detect category
        category = detect_category(gem_name, gem_link, llm_attrs)
        logger.info(f"Detected category: {category.category} (confidence: {category.confidence})")
        
        # 4. Build category-specific search queries
        queries = build_queries_for_category(gem_name, gem_link, llm_attrs, category)
        logger.info(f"Built {len(queries)} search queries: {queries}")
        
        # 5. REAL-TIME Flipkart search for EACH query (deduplicated results)
        all_fk_results = []
        seen_links = set()
        
        for query in queries:
            logger.info(f"Searching Flipkart: '{query}'")
            try:
                fk_results = scrape_flipkart(query, max_results=5)  # top 5 per query
                for r in fk_results:
                    if r["link"] not in seen_links:
                        seen_links.add(r["link"])
                        all_fk_results.append(r)
            except Exception as e:
                logger.warning(f"Flipkart search failed for '{query}': {e}")
                continue
        
        logger.info(f"Total unique Flipkart results: {len(all_fk_results)}")
        
        if not all_fk_results:
            con.close()
            return MatchResponse(
                gem_product={"id": gem_id, "name": gem_name, "price": gem_price, "link": gem_link},
                category=category,
                matches=[],
                queries_used=queries,
                flipkart_searched=0
            )
        
        # 6. Build enriched GeM text for embedding
        slug = extract_slug(gem_link or "")
        gem_enriched = f"{gem_name or ''} {slug}".strip()
        gem_norm = normalize(gem_enriched)
        
        # 7. Extract GeM attributes for soft filter
        gem_attrs = extract_attributes(gem_enriched, KNOWN_BRANDS)
        if llm_attrs.get("brand"):
            gem_attrs["brand"] = llm_attrs["brand"].lower()
        
        # 8. Prepare candidate texts
        fk_names = [(fname or "", fprice or "", flink or "") for fname, fprice, flink in 
                    [(r["name"], r["price"], r["link"]) for r in all_fk_results]]
        fk_texts = [normalize(fname or "") for fname, _, _ in fk_names]
        
        # 9. Compute cosine similarities
        gem_texts = [gem_norm] * len(fk_texts)
        cos_sims = cos_score(gem_texts, fk_texts)
        
        # 10. Score each candidate
        results = []
        for (fname, fprice, flink), cos in zip(fk_names, cos_sims):
            attr_score = soft_attribute_overlap(gem_attrs, fname)
            gates = gates_pass(gem_enriched, fname, category.category, cos, threshold, gem_link, flink)
            comp = composite_score(cos, gates, category.category)
            
            results.append(MatchResult(
                flipkart_name=fname,
                flipkart_price=str(fprice) if fprice else "",
                flipkart_link=flink or "",
                cosine_similarity=round(cos, 4),
                attribute_overlap=round(attr_score, 3),
                composite_score=round(comp, 4),
                gates=gates,
                category=category.category,
                category_confidence=category.confidence,
            ))
        
        # 11. Sort by composite score descending
        results.sort(key=lambda r: -r.composite_score)
        results = results[:top_k]
        
        con.close()
        
        return MatchResponse(
            gem_product={"id": gem_id, "name": gem_name, "price": gem_price, "link": gem_link},
            category=category,
            matches=results,
            queries_used=queries,
            flipkart_searched=len(all_fk_results)
        )
    except HTTPException:
        con.close()
        raise
    except Exception as e:
        con.close()
        logger.error(f"Match error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/match", response_model=MatchResponse)
async def match_endpoint(
    gem_url: str = Query(..., description="GeM product URL (e.g., https://mkp.gem.gov.in/...)"),
    top_k: int = Query(10, ge=1, le=50, description="Number of top matches to return"),
    threshold: float = Query(0.55, ge=0.0, le=1.0, description="Minimum composite score threshold")
):
    """Find Flipkart matches for a GeM product URL.
    
    No pre-loading required. Works for ANY GeM product URL.
    """
    if "gem.gov.in" not in gem_url and "mkp.gem.gov.in" not in gem_url:
        raise HTTPException(status_code=400, detail="URL must be a GeM product page (mkp.gem.gov.in or gem.gov.in)")
    
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