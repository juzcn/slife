"""Entry point for `python -m mcp_plugin` and the `mcp-plugin` console script."""

import sys

from mcp_plugin.cli import main

if __name__ == "__main__":
    sys.exit(main())