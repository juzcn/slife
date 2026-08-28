"""Entry point for ``python -m local_embed`` — delegates to the CLI."""

import sys

from local_embed.cli import main

if __name__ == "__main__":
    sys.exit(main())
