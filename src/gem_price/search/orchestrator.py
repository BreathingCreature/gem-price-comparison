"""Search orchestrator coordinating multi-source product search."""
import asyncio
import random
import time
from typing import List, Dict, Any, Optional, Set
from datetime import datetime
from dataclasses import dataclass, field

from gem_price.core.config import settings
from gem_price.core.logging import setup_logging
from gem_price.core.models import ProductIdentity, MarketplaceResult, MarketplaceMatch
from gem_price.search.google_shopping import search_google_shopping
from gem_price.scrapers.marketplaces import AmazonScraper, FlipkartScraper
from gem_price.matching.verifier import verify_match, verify_exact_match
from gem_price.core.models import (
    ProductIdentity, MarketplaceResult, MarketplaceMatch,
    SearchResult, VerificationResult, MatchStatus
)
from gem_price.core.logging import setup_logging

logger = setup_logging(__name__)


@dataclass
class SearchCandidate:
    """A candidate product from a marketplace search."""
    marketplace: str
    product_name: str
    price: str
    url: str
    image_url: str = ""
    seller: str = ""
    raw_data: dict = field(default_factory=dict)
    source: str = ""


class SearchOrchestrator:
    """Orchestrates multi-source product search and verification."""
    
    def __init__(self):
        self.google_shopping = None
        self.amazon_scraper = None
        self.flipkart_scraper = None
        self._initialized = False
    
    async def initialize(self):
        """Initialize scrapers."""
        if self._initialized:
            return
        
        # Initialize scrapers
        self.amazon_scraper = AmazonScraper()
        self.flipkart_scraper = FlipkartScraper()
        self._initialized = True
        logger.info("Search orchestrator initialized")
    
    async def close(self):
        """Clean up resources."""
        if self.amazon_scraper:
            self.amazon_scraper.close()
        if self.flipkart_scraper:
            self.flipkart_scraper.close()
    
    def build_search_queries(self, identity) -> List[str]:
        """Build search queries from product identity."""
        queries = []
        brand = identity.brand
        model = identity.model_number
        product_type = identity.product_type
        specs = identity.specifications
        
        # Primary: brand + model (most specific)
        if model:
            queries.append(f"{identity.brand} {identity.model_number}")
        
        # Brand + key specs
        if brand and specs:
            key_specs = []
            for key in ["screen_size", "resolution", "refresh_rate", "cpu", "ram", "storage"]:
                if key in specs:
                    specs_list.append(specs[key])
                    if len(key_specs) >= 2:
                        break
            if key_specs:
                queries.append(f"{brand} {' '.join(key_specs)}")
        
        # Brand + product type
        if brand and product_type:
            queries.append(f"{brand} {product_type}")
        
        # Model only
        if model:
            queries.append(model)
        
        # Brand + category
        if brand:
            cat = identity.category.value if hasattr(identity.category, 'value') else str(identity.category)
            queries.append(f"{brand} {cat}")
        
        # Remove duplicates
        unique = []
        for q in queries:
            q = q.strip()
            if q and q not in unique:
                unique.append(q)
        
        return unique[:5]
    
    async def search_all_sources(self, identity) -> List[Dict[str, Any]]:
        """Search all enabled marketplaces for the product."""
        if not self._initialized:
            await self.initialize()
        
        # Build queries
        queries = self.build_search_queries(identity)
        logger.info(f"Search queries: {queries}")
        
        # Collect candidates from all sources
        all_candidates = []
        
        # 1. Google Shopping (primary discovery)
        if "google_shopping" in settings.enabled_marketplaces:
            try:
                logger.info("Searching Google Shopping...")
                google_results = await search_google_shopping(identity)
                for r in google_results:
                    r["source"] = "google_shopping"
                all_results.extend(google_results)
                await asyncio.sleep(random.uniform(1, 2))
            except Exception as e:
                logger.warning(f"Google Shopping search failed: {e}")
        
        # 2. Direct marketplace searches
        marketplace_scrapers = {
            "amazon": self.amazon_scraper,
            "flipkart": self.flipkart_scraper,
        }
        
        for marketplace, scraper in marketplace_scrapers.items():
            if marketplace not in settings.enabled_marketplaces:
                continue
            
            for query in queries[:3]:  # Limit queries per marketplace
                try:
                    logger.info(f"Searching {marketplace}: {query}")
                    results = scraper.search(query, max_results=5)
                    for r in results:
                        r["source"] = marketplace
                    all_results.extend(results)
                    await asyncio.sleep(random.uniform(1, 2))
                except Exception as e:
                    logger.warning(f"{marketplace} search failed: {e}")
        
        # Deduplicate by URL
        seen_urls = set()
        unique = []
        for r in all_results:
            url = r.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique.append(r)
        
        logger.info(f"Total unique candidates: {len(unique)}")
        return unique
    
    async def search_and_verify(
        self,
        identity: 'ProductIdentity',
        threshold: float = 0.9,
        top_k: int = 10
    ) -> 'SearchResult':
        """Search all sources and verify matches."""
        from gem_price.core.models import SearchResult, MarketplaceMatch, VerificationResult, MatchStatus
        
        start_time = time.time()
        
        # Get candidates
        candidates = await self.search_all_sources(identity)
        logger.info(f"Found {len(candidates)} candidates to verify")
        
        # Verify each candidate
        exact_matches = []
        near_matches = []
        
        for candidate in candidates:
            # Create MarketplaceResult
            mp_result = MarketplaceResult(
                marketplace=candidate.get("marketplace", "Unknown"),
                product_name=candidate.get("product_name", ""),
                price=candidate.get("price", 0),
                url=candidate.get("url", ""),
                seller=candidate.get("seller", ""),
                image_url=candidate.get("image_url", ""),
                raw_data=candidate.get("raw_data", {}),
            )
            
            # Verify match
            from gem_price.matching.verifier import verify_match
            verification = verify_match(identity, mp_result)
            
            match = MarketplaceMatch(
                marketplace=mp_result.marketplace,
                product_name=mp_result.product_name,
                price=mp_result.price,
                currency=mp_result.currency,
                seller=mp_result.seller,
                url=mp_result.url,
                in_stock=mp_result.in_stock,
                image_url=mp_result.image_url,
                verification=verification,
                raw_result=mp_result
            )
            
            if verification.is_exact_match and verification.confidence >= threshold:
                exact_matches.append(match)
            elif verification.confidence >= threshold * 0.7:
                near_matches.append(match)
        
        # Sort by confidence
        exact_matches.sort(key=lambda m: m.verification.confidence, reverse=True)
        near_matches.sort(key=lambda m: m.verification.confidence, reverse=True)
        
        # Build result
        search_duration = time.time() - start_time
        
        from gem_price.core.models import SearchResult
        return SearchResult(
            gem_product=identity,
            matches=exact_matches[:top_k],
            near_matches=near_matches[:top_k],
            total_searched=len(all_results),
            exact_matches_count=len(exact_matches),
            search_duration=time.time() - start_time,
            queries_used=[],
            searched_at=datetime.utcnow(),
            cache_hit=False
        )


# Convenience function
async def search_and_compare(
    gem_url: str,
    top_k: int = 10,
    threshold: float = 0.9
) -> 'SearchResult':
    """High-level search and compare function."""
    from gem_price.scrapers.gem import scrape_gem_product
    from gem_price.extraction.fallback_extractor import extract_identity_hybrid
    
    # Step 1: Scrape GeM product
    logger.info(f"Scraping GeM product: {gem_url}")
    gem_identity = scrape_gem_product(gem_url)
    
    # Step 2: Extract identity (LLM + fallback)
    logger.info("Extracting product identity...")
    identity = extract_identity_hybrid("", gem_url)  # Will re-scrape
    # Actually need to use the scraped data
    # For now, just use the scraped identity
    
    # Step 3: Search and verify
    orchestrator = SearchOrchestrator()
    try:
        result = await orchestrator.search_and_verify(identity, threshold=0.9, top_k=10)
        return result
    finally:
        await orchestrator.close()


# Quick test
if __name__ == "__main__":
    import asyncio
    import sys
    sys.path.insert(0, r"C:\Users\Creature\Creature Folder\Synced\Project\code\src")
    
    async def test():
        from gem_price.scrapers.gem import scrape_gem_product
        from gem_price.extraction.fallback_extractor import extract_identity_hybrid
        
        url = "https://mkp.gem.gov.in/computer-mouse/lapcare-l70-plus/p-5116877-18283206337-cat.html"
        gem = scrape_gem_product(url)
        identity = extract_identity_hybrid("", url)
        identity.source_url = url
        
        orchestrator = SearchOrchestrator()
        result = await orchestrator.search_and_verify(identity, threshold=0.9, top_k=5)
        
        print(f"GeM: {result.gem_product.raw_name}")
        print(f"Category: {result.gem_product.category}")
        print(f"Exact matches: {len(result.matches)}")
        for m in result.matches:
            v = m.verification
            print(f"  {m.marketplace}: {m.product_name[:50]} | ${m.price} | conf={v.confidence:.2f} | {v.match_status}")
        
        await orchestrator.close()
    
    asyncio.run(test())