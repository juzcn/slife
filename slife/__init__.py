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

from slife.bootstrap import restore_windows_console, seed_skills, setup_logging
from slife.config import Config, parse_cli_agent
from slife.logfmt import init_session_id
from slife.paths import get_config_path, get_data_dir, get_skills_dir
from slife.ui.app import SlifeApp

logger = logging.getLogger("slife")


def main(config_path: str = "slife.json5"):
    """Entry point for the Slife TUI application.

    Dev mode (detected via pyproject.toml): data files stay in CWD.
    Otherwise: everything lives in ``~/.slife/``.
    """
    agent_id = parse_cli_agent(sys.argv)

    # Resolve data dir BEFORE logging setup so logs go to the right place.
    # Only two modes:
    #   1. Dev (pyproject.toml in CWD): everything in CWD
    #   2. Production: everything in ~/.slife/
    # Unless the user passes an explicit config path — then use its parent.
    _cp = Path(config_path).expanduser()
    if _cp.is_absolute():
        # Explicit path given — use its parent as data dir
        data_dir = str(_cp.parent.resolve())
    else:
        data_dir = str(get_data_dir())
        _cp = get_config_path()  # resolve to ~/.slife/slife.json5 or CWD/slife.json5
    os.environ["SLIFE_DATA_DIR"] = data_dir
    os.environ["SLIFE_CONFIG_DIR"] = data_dir

    # Seed skills from the installed package to the data directory on
    # first run, so users can edit and add their own skills.
    seed_skills(get_skills_dir())

    # Generate session ID — shared with MCP subprocess via env var
    sid = init_session_id()
    os.environ["SLIFE_SESSION_ID"] = sid
    os.environ["SLIFE_AGENT_ID"] = agent_id

    # Force UTF-8 encoding for Python subprocesses on Windows.
    # Without this, Python defaults to the system code page (e.g. GBK / cp936)
    # and crashes when printing characters outside that encoding to stdout.
    if sys.platform == "win32":
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    log_path, _ = setup_logging(agent_id=agent_id)

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
             f"memory={'enabled' if config.memdb_config else 'disabled'}.",
    )

    # Check external tooling availability (best-effort, reports via health system)
    from slife.health import check_external_deps
    check_external_deps()

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
    except KeyboardInterrupt:
        # Ctrl+C pressed during startup or outside the TUI — exit quietly.
        # The TUI's own ctrl+c binding handles the normal case via action_quit.
        pass
    finally:
        # Restore console mode on Windows — Textual's driver sets
        # ENABLE_VIRTUAL_TERMINAL_INPUT and clears line-editing flags.
        # If the driver's stop_application_mode() doesn't run (crash,
        # anyio task-group interference, etc.), the terminal is left
        # in raw mode (arrow keys showing ^[[A).  This is the safety net.
        if sys.platform == "win32":
            restore_windows_console()
        # Ensure child processes are cleaned up even on crash.
        app.service.kill_child_processes()

    # Session ended — log summary
    usage = app.service.session_usage
    logger.info(
        "session_end tok_p=%s tok_c=%s tok_t=%s",
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
    )
