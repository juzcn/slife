"""Allow running as: python -m slife [--headless] [--agent <agent-name>]"""

import sys

from slife import main
from slife.config import parse_cli_agent


def _has_headless_flag(argv: list[str]) -> bool:
    """Check if --headless is present in argv."""
    return "--headless" in argv[1:]


if __name__ == "__main__":
    if _has_headless_flag(sys.argv):
        # Headless mode: run without TUI (for subagent processes)
        from slife.subagent.headless import main as headless_main
        # Filter out --headless flag, pass remaining args to headless parser
        headless_argv = [a for a in sys.argv if a != "--headless"]
        headless_main(headless_argv[1:])
    else:
        main(agent_name=parse_cli_agent(sys.argv))
