"""slife — Silicon-based life based on LLM.

A terminal-based AI agent with extensible tool system and multi-model support.
Config: slife.json5 (JSON with comments, OpenClaw-style).

Usage:
    uv run python -m slife                # uses slife.json5
    uv run python -m slife myconf.json5   # uses a specific config
"""

import logging

from slife.config import Config
from slife.ui.app import SlifeApp

logger = logging.getLogger("slife")


def main(config_path: str = "slife.json5"):
    """Entry point for the slife TUI application."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    logger.info("Loading config...")
    config = Config.from_json5(config_path)

    active = config.active_model
    logger.info("Model: %s (%s)", active.ref, active.display_name)
    logger.info("Thinking: %s", "on" if active.thinking_enabled else "off")
    logger.info("Tools: %d loaded", len(config.tools))
    logger.info("Starting TUI...")

    app = SlifeApp(config)
    app.run()
