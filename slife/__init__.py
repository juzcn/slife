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
from slife.paths import get_data_dir, get_config_path
from slife.ui.app import SlifeApp

logger = logging.getLogger("slife")


def _check_external_deps() -> None:
    """Check that optional external tools are available.

    Reports status via the health system so ``system_health`` can
    surface missing tools to the LLM / user.  Does NOT attempt to
    install anything — the one-click install scripts handle that.
    """
    import shutil as _shutil
    import subprocess as _sp

    from slife.health import record as _record

    # ── Node.js / npm (used by readabilipy for article extraction) ──
    node_path = _shutil.which("node")
    npm_path = _shutil.which("npm")

    if node_path:
        try:
            r = _sp.run(["node", "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                _record("node", "ok", key="version", value=r.stdout.strip(),
                        hint="Node.js found — fetch MCP can use Readability.js for article extraction.")
            else:
                _record("node", "warning", key="exit", value=str(r.returncode),
                        hint="node exists but returned non-zero. Fetch MCP falls back to pure-Python extraction.")
        except Exception:
            _record("node", "warning", key="error", value="unexpected error",
                    hint="node check failed. Fetch MCP uses pure-Python extraction.")
    else:
        _record("node", "warning", key="missing", value="not found",
                hint="Node.js not installed. Re-run install script or install manually from https://nodejs.org. Fetch MCP uses pure-Python extraction.")

    if npm_path:
        try:
            r = _sp.run(["cmd", "/c", "npm", "version"], capture_output=True, text=True, timeout=10) if sys.platform == "win32" else _sp.run(["npm", "version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                _record("npm", "ok", key="version", value=(r.stdout.strip().splitlines()[0] if r.stdout else "?").split(":")[-1].strip().strip("'").strip('"').rstrip(","),
                        hint="npm found.")
            else:
                _record("npm", "warning", key="exit", value=str(r.returncode),
                        hint="npm exists but returned non-zero.")
        except Exception:
            _record("npm", "warning", key="error", value="unexpected error",
                    hint="npm check failed.")
    else:
        _record("npm", "warning", key="missing", value="not found",
                hint="npm not installed. Re-run install script or install Node.js from https://nodejs.org.")

    # ── uv / uvx (used to run MCP servers) ──
    uv_path = _shutil.which("uv")
    if uv_path:
        try:
            r = _sp.run(["uv", "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                _record("uv", "ok", key="version", value=r.stdout.strip(),
                        hint="uv found — MCP servers can be spawned via uvx.")
            else:
                _record("uv", "warning", key="exit", value=str(r.returncode),
                        hint="uv exists but returned non-zero.")
        except Exception:
            _record("uv", "warning", key="error", value="unexpected error")
    else:
        _record("uv", "warning", key="missing", value="not found",
                hint="uv not installed. Re-run the install script or install from https://astral.sh.")


def main(config_path: str = "slife.json5"):
    """Entry point for the Slife TUI application.

    Dev mode (detected via pyproject.toml): data files stay in CWD.
    Otherwise: everything lives in ``~/.slife/``.
    """
    agent_id = parse_cli_agent(sys.argv)

    # Resolve data dir BEFORE logging setup so logs go to the right place.
    # Explicit config path → use its parent as data dir.
    # Otherwise → dev: CWD, production: ~/.slife/
    _cp = Path(config_path).expanduser()
    if _cp.is_absolute() or _cp.exists():
        data_dir = str(_cp.parent.resolve())
    else:
        data_dir = str(get_config_path().parent.resolve())
    os.environ["SLIFE_DATA_DIR"] = data_dir
    os.environ["SLIFE_CONFIG_DIR"] = data_dir

    # Generate session ID — shared with MCP subprocess via env var
    sid = init_session_id()
    os.environ["SLIFE_SESSION_ID"] = sid
    os.environ["SLIFE_AGENT_ID"] = agent_id

    log_path, console_handler = setup_logging(agent_id=agent_id)

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

    # Check external tooling availability (best-effort, reports via health system)
    _check_external_deps()

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
