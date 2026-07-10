"""slife — Silicon-based life based on LLM.

A terminal-based AI agent with extensible tool system and multi-model support.
Config: slife.json5 (JSON with comments, OpenClaw-style).

Usage:
    uv run python -m slife                # uses slife.json5
    uv run python -m slife myconf.json5   # uses a specific config
"""

import logging
import os
from datetime import datetime
from pathlib import Path

from slife.config import Config
from slife.ui.app import SlifeApp

logger = logging.getLogger("slife")

LOG_DIR = Path("logs")


def _session_log_path() -> Path:
    """Generate a timestamped log file path for this session."""
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return LOG_DIR / f"slife_{ts}.log"


def setup_logging(level: int = logging.DEBUG) -> tuple[Path, logging.Handler]:
    """Configure logging to both console and file.

    Console: INFO+ during startup (before TUI), WARNING+ during TUI runtime.
    File:    DEBUG+ with timestamps for troubleshooting.
    Each session writes to a new logs/slife_YYYYMMDD_HHMMSS.log file.

    Returns:
        (log_path, console_handler) — caller should raise console to WARNING
        before starting the TUI to prevent display corruption.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console handler — INFO during startup, caller raises to WARNING before TUI
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console)

    # File handler — detailed format with timestamps, one file per session
    log_path = _session_log_path()
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)-7s] %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(file_handler)

    return log_path, console


def main(config_path: str = "slife.json5"):
    """Entry point for the slife TUI application."""
    log_path, console_handler = setup_logging()

    logger.info("Log: %s", log_path)
    logger.info("Loading config…")
    config = Config.from_json5(config_path)

    # Apply env vars from config to the process environment
    if config.env:
        for key, value in config.env.items():
            os.environ[key] = str(value)
            logger.info("Env: %s = %s", key, value)

    active = config.active_model
    logger.info("Model: %s (%s)", active.ref, active.display_name)
    logger.info("Thinking: %s", "on" if active.thinking_enabled else "off")
    logger.info("Tools: %d loaded", len(config.tools))

    # Suppress console logging during TUI runtime to prevent display corruption.
    # All messages still go to the per-session log file at DEBUG level.
    console_handler.setLevel(logging.WARNING)

    logger.info("Starting TUI…")

    app = SlifeApp(config)
    app.run()
