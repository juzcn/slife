"""Allow running as: python -m Slife [--headless] [--agent <id>] [--lang en|zh]"""

import sys

from slife import main


def _has_headless_flag(argv: list[str]) -> bool:
    """Check if --headless is present in argv."""
    return "--headless" in argv[1:]


if __name__ == "__main__":
    if _has_headless_flag(sys.argv):
        # Headless mode: run without TUI (for subagent processes).
        # Pass the FULL argv (program name included) — the headless CLI
        # scanner (parse_cli_config_path) slices argv[1:] itself, so a
        # pre-stripped list would double-strip a positional config path.
        from slife.subagent.headless import main as headless_main

        headless_argv = [a for a in sys.argv if a != "--headless"]
        headless_main(headless_argv)
    else:
        main()
