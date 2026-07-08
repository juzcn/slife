"""slife — Silicon-based life based on LLM.

A terminal-based AI agent with extensible tool system and multi-model support.
Config: slife.json5 (JSON with comments, OpenClaw-style).

Usage:
    uv run main.py                # uses slife.json5
    uv run main.py myconf.json5   # uses a specific config
"""

from slife.config import Config
from slife.ui.app import SlifeApp


def main(config_path: str = "slife.json5"):
    """Entry point for the slife TUI application."""
    print("Loading config...")
    config = Config.from_json5(config_path)

    active = config.active_model
    print(f"Model: {active.ref} ({active.display_name})")
    print(f"Thinking: {'on' if active.thinking_enabled else 'off'}")
    print(f"Tools: {len(config.tools)} loaded")
    print("Starting TUI...")

    app = SlifeApp(config)
    app.run()
