"""Package entry point for `python -m gem_price`."""
from gem_price.cli import main

if __name__ == "__main__":
    import sys
    sys.exit(main())