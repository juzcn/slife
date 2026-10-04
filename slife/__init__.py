"""Slife — Silicon-based life based on LLM.

A terminal-based AI agent with extensible tool system and multi-model support.
Config: ~/.slife/slife.yaml (JSON with comments).

Usage:
    uv run python -m slife                # dev: CWD, prod: ~/.slife/
    uv run python -m slife myconf.yaml   # uses a specific config

This package ``__init__`` is deliberately **import-light**: nothing beyond
the stdlib is imported here, so ``import slife.config`` (or any
``slife.*``) does not drag in Textual, the agent loop, MQTT/paho, etc.
The TUI entry point :func:`main` imports its heavy dependencies lazily
when actually invoked (F1).
"""

import logging
import signal
import sys
from importlib import import_module

logger = logging.getLogger("slife")


def __getattr__(name: str):
    """Lazily expose the heavy names the app/entry points and tests use.

    ``main`` is a real function here; ``Config``/``SlifeApp`` are imported
    on first access so ``import slife.config`` never pays for the TUI, and
    ``test_main``'s ``patch("slife.Config…")`` / ``patch("slife.SlifeApp")``
    keep resolving to the live classes.  Anything else falls through to a
    submodule import (``slife.paths`` etc. from ``from slife import X``).
    """
    if name == "Config":
        Config = import_module("slife.config").Config
        globals()["Config"] = Config
        return Config
    if name == "SlifeApp":
        SlifeApp = import_module("slife.ui.app").SlifeApp
        globals()["SlifeApp"] = SlifeApp
        return SlifeApp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main(config_path: str | None = None):
    """Entry point for Slife — the TUI by default, the headless agent on request.

    Dev mode (detected via pyproject.toml): data files stay in CWD.
    Otherwise: everything lives in ``~/.slife/``.  An explicit config path
    (positional CLI arg or the *config_path* parameter) is honored — its
    parent directory becomes the data dir.  ``--lang en|zh`` overrides the
    TUI language; without it the OS locale is detected at import.
    ``--headless`` runs the same agent with no terminal attached (see
    :mod:`slife.headless`); every host shares the boot
    (:func:`slife.bootstrap.prepare_session`) and this function's teardown.

    Everything is imported INSIDE the function — the package ``__init__``
    is import-light, so invoking an app is the only act that pays for
    Textual/agent/bootstrap (F1) — and the host branch imports its own
    module, so ``--headless`` never imports Textual at all.
    """
    from slife.config import CLI_USAGE, parse_cli_help

    # The exit happens BEFORE the heavy imports below, so `--help` answers
    # without paying for Textual, the agent loop or the plugins.
    if parse_cli_help(sys.argv):
        print(CLI_USAGE, end="")
        return

    from slife.bootstrap import (
        clear_session_marker,
        prepare_session,
        restore_windows_console,
    )
    from slife.config import (
        parse_cli_agent,
        parse_cli_config_path,
        parse_cli_headless,
        parse_cli_lang,
    )
    from slife.ui.i18n import set_language

    agent_name = parse_cli_agent(sys.argv)
    explicit = config_path or parse_cli_config_path(sys.argv)
    lang = parse_cli_lang(sys.argv)
    if lang is not None:
        set_language(lang)
    headless = parse_cli_headless(sys.argv)

    # Everything a process does before it has an agent — shared with the
    # headless host, so the two can never drift about where the data dir is,
    # which log they write, or whether the marker was written.
    config, log_path = prepare_session(explicit, agent_name)

    # Logs never reach the terminal: setup_logging() runs the console stderr
    # handler at CRITICAL+1 (a no-op), so all diagnostics go to the per-session
    # log file at true level, and the terminal belongs entirely to the host's
    # user surface — the TUI, or nothing at all when headless.  User-visible
    # status is surfaced there by the business layer.

    app = None
    service = None
    fatal: str | None = None
    try:
        if headless:
            logger.debug("headless starting…")
            from slife.headless import run_headless  # lazy — keeps this import-light

            service, fatal = run_headless(config)
        else:
            from slife import SlifeApp  # lazy via __getattr__ — patchable by tests

            logger.debug("tui starting…")
            app = SlifeApp(config)
            service = app.service
            app.run()
    except KeyboardInterrupt:
        # Ctrl+C pressed during startup or outside the host — exit quietly.
        # The TUI's own ctrl+c binding handles the normal case via action_quit;
        # the headless host turns it into its own shutdown.
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
        # Teardown reached — the marker this session wrote at startup is no
        # longer evidence of anything.  (Killed first?  It stays, and the next
        # start reports it: bootstrap.previous_session_killed.)
        clear_session_marker(log_path.parent)
        # Ensure child processes are cleaned up even on crash.  The host's own
        # graceful stop already ran; this is the second layer, for the paths
        # that never reached it.
        if service is not None:
            service.kill_child_processes()

        # A fatal startup failure (broken memory DB, failed required plugin)
        # must never be silent.  The TUI has now torn down its alternate
        # screen, so the message stored by _fatal_exit can finally reach the
        # terminal, and the shell sees a non-zero exit code; the headless host
        # has no screen to tear down and returns its message directly.  Only a
        # real string counts (tests use MagicMock for SlifeApp, whose
        # auto-created attributes would otherwise look truthy here).
        if app is not None:
            fatal = getattr(app, "_fatal_message", None)
        if isinstance(fatal, str) and fatal:
            print(f"\n{fatal}", file=sys.stderr)
            raise SystemExit(1)

    # Session ended — log summary
    if service is not None:
        usage = service.session_usage
        logger.info(
            "session_end tok_p=%s tok_c=%s tok_t=%s",
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
        )
