"""Entry point so that ``python -m cubemx1`` works."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
