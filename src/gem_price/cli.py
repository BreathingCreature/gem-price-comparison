"""CLI interface for Universal Price Comparison."""
import asyncio
import sys
import argparse
import logging
from typing import List, Optional
from pathlib import Path

from gem_price.core.config import settings
from gem_price.core.logging import setup_logging
from gem_price.scrapers.gem import scrape_gem_product
from gem_price.extraction.fallback_extractor import extract_identity_hybrid
from gem_price.search.orchestrator import SearchOrchestrator
from gem_price.core.models import SearchResult
from gem_price.core.logging import setup_logging

logger = setup_logging(__name__)


async def compare_command(args: argparse.Namespace) -> int:
    """Compare GeM product price across marketplaces."""
    logger.info(f"Comparing GeM product: {args.gem_url}")
    
    # Scrape GeM product
    logger.info("Scraping GeM product...")
    gem = scrape_gem_product(args.gem_url)
    
    # Extract identity (hybrid LLM + fallback)
    logger.info("Extracting product identity...")
    identity = extract_identity_hybrid("", args.gem_url)
    identity.source_url = args.gem_url
    
    logger.info(f"Identified: {identity.brand} {identity.model_number} ({identity.category})")
    logger.info(f"Confidence: {identity.confidence:.2f}, Method: {identity.extraction_method}")
    
    # Search and verify
    orchestrator = SearchOrchestrator()
    try:
        result = await orchestrator.search_and_verify(
            identity,
            threshold=args.threshold,
            top_k=args.top_k
        )
        
        # Output results
        print(f"\n{'='*80}")
        print(f"GeM Product: {result.gem_product.raw_name or 'Unknown'}")
        print(f"Brand: {result.gem_product.brand} | Model: {result.gem_product.model_number}")
        print(f"Category: {result.gem_product.category} | Confidence: {result.gem_product.confidence:.2f}")
        print(f"Extraction: {result.gem_product.extraction_method}")
        print(f"{'='*80}")
        
        print(f"\nSearch Duration: {result.search_duration:.1f}s")
        print(f"Total Candidates Searched: {result.total_searched}")
        print(f"Exact Matches: {result.exact_matches_count}")
        
        if result.matches:
            print(f"\n{'='*80}")
            print("EXACT MATCHES")
            print(f"{'='*80}")
            for i, match in enumerate(result.matches, 1):
                v = match.verification
                print(f"\n  {i}. {match.marketplace}")
                print(f"     Product: {match.product_name}")
                print(f"     Price: ₹{match.price:,.2f} {match.currency}")
                print(f"     Seller: {match.seller or 'N/A'}")
                print(f"     URL: {match.url}")
                print(f"     Match Confidence: {match.verification.confidence:.2%}")
                print(f"     Status: {match.verification.match_status.value}")
                print(f"     Matched Specs: {', '.join(match.verification.matched_specs) or 'None'}")
                if match.verification.mismatched_specs:
                    print(f"     Mismatched: {', '.join(match.verification.mismatched_specs)}")
                if match.verification.missing_specs:
                    print(f"     Missing: {', '.join(match.verification.missing_specs)}")
                print(f"     URL: {match.url}")
        else:
            print("\nNo exact matches found.")
        
        if result.near_matches:
            print(f"\n{'='*80}")
            print("NEAR MATCHES (similar products)")
            print(f"{'='*80}")
            for i, match in enumerate(result.near_matches[:5], 1):
                v = match.verification
                print(f"  {i}. {match.marketplace}: {match.product_name[:60]}")
                print(f"     Price: ₹{match.price:,.2f} | Conf: {match.verification.confidence:.2%} | {match.verification.match_status.value}")
        
        if not result.matches and not result.near_matches:
            print("\nNo matches found on any marketplace.")
        
        return 0
    except Exception as e:
        logger.error(f"Comparison failed: {e}")
        return 1
    finally:
        await orchestrator.close()


async def scrape_command(args: argparse.Namespace) -> int:
    """Scrape a single marketplace for a query."""
    logger.info(f"Scraping {args.marketplace} for: {args.query}")
    
    if args.marketplace == "gem":
        result = scrape_gem_product(args.query)
        print(f"Name: {result.raw_name}")
        print(f"Brand: {result.brand}")
        print(f"Model: {result.model_number}")
        print(f"Category: {result.category}")
        print(f"Price: {result.price}")
    else:
        from gem_price.scrapers.marketplaces import AmazonScraper, FlipkartScraper
        
        scraper_map = {
            "amazon": AmazonScraper(),
            "flipkart": FlipkartScraper(),
        }
        
        scraper = scraper_map.get(args.marketplace.lower())
        if not scraper:
            logger.error(f"Unknown marketplace: {args.marketplace}")
            return 1
        
        try:
            results = scraper.search(args.query, max_results=args.max_results)
            for i, r in enumerate(results, 1):
                print(f"  {i}. {r['product_name'][:80]}")
                print(f"     Price: {r['price']} | URL: {r['url'][:80]}")
        finally:
            scraper.close()
    
    return 0


async def identity_command(args: argparse.Namespace) -> int:
    """Extract and display product identity from GeM URL."""
    logger.info(f"Extracting identity from: {args.gem_url}")
    
    gem = scrape_gem_product(args.gem_url)
    identity = extract_identity_hybrid("", args.gem_url)
    identity.source_url = args.gem_url
    
    print(f"\nProduct Identity:")
    print(f"  Brand: {identity.brand}")
    print(f"  Model: {identity.model_number}")
    print(f"  Type: {identity.product_type}")
    print(f"  Category: {identity.category}")
    print(f"  Confidence: {identity.confidence:.2%}")
    print(f"  Method: {identity.extraction_method}")
    print(f"\nSpecifications:")
    for k, v in identity.specifications.items():
        print(f"  {k}: {v}")
    
    return 0


async def test_gem_command(args: argparse.Namespace) -> int:
    """Test GeM scraping."""
    logger.info(f"Testing GeM scrape: {args.url}")
    gem = scrape_gem_product(args.url)
    print(f"Name: {gem.raw_name}")
    print(f"Price: {gem.price}")
    print(f"Category: {gem.category}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="gem-price",
        description="Universal Price Comparison - Compare GeM product prices across marketplaces"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level"
    )
    
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Compare command
    compare_parser = subparsers.add_parser("compare", help="Compare GeM product prices")
    compare_parser.add_argument("gem_url", help="GeM product URL")
    compare_parser.add_argument("--top-k", type=int, default=10, help="Top K results")
    compare_parser.add_argument("--threshold", type=float, default=0.9, help="Match threshold")
    compare_parser.add_argument("--marketplaces", nargs="+", help="Marketplaces to search")
    
    # Scrape command
    scrape_parser = subparsers.add_parser("scrape", help="Scrape a marketplace")
    scrape_parser.add_argument("marketplace", choices=["gem", "amazon", "flipkart"])
    scrape_parser.add_argument("query", help="Search query")
    scrape_parser.add_argument("--max-results", type=int, default=10)
    
    # Identity command
    identity_parser = subparsers.add_parser("identity", help="Extract product identity from GeM URL")
    identity_parser.add_argument("gem_url", help="GeM product URL")
    
    # Test command
    test_parser = subparsers.add_parser("test-gem", help="Test GeM scraping")
    test_parser.add_argument("url", help="GeM URL to test")
    
    args = parser.parse_args()
    
    # Set log level
    logging.getLogger().setLevel(args.log_level)
    
    # Run async command
    if args.command == "compare":
        return asyncio.run(compare_command(args))
    elif args.command == "scrape":
        return asyncio.run(scrape_command(args))
    elif args.command == "identity":
        return asyncio.run(identity_command(args))
    elif args.command == "test-gem":
        return asyncio.run(test_gem_command(args))
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    import logging
    sys.exit(main())