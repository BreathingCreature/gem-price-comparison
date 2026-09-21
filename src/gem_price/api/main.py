"""FastAPI application for Universal Price Comparison API."""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import HttpUrl

from gem_price.core.config import settings
from gem_price.core.logging import setup_logging
from gem_price.api.schemas import (
    PriceComparisonRequest,
    PriceComparisonResponse,
    HealthResponse
)

logger = setup_logging(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    logger.info("Starting Universal Price Comparison API...")
    try:
        # Warm up model
        from gem_price.matching.engine import get_model
        get_model()
        logger.info("Embedding model loaded")
    except Exception as e:
        logger.warning(f"Could not load embedding model: {e}")
    yield
    # Shutdown
    logger.info("Shutting down API...")


app = FastAPI(
    title="Universal Price Comparison API",
    description="Compare GeM product prices across Indian e-marketplaces in real-time",
    version="2.0.0",
    lifespan=lifespan
)


@app.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    try:
        from gem_price.matching.engine import get_model
        get_model()
        return HealthResponse(status="ok", model_loaded=True)
    except Exception:
        return HealthResponse(status="ok", model_loaded=False)


@app.post("/compare", response_model=PriceComparisonResponse)
async def compare_prices(request: PriceComparisonRequest):
    """Compare GeM product price across marketplaces."""
    from gem_price.scrapers.gem import scrape_gem_product
    from gem_price.extraction.fallback_extractor import extract_identity_hybrid
    from gem_price.search.orchestrator import SearchOrchestrator
    from gem_price.core.models import SearchResult
    
    gem_url = str(request.gem_url)
    
    # Scrape GeM product
    logger.info(f"Scraping GeM product: {request.gem_url}")
    gem = scrape_gem_product(str(request.gem_url))
    
    # Extract identity (LLM + fallback)
    logger.info("Extracting product identity...")
    identity = extract_identity_hybrid("", str(request.gem_url))
    identity.source_url = str(request.gem_url)
    
    # Search and verify
    orchestrator = SearchOrchestrator()
    try:
        result = await orchestrator.search_and_verify(
            identity,
            threshold=request.threshold,
            top_k=request.top_k
        )
        
        # Convert to API response
        exact_matches = []
        for match in result.matches:
            v = match.verification
            exact_matches.append({
                "marketplace": match.marketplace,
                "product_name": match.product_name,
                "price": match.price,
                "currency": match.currency,
                "seller": match.seller,
                "url": match.url,
                "in_stock": match.in_stock,
                "image_url": match.image_url,
                "verification": {
                    "is_exact_match": v.is_exact_match,
                    "match_status": v.match_status.value,
                    "confidence": v.confidence,
                    "matched_specs": v.matched_specs,
                    "mismatched_specs": v.mismatched_specs,
                    "missing_specs": v.missing_specs,
                    "model_match": v.model_match,
                    "brand_match": v.brand_match,
                }
            })
        
        near_matches = []
        for match in result.near_matches:
            v = match.verification
            near_matches.append({
                "marketplace": match.marketplace,
                "product_name": match.product_name,
                "price": match.price,
                "currency": match.currency,
                "seller": match.seller,
                "url": match.url,
                "in_stock": match.in_stock,
                "image_url": match.image_url,
                "verification": {
                    "is_exact_match": v.is_exact_match,
                    "match_status": v.match_status.value,
                    "confidence": v.confidence,
                    "matched_specs": v.matched_specs,
                    "mismatched_specs": v.mismatched_specs,
                    "missing_specs": v.missing_specs,
                }
            })
        
        return PriceComparisonResponse(
            gem_product={
                "id": result.gem_product.id if hasattr(result.gem_product, 'id') else 0,
                "name": result.gem_product.raw_name,
                "brand": result.gem_product.brand,
                "model_number": result.gem_product.model_number,
                "category": result.gem_product.category.value if hasattr(result.gem_product.category, 'value') else str(result.gem_product.category),
                "specifications": result.gem_product.specifications,
                "source_url": result.gem_product.source_url,
                "confidence": result.gem_product.confidence,
                "extraction_method": result.gem_product.extraction_method,
            },
            exact_matches=exact_matches,
            near_matches=near_matches,
            not_found=[],  # TODO: track not found marketplaces
            total_searched=result.total_searched,
            search_duration=result.search_duration,
            queries_used=result.queries_used,
            cached=result.cache_hit
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Match error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await orchestrator.close()


@app.get("/")
async def root():
    return {
        "name": "Universal Price Comparison API",
        "version": "2.0.0",
        "description": "Real-time GeM product price comparison across Indian marketplaces",
        "endpoints": {
            "POST /compare": "Compare GeM product price across marketplaces",
            "GET /health": "Health check"
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)