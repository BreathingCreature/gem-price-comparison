"""FastAPI endpoint for real-time GeM → Flipkart price comparison.

Usage:
    python -m api.main
    # Then: GET http://localhost:8000/match?gem_url=<GeM_product_URL>

Returns JSON with top matches including prices, similarity scores, and gates.
"""
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl
from typing import Optional, List
import sqlite3
import re
import sys
from pathlib import Path

# Add project root to path
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from matching.match_engine import (
    normalize, extract_slug, extract_attributes, detect_category,
    build_queries_for_category, soft_attribute_overlap,
    embed, cos_score, gates_pass, composite_score, get_model,
    ProductCategory, _MODEL
)
from matching.llm_attributes import get_product_attributes

DB_PATH = BASE / "db" / "gem_project.db"

app = FastAPI(
    title="GeM Price Comparison API",
    description="Real-time price comparison: GeM product → Flipkart matches",
    version="2.0.0"
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
    
    # GeM product page selectors
    name_elem = soup.select_one(".variant-title, .product-title, h1")
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
    # Check cache first
    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()
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
    gem_attrs = extract_attributes(gem_enriched)
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
        
        results.append({
            "flipkart_id": fid,
            "flipkart_name": fname,
            "flipkart_price": fprice_str,
            "flipkart_link": flink or "",
            "cosine_similarity": round(cos, 4),
            "attribute_overlap": round(attr_score, 3),
            "composite_score": round(comp, 4),
            "gates": gates,
            "category": category.category,
            "category_confidence": category.confidence,
        })
    
    # Sort by composite score descending
    results.sort(key=lambda r: -r["composite_score"])
    results = results[:top_k]
    
    # Convert to MatchResult objects using factory
    match_results = []
    for r in results:
        mr = MatchResult(
            flipkart_id=r["flipkart_id"],
            flipkart_name=r["flipkart_name"],
            flipkart_price=str(r["flipkart_price"]) if r["flipkart_price"] is not None else "",
            flipkart_link=r["flipkart_link"] or "",
            cosine_similarity=r["cosine_similarity"],
            attribute_overlap=r["attribute_overlap"],
            composite_score=r["composite_score"],
            gates=r["gates"],
            category=r["category"],
            category_confidence=r["category_confidence"],
        )
        match_results.append(mr)
    
    con.close()
    
    return MatchResponse(
        gem_product={"id": gem_id, "name": gem_name, "price": gem_price, "link": gem_link},
        category=category,
        matches=match_results,
        query_used=queries
    )


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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    try:
        get_model()
        return {"status": "ok", "model_loaded": True}
    except Exception:
        return {"status": "ok", "model_loaded": False}


# Global model reference for health check
_MODEL = None

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)