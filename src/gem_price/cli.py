"""CLI entry point for GeM Price Comparison."""
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gem_price.core.logging import setup_logging
from gem_price.core.config import settings

logger = setup_logging(__name__)


def main():
    """Main CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage: python -m gem_price <command> [args...]")
        print("Commands:")
        print("  scrape-gem        -- Scrape GeM catalog")
        print("  scrape-flipkart   -- Scrape Flipkart catalog")
        print("  per-product-lookup -- Per-product Flipkart lookup")
        print("  match-engine      -- Run matching engine")
        print("  per-product-match -- Per-product matching")
        print("  load-data         -- Load CSVs into DB")
        print("  api               -- Start API server")
        return 1
    
    command = sys.argv[1]
    args = sys.argv[2:]
    
    if command == "scrape-gem":
        from gem_price.scrapers.gem import main as gem_main
        sys.argv = [sys.argv[0]] + args
        return gem_main()
    elif command == "scrape-flipkart":
        from gem_price.scrapers.flipkart import main as fk_main
        sys.argv = [sys.argv[0]] + args
        return fk_main()
    elif command == "per-product-lookup":
        from gem_price.scrapers.per_product_lookup import main as ppl_main
        sys.argv = [sys.argv[0]] + args
        return ppl_main()
    elif command == "match-engine":
        from gem_price.matching.engine import main as me_main
        sys.argv = [sys.argv[0]] + args
        return me_main()
    elif command == "load-data":
        from gem_price.db.load_data import main as ld_main
        sys.argv = [sys.argv[0]] + args
        return ld_main()
    elif command == "api":
        import uvicorn
        uvicorn.run("gem_price.api.main:app", host=settings.api_host, port=settings.api_port, reload=False)
        return 0
    else:
        print(f"Unknown command: {command}")
        return 1


if __name__ == "__main__":
    sys.exit(main())