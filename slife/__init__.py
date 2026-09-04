"""Slife — Silicon-based life based on LLM.

A terminal-based AI agent with extensible tool system and multi-model support.
Config: ~/.slife/slife.json5 (JSON with comments).

Usage:
    uv run python -m slife                # dev: CWD, prod: ~/.slife/
    uv run python -m slife myconf.json5   # uses a specific config
"""

import logging
import os
import signal
import sys
from pathlib import Path

from slife.bootstrap import (
    restore_windows_console,
    seed_skills,
    setup_logging,
)
from slife.config import Config, parse_cli_agent, parse_cli_config_path, parse_cli_lang
from slife.logfmt import init_session_id
from slife.paths import get_config_path, get_data_dir, get_skills_dir
from slife.ui.app import SlifeApp
from slife.ui.i18n import set_language

logger = logging.getLogger("slife")


def main(config_path: str | None = None):
    """Entry point for the Slife TUI application.

    Dev mode (detected via pyproject.toml): data files stay in CWD.
    Otherwise: everything lives in ``~/.slife/``.  An explicit config path
    (positional CLI arg or the *config_path* parameter) is honored — its
    parent directory becomes the data dir.  ``--lang en|zh`` overrides the
    TUI language; without it the OS locale is detected at import.
    """
    agent_name = parse_cli_agent(sys.argv)
    explicit = config_path or parse_cli_config_path(sys.argv)
    lang = parse_cli_lang(sys.argv)
    if lang is not None:
        set_language(lang)

    # Resolve data dir BEFORE logging setup so logs go to the right place.
    # Only two modes:
    #   1. Dev (pyproject.toml in CWD): everything in CWD
    #   2. Production: everything in ~/.slife/
    # Unless the user passes an explicit config path — then use its parent.
    if explicit:
        _cp = Path(explicit).expanduser()
        if not _cp.is_absolute():
            _cp = Path.cwd() / _cp
        data_dir = str(_cp.parent.resolve())
    else:
        data_dir = str(get_data_dir())
        _cp = get_config_path()  # resolve to ~/.slife/slife.json5 or CWD/slife.json5
    os.environ["SLIFE_DATA_DIR"] = data_dir
    os.environ["SLIFE_CONFIG_DIR"] = data_dir
    # Log directory — inherited by plugin children (internal AND external) so
    # their per-session logs land next to the main session log, regardless of
    # whether the plugin can import slife.  External plugins (local-embed,
    # mcp_plugin) read this instead of their standalone default.
    os.environ["SLIFE_LOG_DIR"] = str(Path(data_dir) / "logs")

    # Seed skills from the installed package to the data directory on
    # first run, so users can edit and add their own skills.
    seed_skills(get_skills_dir())

    # Generate session ID — shared with MCP subprocess via env var
    sid = init_session_id()
    os.environ["SLIFE_SESSION_ID"] = sid
    os.environ["SLIFE_AGENT_NAME"] = agent_name

    # Force UTF-8 encoding for Python subprocesses on Windows.
    # Without this, Python defaults to the system code page (e.g. GBK / cp936)
    # and crashes when printing characters outside that encoding to stdout.
    if sys.platform == "win32":
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    log_path, _ = setup_logging(agent_name=agent_name)

    logger.debug("log_path=%s", log_path)
    logger.debug("data_dir=%s", data_dir)
    from slife.logfmt import elapsed as _elapsed

    logger.debug("config loading…")
    with _elapsed("config_load", logger, level=logging.DEBUG, path=str(_cp)):
        try:
            config = Config.from_json5(str(_cp), agent_name=agent_name)
        except Exception as exc:
            # Terminal belongs to the user — one actionable line, never a
            # traceback.  Full exception details stay in the session log.
            logger.exception("config_load_failed path=%s", _cp)
            print(f"Config error: {exc}", file=sys.stderr)
            print(f"Config: {_cp}  Log: {log_path}", file=sys.stderr)
            raise SystemExit(1)
    from slife.health import record
    _mcp_servers = 0
    try:
        from mcp_plugin import config as _mcp_cfg
        _mcp_servers = _mcp_cfg.count_servers()
    except Exception:
        pass
    record(
        "config", "ok",
        key="path", value=str(_cp),
        hint=f"Config loaded: {len(config.models)} models, "
             f"{_mcp_servers} MCP servers, "
             f"embeddings={'enabled' if (config.embeddings_config and config.embeddings_config.enabled and config.embeddings_config.active_model) else 'disabled'}.",
    )

    # Check external tooling availability (best-effort, reports via health system)
    from slife.health import check_external_deps
    check_external_deps()

    # Log env vars from config (already applied to os.environ by Config.from_json5).
    # Every value goes through the shared sanitizer first — this catches
    # connection strings (DATABASE_URL=postgres://user:pass@host/db) whose
    # password is embedded in the value, and known key shapes.  The key-name
    # heuristic is a fallback for credential-named keys whose value matched no
    # known shape (short secret, arbitrary token).
    if config.env:
        from slife.logfmt import sanitize_secrets
        for key, value in config.env.items():
            s = sanitize_secrets(str(value))
            if s == str(value) and any(
                hint in key.upper() for hint in ("KEY", "SECRET", "TOKEN", "PASSWORD")
            ):
                masked = str(value)[:4] + "…" + str(value)[-4:] if len(str(value)) > 8 else "***"
                s = masked
            logger.debug("env %s=%s", key, s)

    active = config.active_model
    logger.debug("model=%s provider=%s", active.ref, active.display_name)
    logger.debug("thinking=%s", "on" if active.thinking_enabled else "off")
    logger.debug("tools=%d", len(config.tools))
    record(
        "model", "ok",
        key="active", value=active.ref,
        hint=f"Model: {active.ref}, "
             f"thinking={'on' if active.thinking_enabled else 'off'}, "
             f"context={active.context_window}.",
    )

    # Logs never reach the terminal: setup_logging() runs the console stderr
    # handler at CRITICAL+1 (a no-op), so all diagnostics go to the per-session
    # log file at true level, and the terminal belongs entirely to the TUI.
    # User-visible status is surfaced there by the business layer.

    logger.debug("tui starting…")

    app = SlifeApp(config)
    try:
        app.run()
    except KeyboardInterrupt:
        # Ctrl+C pressed during startup or outside the TUI — exit quietly.
        # The TUI's own ctrl+c binding handles the normal case via action_quit.
        pass
    finally:
        # Mask SIGINT FIRST — before any teardown work.  A Ctrl+C that
        # lands while the app is shutting down (plugin stops, subprocess
        # kills, interpreter GC of the Textual widget graph) would leave
        # the SIGINT flag set past main()'s return; the pending
        # KeyboardInterrupt is then raised by CPython during finalization
        # inside a weakref callback (Textual keeps DOMNodes and timers in
        # ``WeakSet``s) and printed as a noisy
        #   "Exception ignored in: <function WeakSet._remove> KeyboardInterrupt"
        # just as the shell prompt returns.  With SIGINT ignored the event
        # is dropped silently.  The in-app ctrl+c binding (action_quit)
        # already handled the normal exit; this only covers late re-presses.
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except (ValueError, OSError):
            pass

        # Restore console mode on Windows — Textual's driver sets
        # ENABLE_VIRTUAL_TERMINAL_INPUT and clears line-editing flags.
        # If the driver's stop_application_mode() doesn't run (crash,
        # anyio task-group interference, etc.), the terminal is left
        # in raw mode (arrow keys showing ^[[A).  This is the safety net.
        if sys.platform == "win32":
            restore_windows_console()
        # Ensure child processes are cleaned up even on crash.
        app.service.kill_child_processes()

        # A fatal startup failure (broken memory DB, failed required plugin)
        # must never be silent: the TUI has now torn down its alternate
        # screen, so the message stored by _fatal_exit can finally reach the
        # terminal, and the shell sees a non-zero exit code.  Only a real
        # string counts (tests use MagicMock for SlifeApp, whose auto-created
        # attributes would otherwise look truthy here).
        fatal = getattr(app, "_fatal_message", None)
        if isinstance(fatal, str) and fatal:
            print(f"\n{fatal}", file=sys.stderr)
            raise SystemExit(1)

    # Session ended — log summary
    usage = app.service.session_usage
    logger.info(
        "session_end tok_p=%s tok_c=%s tok_t=%s",
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
    )
