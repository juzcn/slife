"""Slife — Silicon-based life based on LLM.

A terminal-based AI agent with extensible tool system and multi-model support.
Config: ~/.slife/slife.json5 (JSON with comments, OpenClaw-style).

Usage:
    uv run python -m slife                # dev: CWD, prod: ~/.slife/
    uv run python -m slife myconf.json5   # uses a specific config
"""

import logging
import os
import sys
from pathlib import Path

from slife.bootstrap import setup_logging
from slife.config import Config, parse_cli_agent
from slife.logfmt import init_session_id
from slife.ui.app import SlifeApp

logger = logging.getLogger("slife")


def _is_dev() -> bool:
    """Check whether we're running from the slife source tree.

    Reads ``pyproject.toml`` in CWD and checks that ``[project] name``
    equals ``"slife"``.
    """
    import tomllib
    try:
        data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        return data.get("project", {}).get("name") == "slife"
    except Exception:
        return False


def main(config_path: str = "slife.json5"):
    """Entry point for the Slife TUI application.

    Dev mode (detected via pyproject.toml): data files stay in CWD.
    Otherwise: everything lives in ``~/.slife/``.
    """
    agent_id = parse_cli_agent(sys.argv)

    log_path, console_handler = setup_logging(agent_id=agent_id)

    # Resolve config path
    _cp = Path(config_path).expanduser()
    if not _cp.is_absolute() and not _cp.exists():
        if not _is_dev():
            _cp = Path.home() / ".slife" / "slife.json5"
    data_dir = str(_cp.parent.resolve())
    os.environ["SLIFE_DATA_DIR"] = data_dir
    os.environ["SLIFE_CONFIG_DIR"] = data_dir

    # Generate session ID — shared with MCP subprocess via env var
    sid = init_session_id()
    os.environ["SLIFE_SESSION_ID"] = sid
    os.environ["SLIFE_AGENT_ID"] = agent_id

    logger.debug("log_path=%s", log_path)
    logger.debug("data_dir=%s", data_dir)
    from slife.logfmt import elapsed as _elapsed

    logger.debug("config loading…")
    with _elapsed("config_load", logger, level=logging.DEBUG, path=str(_cp)):
        try:
            config = Config.from_json5(str(_cp), agent_id=agent_id)
        except Exception:
            logger.exception("config_load_failed path=%s", config_path)
            raise
    from slife.health import record
    record(
        "config", "ok",
        key="path", value=config_path,
        hint=f"Config loaded: {len(config.models)} models, "
             f"{len(config.mcp_config.servers) if config.mcp_config else 0} MCP servers, "
             f"memory={'enabled' if config.memory_config else 'disabled'}.",
    )

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
    record(
        "model", "ok",
        key="active", value=active.ref,
        hint=f"Model: {active.display_name}, "
             f"thinking={'on' if active.thinking_enabled else 'off'}, "
             f"context={active.context_window}.",
    )

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
