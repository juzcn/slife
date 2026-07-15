"""Slife — Silicon-based life based on LLM.

A terminal-based AI agent with extensible tool system and multi-model support.
Config: slife.json5 (JSON with comments, OpenClaw-style).

Usage:
    uv run python -m slife                # uses slife.json5
    uv run python -m slife myconf.json5   # uses a specific config
"""

import logging
import os

from slife.bootstrap import setup_logging
from slife.config import Config, parse_cli_agent, parse_cli_user
from slife.logfmt import init_session_id
from slife.ui.app import SlifeApp

logger = logging.getLogger("slife")


def main(config_path: str = "slife.json5", agent_name: str | None = None):
    """Entry point for the Slife TUI application.

    Args:
        config_path: Path to a slife.json5 configuration file.
        agent_name: If provided, enables A2A and sets the agent identity.
                    Equivalent to ``--agent`` on the CLI.
    """
    # Parse --agent and --user from sys.argv when called via setuptools entry point
    import sys as _sys
    if agent_name is None:
        agent_name = parse_cli_agent(_sys.argv)

    user = parse_cli_user(_sys.argv)

    log_path, console_handler = setup_logging(user=user)

    # Generate session ID — shared with MCP subprocess via env var
    sid = init_session_id()
    os.environ["SLIFE_SESSION_ID"] = sid
    os.environ["SLIFE_USER"] = user

    logger.debug("log_path=%s", log_path)
    logger.debug("config loading…")
    config = Config.from_json5(config_path, agent_name=agent_name, user=user)

    # Log env vars from config (already applied to os.environ by Config.from_json5)
    if config.env:
        for key, value in config.env.items():
            # Mask API key values: only log the key name and first/last chars
            if any(hint in key.upper() for hint in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
                masked = str(value)[:4] + "…" + str(value)[-4:] if len(str(value)) > 8 else "***"
                logger.debug("env %s=%s", key, masked)
            else:
                logger.debug("env %s=%s", key, value)

    active = config.active_model
    logger.debug("model=%s provider=%s", active.ref, active.display_name)
    logger.debug("thinking=%s", "on" if active.thinking_enabled else "off")
    logger.debug("tools=%d", len(config.tools))

    # Console logging is already at WARNING via setup_logging().
    # All messages still go to the per-session log file at DEBUG level.

    logger.debug("tui starting…")

    app = SlifeApp(config)
    try:
        app.run()
    finally:
        # Ensure child processes are cleaned up even on crash.
        # action_quit() handles the normal path; this is the safety net.
        app.service.kill_child_processes()

    # Session ended — log summary
    usage = app.service.session_usage
    logger.info(
        "session_end tok_p=%s tok_c=%s tok_t=%s",
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
    )
