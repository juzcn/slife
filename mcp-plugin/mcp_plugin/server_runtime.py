"""mcp_plugin server bootstrap — FastMCP setup, logging, port signal.

A slife-free copy of the subset of ``slife.server_utils`` that a standalone
MCP gateway server needs: per-session logging, port binding, the ready
port signal on stdout, and the ``create_plugin_server`` / ``run_plugin_server``
pair.

Serving contract (same as the Slife plugin contract):
  - The child starts a Streamable HTTP server on an auto-assigned port.
  - ``{"port": N}`` is written to stdout ONCE the app's lifespan completed —
    the signal means "ready to serve MCP", so the parent's first
    ``initialize`` always lands on a ready server.
  - stdout is then closed; all user-facing output goes to stderr.
"""

import atexit
import json
import logging
import os
import socket
import sys
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

from mcp_plugin.logging import (
    FILE_LOG_FORMAT,
    SessionFormatter,
    configure_root_logging,
    resolve_log_dir,
    set_session_id,
    silence_noisy_loggers,
)

logger = logging.getLogger(__name__)

# FastMCP-specific loggers that should also be silenced.
_FASTMCP_NOISE = ("mcp.server.lowlevel.server", "fastmcp")


# ── Logging setup / shutdown ────────────────────────────────────────────


def setup_server_logging(
    service_name: str,
    log_dir: Path | None = None,
) -> Path:
    """Configure shared logging for the server process (stderr + file).

    - Adopts ``SLIFE_SESSION_ID`` and ``SLIFE_AGENT_NAME`` from the parent env.
    - stderr: DEBUG+ with timestamped format (parent captures and relays).
    - File:    DEBUG+ with ``SessionFormatter`` (session/request IDs), one per session.
    - File naming: ``{YYYYMMDD_HHMMSS}_{agent_name}_{service}.log``.
    - Silences httpx/httpcore/… and FastMCP noise.

    Returns the log file path.
    """
    if log_dir is None:
        log_dir = resolve_log_dir()

    _sid = os.environ.get("SLIFE_SESSION_ID", "")
    if _sid:
        set_session_id(_sid)

    _agent_name = os.environ.get("SLIFE_AGENT_NAME", "slife")

    stderr_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"{ts}_{_agent_name}_{service_name}.log"

    configure_root_logging(
        stderr_level=logging.DEBUG,
        stderr_format=stderr_fmt,
        file_path=log_path,
        file_level=logging.DEBUG,
        file_format=SessionFormatter(FILE_LOG_FORMAT),
        clear_existing=True,
    )

    # Silence FastMCP-internal loggers (in addition to the standard set)
    silence_noisy_loggers(extra=_FASTMCP_NOISE)

    # Safety net — close log handlers on normal process exit (atexit does
    # NOT fire on Windows TerminateProcess, but it catches sys.exit and
    # unhandled exceptions).  shutdown_server_logging is idempotent so it
    # is safe if a finally block also calls it.
    atexit.register(shutdown_server_logging)

    # Uncaught startup exceptions → one clean line on stderr (the host
    # relays it as the load-failure reason), full traceback in the log file.
    install_uncaught_exception_cleanup()

    return log_path


_uncaught_cleanup_installed = False


def install_uncaught_exception_cleanup() -> None:
    """Plugin-child safety net: an uncaught exception prints ONE actionable
    line to stderr (what the host relays as the load-failure reason) and the
    full traceback only to the session log file.  Idempotent."""
    global _uncaught_cleanup_installed
    if _uncaught_cleanup_installed:
        return
    def _hook(exc_type, exc, tb) -> None:
        # Full traceback only into the session log FILE handler(s) (devs);
        # stderr gets the single actionable line the host relays.
        try:
            _tb = "".join(traceback.format_exception(exc_type, exc, tb))
        except Exception:
            _tb = str(exc)
        try:
            _record = logging.LogRecord(
                "plugin_fatal", logging.ERROR, "", 0,
                "plugin_fatal: %s", (_tb,), None,
            )
            for _h in list(logging.getLogger().handlers):
                if isinstance(_h, logging.FileHandler):
                    _h.emit(_record)
        except Exception:
            pass
        try:
            print(f"[plugin] {exc_type.__name__}: {exc}", file=sys.stderr)
        except Exception:
            pass

    sys.excepthook = _hook
    _uncaught_cleanup_installed = True


def shutdown_server_logging(extra_logger_names: tuple[str, ...] = ()) -> None:
    """Close and remove all root handlers, releasing Windows file locks.

    Idempotent — safe to call even if ``setup_server_logging`` was never
    called, or to call multiple times.
    """
    _root = logging.getLogger()
    for handler in list(_root.handlers):
        try:
            handler.flush()
            handler.close()
        except Exception:
            pass
    _root.handlers.clear()

    for name in extra_logger_names:
        child = logging.getLogger(name)
        for handler in list(child.handlers):
            try:
                handler.flush()
                handler.close()
            except Exception:
                pass
        child.handlers.clear()


# ── Port binding ──────────────────────────────────────────────────────


def bind_free_port(host: str = "127.0.0.1") -> tuple[socket.socket, int]:
    """Bind a socket to *host*:0 and return ``(socket, port)``.

    The OS assigns a free port.  The returned socket is pre-bound and
    can be passed directly to FastMCP via ``sockets=[sock]`` — no race
    between port discovery and server startup.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    port = sock.getsockname()[1]
    return sock, port


def signal_port(port: int) -> None:
    """Write the port to stdout as a JSON line and close stdout.

    The parent reads this line to discover the dynamically-assigned port,
    then connects via Streamable HTTP.
    """
    line = json.dumps({"port": port}, ensure_ascii=False)
    sys.stdout.buffer.write((line + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    sys.stdout.close()


# Set by ``run_plugin_server`` just before serving; invoked by the wrapped
# lifespan in ``create_plugin_server`` once the app is ready.  Module-level is
# safe: every plugin runs in its own process.
_ready_callback: "Callable[[], None] | None" = None


# ── Plugin factory & runner ────────────────────────────────────────────


def create_plugin_server(
    name: str,
    instructions: str,
    *,
    lifespan: "Callable | None" = None,
) -> tuple:
    """Create a standard MCP plugin FastMCP server with logging.

    ``name`` should be ``"mcp-plugin"`` — drives the logger name
    (``mcp_plugin``) and the log-file suffix (``plugin``).

    Returns:
        ``(mcp, log_path, logger)`` — the FastMCP instance ready for
        ``@mcp.tool`` decoration, the per-session log file path, and
        a configured logger.
    """
    from fastmcp import FastMCP

    # When a host (slife) spawned us, it exports SLIFE_PLUGIN_NAME so the
    # per-session log file is named after the plugin (e.g. "_mcp.log"), not the
    # generic "-plugin" suffix.  Standalone keeps the name-derived suffix.
    service_suffix = os.environ.get("SLIFE_PLUGIN_NAME") or (
        name.split("-", 1)[-1] if "-" in name else name
    )
    logger_name = name.replace("-", "_")

    log_path = setup_server_logging(service_suffix)
    plogger = logging.getLogger(logger_name)

    # Wrap the plugin's lifespan so the port signal is emitted only after the
    # app is ready to serve MCP (the contract: signal = "ready", see
    # ``signal_port``).  The parent connects the moment it reads the signal,
    # so its first ``initialize`` must always succeed — signalling before the
    # lifespan finished raced the handshake and hung the plugin load.
    @asynccontextmanager
    async def _ready_wrapped(app):
        if lifespan is not None:
            async with lifespan(app):
                cb = _ready_callback
                if cb is not None:
                    cb()
                yield
        else:
            cb = _ready_callback
            if cb is not None:
                cb()
            yield

    server = FastMCP(name, instructions=instructions, lifespan=_ready_wrapped)

    return server, log_path, plogger


def run_plugin_server(
    mcp_server,
    *,
    port: int = 0,
    host: str = "127.0.0.1",
    show_banner: bool = False,
    sockets: "list[socket.socket] | None" = None,
) -> None:
    """Start the server on Streamable HTTP transport.

    Blocks until the server shuts down.  The port signal (``{"port": N}``
    on stdout) is emitted by the server's lifespan once the app is ready to
    serve MCP — not here, before startup — so the parent's connection
    handshake always lands on a ready server.
    """
    global _ready_callback

    # Resolve the serving port up front so the ready callback can signal it.
    if sockets:
        port = sockets[0].getsockname()[1]
    elif not port:
        sock, port = bind_free_port()
        sockets = [sock]

    _ready_callback = lambda: signal_port(port)
    try:
        if sockets:
            logger.info("plugin_ready transport=streamable-http sockets=%d",
                        len(sockets))
            mcp_server.run(
                transport="streamable-http", host=host, sockets=sockets,
                show_banner=show_banner,
                json_response=True,
                uvicorn_config={"log_config": None},
            )
        else:
            logger.info("plugin_ready transport=streamable-http port=%s", port)
            mcp_server.run(
                transport="streamable-http", host=host, port=port,
                show_banner=show_banner,
                json_response=True,
                uvicorn_config={"log_config": None},
            )
    finally:
        _ready_callback = None
        logger.info("plugin_shutdown port=%s", port)