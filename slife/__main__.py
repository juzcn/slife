"""Allow running as: ``python -m slife [--headless] [--agent <id>] [--lang en|zh]``.

A thin router: ``--headless`` selects the worker process, anything else the
TUI.  Both flags live in the shared scanners (``slife.config``) so the console
script ``slife``, this module and ``--help`` cannot disagree about them.
"""

import sys

from slife import main
from slife.config import parse_cli_headless

if __name__ == "__main__":
    if parse_cli_headless(sys.argv):
        # Headless mode: run without TUI (for subagent processes).
        # Pass the FULL argv (program name included) — the CLI scanners slice
        # argv[1:] themselves, so a pre-stripped list would double-strip a
        # positional config path.
        from slife.subagent.headless import main as headless_main

        headless_main([a for a in sys.argv if a != "--headless"])
    else:
        main()
