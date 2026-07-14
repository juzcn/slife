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
from slife.logfmt import init_session_id, SessionFormatter, FILE_LOG_FORMAT
from slife.ui.app import SlifeApp

logger = logging.getLogger("slife")

LOG_DIR = Path("logs")


def _session_log_path(agent_name: str | None = None) -> Path:
    """Generate a timestamped log file path for this session.

    Follows the same naming convention as sub-agent logs:
    ``logs/slife_<name>_YYYYMMDD_HHMMSS.log``.
    """
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = agent_name or "main"
    return LOG_DIR / f"slife_{name}_{ts}.log"


def setup_logging(
    agent_name: str | None = None,
    level: int = logging.DEBUG,
) -> tuple[Path, logging.Handler]:
    """Configure logging to both console and file.

    Console: INFO+ during startup (before TUI), WARNING+ during TUI runtime.
    File:    DEBUG+ with timestamps, session/request IDs for troubleshooting.
    Each session writes to a new ``logs/slife_<name>_YYYYMMDD_HHMMSS.log`` file.

    Returns:
        (log_path, console_handler) — caller should raise console to WARNING
        before starting the TUI to prevent display corruption.
    """
    root = logging.getLogger()

    # Dedup: skip if handlers already set up (e.g. tests calling main() repeatedly)
    if root.handlers:
        # Find the first StreamHandler that writes to stderr
        console = next(
            (h for h in root.handlers if isinstance(h, logging.StreamHandler)
             and getattr(h, 'stream', None) is not None),
            None
        )
        if console is not None:
            return _session_log_path(agent_name), console

    root.setLevel(logging.DEBUG)

    # Console handler — INFO during startup, caller raises to WARNING before TUI
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console)

    # File handler — detailed format with session/request IDs, one per session
    log_path = _session_log_path(agent_name)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(SessionFormatter(FILE_LOG_FORMAT))
    root.addHandler(file_handler)

    # Silence noisy third-party HTTP/logging libraries.
    # These dump full request/response bodies at DEBUG — thousands of
    # characters per API call. slife's own DEBUG output is sufficient.
    for noisy in (
        "openai._base_client",
        "httpcore.connection",
        "httpcore.http11",
        "httpcore.proxy",
        "httpcore._synchronization",
        "httpx",
        "asyncio",
        "urllib3",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return log_path, console


def _parse_cli_name(argv: list[str]) -> str | None:
    """Extract ``--name <value>`` from CLI args.

    Returns ``None`` when ``--name`` is not provided (A2A stays disabled).
    """
    args = argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--name" and i + 1 < len(args):
            return args[i + 1]
        i += 1
    return None


def main(config_path: str = "slife.json5", agent_name: str | None = None):
    """Entry point for the slife TUI application.

    Args:
        config_path: Path to a slife.json5 configuration file.
        agent_name: If provided, enables A2A and sets the agent identity.
                    Equivalent to ``--name`` on the CLI.
    """
    # Parse --name from sys.argv when called via setuptools entry point
    import sys as _sys
    if agent_name is None:
        agent_name = _parse_cli_name(_sys.argv)

    log_path, console_handler = setup_logging(agent_name=agent_name)

    # Generate session ID — shared with MCP subprocess via env var
    sid = init_session_id()
    os.environ["SLIFE_SESSION_ID"] = sid

    logger.debug("log_path=%s", log_path)
    logger.debug("config loading…")
    config = Config.from_json5(config_path, agent_name=agent_name)

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

    # Suppress console logging during TUI runtime to prevent display corruption.
    # All messages still go to the per-session log file at DEBUG level.
    console_handler.setLevel(logging.WARNING)

    logger.debug("tui starting…")

    app = SlifeApp(config)
    app.run()

    # Session ended — log summary
    usage = app.service.session_usage
    logger.info(
        "session_end tok_p=%s tok_c=%s tok_t=%s",
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
    )
