"""Google Shopping search for product discovery."""
import re
import urllib.parse
from typing import List, Dict, Any, Optional
from datetime import datetime

import aiohttp
import asyncio
from bs4 import BeautifulSoup

from gem_price.core.config import settings
from gem_price.core.logging import setup_logging
from gem_price.core.models import ProductIdentity

logger = setup_logging(__name__)


class GoogleShoppingSearcher:
    """Search Google Shopping for product listings across marketplaces."""
    
    def __init__(self):
        self.base_url = "https://www.google.com/search"
        self.session = None
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-IN,en;q=0.9,en-US;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
    
    def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=settings.search_timeout)
            connector = aiohttp.TCPConnector(limit=5, limit_per_host=2)
            self.session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=timeout,
                connector=connector
            )
        return self.session
    
    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
    
    def build_queries(self, identity: 'ProductIdentity') -> List[str]:
        """Build search queries from product identity."""
        queries = []
        brand = identity.brand
        model = identity.model_number
        product_type = identity.product_type
        specs = identity.specifications
        
        # Primary: brand + model (most specific)
        if identity.model_number:
            queries.append(f"{identity.brand} {identity.model_number}")
        
        # Brand + key specs
        if brand and specs:
            key_specs = []
            for key in ["screen_size", "resolution", "refresh_rate", "cpu", "ram", "storage"]:
                if key in identity.specifications:
                    key_specs.append(identity.specifications[key])
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
            queries.append(f"{brand} {identity.category.value if hasattr(identity.category, 'value') else str(identity.category)}")
        
        # Remove duplicates and empty
        unique = []
        for q in queries:
            q = q.strip()
            if q and q not in unique:
                unique.append(q)
        
        return unique[:5]  # Limit to 5 queries
    
    async def search(self, identity: 'ProductIdentity') -> List[Dict[str, Any]]:
        """Search Google Shopping for product listings."""
        queries = self.build_queries(identity)
        logger.info(f"Google Shopping queries: {queries}")
        
        all_results = []
        session = self._get_session()
        
        for query in queries:
            try:
                results = await self._search_query(query, session)
                all_results.extend(results)
                # Small delay between queries
                await asyncio.sleep(random.uniform(1, 2))
            except Exception as e:
                logger.warning(f"Google search failed for '{query}': {e}")
        
        # Deduplicate by URL
        seen = set()
        unique = []
        for r in all_results:
            url = r.get("url", "")
            if url and url not in seen:
                seen.add(url)
                unique.append(r)
        
        return unique[:settings.max_search_results]
    
    async def _search_query(self, query: str, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        """Execute a single search query."""
        encoded = urllib.parse.quote_plus(query + " site:amazon.in OR site:flipkart.com OR site:reliancedigital.in OR site:croma.com OR site:vijaysales.com OR site:mdcomputers.in OR site:primeabgb.com")
        url = f"{self.base_url}?tbm=shop&q={encoded}&gl=in&hl=en"
        
        try:
            async with session.get(url) as resp:
                html = await resp.text()
                return self._parse_results(html)
        except Exception as e:
            logger.error(f"Google search failed: {e}")
            return []
    
    def _parse_results(self, html: str) -> List[Dict[str, Any]]:
        """Parse Google Shopping results HTML."""
        soup = BeautifulSoup(html, "html.parser")
        results = []
        
        # Google Shopping results selectors (these change frequently)
        # Try multiple selectors
        result_containers = []
        for selector in [
            ".sh-dgr__content",
            ".sh-dgr__grid-result",
            ".pla-unit",
            ".product-result",
            ".sh-dgr__grid-result",
            "div[data-docid]"
        ]:
            elems = soup.select(selector)
            if elems:
                result_containers = elems
                break
        
        for container in result_containers[:20]:
            try:
                # Extract product info
                name_elem = container.select_one("h3, .product-title, .product-name, h3 a")
                name = name_elem.get_text(strip=True) if name_elem else ""
                
                # Price
                price = ""
                for price_sel in [".price", ".price-text", "[aria-label*='price']", ".a-price-whole"]:
                    price_elem = container.select_one(price_sel)
                    if price_elem:
                        price = price_elem.get_text(strip=True)
                        break
                
                # Link
                link = ""
                link_elem = container.select_one("a[href]")
                if link_elem and link_elem.get("href"):
                    link = link_elem["href"]
                    if link.startswith("/"):
                        link = "https://www.google.com" + link
                
                # Seller/merchant
                seller = ""
                for sel in [".merchant-name", ".seller-name", ".merchant"]:
                    elem = container.select_one(sel)
                    if elem:
                        seller = elem.get_text(strip=True)
                        break
                
                # Image
                img = ""
                img_elem = container.select_one("img")
                if img_elem and img_elem.get("src"):
                    img = img_elem["src"]
                
                if not name:
                    continue
                
                # Determine marketplace from URL
                marketplace = "Unknown"
                link_lower = link.lower() if isinstance(link, str) else ""
                if "amazon" in link_lower:
                    marketplace = "Amazon.in"
                elif "flipkart" in link_lower:
                    marketplace = "Flipkart"
                elif "reliancedigital" in link_lower:
                    marketplace = "Reliance Digital"
                elif "croma" in link_lower:
                    marketplace = "Croma"
                elif "vijaysales" in link_lower:
                    marketplace = "Vijay Sales"
                elif "mdcomputers" in link_lower:
                    marketplace = "MD Computers"
                elif "primeabgb" in link_lower:
                    marketplace = "PrimeABGB"
                elif "samsung.com" in link_lower:
                    marketplace = "Samsung Official"
                elif "lg.com" in link_lower:
                    marketplace = "LG Official"
                elif "dell.com" in link_lower:
                    marketplace = "Dell Official"
                elif "hp.com" in link_lower:
                    marketplace = "HP Official"
                
                results.append({
                    "marketplace": marketplace,
                    "product_name": name,
                    "price": price,
                    "url": link,
                    "image_url": img,
                    "seller": seller,
                    "source": "google_shopping",
                    "raw_query": ""
                })
            
            except Exception as e:
                logger.debug(f"Failed to parse result: {e}")
                continue
        
        return results


async def search_google_shopping(identity: 'ProductIdentity') -> List[Dict[str, Any]]:
    """Main entry point for Google Shopping search."""
    searcher = GoogleShoppingSearcher()
    try:
        return await searcher.search(identity)
    finally:
        await searcher.close()


# Quick test
if __name__ == "__main__":
    import asyncio
    import sys
    sys.path.insert(0, r"C:\Users\Creature\Creature Folder\Synced\Project\code\src")
    
    from gem_price.core.models import ProductIdentity, ProductCategory
    
    identity = ProductIdentity(
        brand="Samsung",
        model_number="LS49C950UAWXXL",
        product_type="Monitor",
        category=ProductCategory.MONITOR,
        specifications={
            "screen_size": "49 inch",
            "resolution": "5120x1440",
            "refresh_rate": "240Hz",
            "panel_type": "QLED"
        }
    )
    
    results = asyncio.run(search_google_shopping(identity))
    for r in results[:5]:
        print(f"  {r['marketplace']}: {r['product_name'][:50]} - {r.get('price', 'N/A')}")