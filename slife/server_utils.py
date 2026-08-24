"""Slife plugin server specification & shared utilities.

═══════════════════════════════════════════════════════════════════════
Plugin Contract (third-party plugins MUST follow this)
═══════════════════════════════════════════════════════════════════════

File
  ``slife/plugins/<name>/server.py`` — a single module with a ``main()``
  entry point.  The harness spawns it via::

      python -m slife.plugins.<name>.server

FastMCP instance
  A module-level ``mcp = FastMCP("<name>", instructions="…")`` instance.
  All tools are decorated with ``@mcp.tool(name="…")``.

Logging
  Call ``setup_server_logging("<suffix>")`` at module level.  Returns the
  per-session log path.  The harness streams stderr to its own log.

Lazy-init rule (CRITICAL)
  Never call ``asyncio.run()`` — FastMCP's ``mcp.run()`` creates its own
  event loop and ``aiosqlite`` / ``aiohttp`` connections created in a
  prior loop will hang forever.  Instead, initialize resources lazily on
  the first tool call, or use FastMCP's lifespan hooks.

Entry point
  :func:`run_plugin_server(mcp) <.run_plugin_server>` is the single,
  one-line call that starts the server.  It handles port binding, FastMCP
  startup, and — after the app is ready (its lifespan completed) — the
  parent port signal.  The signal means *"ready to serve MCP"*, so the
  harness's first ``initialize`` always lands on a ready server (see
  :func:`signal_port`).

Tool registration
  The harness connects to the plugin via Streamable HTTP, calls
  ``tools/list``, and wraps every tool as an ``MCPProxyTool`` via
  ``slife.mcp.tool_adapter.create_proxy_tools``.  Tools with names in
  ``<server>__<tool>`` format are placed in the LLM's tool registry.

Internal tools
  A tool whose name starts with ``__`` (e.g. ``__memory_save_turn``) is an
  *internal tool* — the plugin contract's marker for "not exposed to the
  LLM".  Both registration paths (generic spawn and subagent connect)
  filter these out by the ``__`` prefix (see :func:`is_internal_tool`), so
  they never reach the LLM's tool registry.  The main process calls them
  programmatically via ``client.call_tool()``.  This is distinct from the
  harness concept: a single ``_`` prefix (e.g. the native ``_sys_note``)
  means *harness* — LLM-visible-but-reserved, auto-invoked by the agent
  loop.  The plugin description text is not a filter key — the ``__``
  prefix is canonical.

Non-MCP endpoints on an MCP plugin
  A plugin may serve plain HTTP endpoints *in addition to* MCP tools on
  the same port — register them with ``@mcp.custom_route(path, methods=...)``
  and FastMCP mounts them on the same uvicorn app as the Streamable HTTP
  endpoint.  ``sharefile`` does exactly this: the MCP tool ``share_file``
  plus ``GET /share/{file_id}`` for serving the actual file bytes — one port,
  two protocols.  Such a plugin binds its
  own port in ``main()`` (when it must know the port, e.g. to point the
  ngrok tunnel at it) and passes the pre-bound socket to
  :func:`run_plugin_server(mcp, sockets=[sock])`.

Minimal example
  See :file:`slife/plugins/mcp/server.py` (the simplest built-in plugin)::

      # server.py
      from fastmcp import FastMCP
      from slife.server_utils import setup_server_logging, run_plugin_server

      _log_path = setup_server_logging("my_plugin")

      mcp = FastMCP("slife-my-plugin", instructions="…")

      @mcp.tool(name="my_tool")
      async def my_tool(arg: str = "") -> str:
          return f"Hello {arg}"

      def main():
          run_plugin_server(mcp)

      if __name__ == "__main__":
          main()

Build-time registration
  The harness auto-discovers plugin tools.  No additional wiring needed.

═══════════════════════════════════════════════════════════════════════
Shared utilities
═══════════════════════════════════════════════════════════════════════
"""

import atexit
import json
import logging
import os
import socket
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

from slife.logfmt import (
    SessionFormatter,
    FILE_LOG_FORMAT,
    resolve_log_dir,
    set_session_id,
    silence_noisy_loggers,
)

logger = logging.getLogger(__name__)

# ── Internal-tool marker ──────────────────────────────────────────────────

#: Plugin internal-tool prefix.  ``__``-prefixed MCP tools are internal to
#: the plugin — called programmatically by the main process via
#: ``client.call_tool()``, never exposed to the LLM.  (Single ``_`` =
#: harness, LLM-visible-but-reserved, e.g. the native ``_sys_note``.)
INTERNAL_TOOL_PREFIX = "__"


def is_internal_tool(name: str) -> bool:
    """Return True for plugin internal tools (``__``-prefixed).

    The plugin contract's marker for "not exposed to the LLM": both
    registration paths filter these out by the ``__`` prefix, and the main
    process reaches them via ``client.call_tool()``.  Distinct from harness
    (single ``_``) — see the module docstring.
    """
    return name.startswith(INTERNAL_TOOL_PREFIX)


# FastMCP-specific loggers that should also be silenced.
_FASTMCP_NOISE = ("mcp.server.lowlevel.server", "fastmcp")


# ── Logging setup / shutdown ────────────────────────────────────────────


def setup_server_logging(
    service_name: str,
    log_dir: Path | None = None,
) -> Path:
    """Configure shared logging for a server process (stderr + file).

    - Adopts ``SLIFE_SESSION_ID`` and ``SLIFE_AGENT_NAME`` from the parent env.
    - stderr: DEBUG+ with timestamped format (parent captures and relays).
    - File:    DEBUG+ with ``SessionFormatter`` (session/request IDs), one per session.
    - File naming: ``{YYYYMMDD_HHMMSS}_{agent_name}_{service}.log``
      (e.g. ``logs/20260808_143025_slife_mcp.log``).
    - Silences httpx/httpcore/openai/asyncio and FastMCP noise.

    Returns the log file path.
    """
    from slife.logfmt import configure_root_logging

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

    return log_path


def shutdown_server_logging(extra_logger_names: tuple[str, ...] = ()) -> None:
    """Close and remove all root handlers, releasing Windows file locks.

    Call this before process exit to ensure the log file can be rotated
    or inspected by the parent process.  Idempotent — safe to call even
    if ``setup_server_logging`` was never called, or to call multiple times
    (e.g. from both a finally block and an atexit handler).
    """
    _root = logging.getLogger()
    for handler in list(_root.handlers):
        try:
            handler.flush()
            handler.close()
        except Exception:
            pass
    _root.handlers.clear()

    # Also silence any named loggers whose handlers weren't on root
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

    The parent ``MCPWrapperProcess`` reads this line to discover the
    dynamically-assigned port, then connects via Streamable HTTP.

    Per the plugin loading contract, the signal is emitted only AFTER the
    MCP application is ready to serve (its lifespan has completed) — so the
    parent's first ``initialize`` handshake always succeeds.  The signal
    means *"ready to serve MCP on this port"*, not just *"port allocated"*.
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
    """Create a standard Slife plugin FastMCP server with logging.

    A single call replaces the per-plugin boilerplate of
    ``setup_server_logging`` + ``logging.getLogger`` + ``FastMCP(…)``::

        from slife.server_utils import create_plugin_server, run_plugin_server

        mcp, _log_path, logger = create_plugin_server(
            "slife-my-plugin",
            instructions="My plugin — does X and Y.",
        )

        @mcp.tool(name="my_tool")
        async def my_tool(arg: str = "") -> str:
            return f"Hello {arg}"

        def main():
            run_plugin_server(mcp)

        if __name__ == "__main__":
            main()

    Args:
        name: e.g. ``"slife-mcp"`` — drives the logger name
            (``slife_mcp``) and log-file suffix (``_mcp``).
        instructions: FastMCP server instructions string.
        lifespan: Optional ``@asynccontextmanager`` startup/shutdown hook
            (FastMCP ``lifespan=`` argument).  Runs on the server's event
            loop — enter before serving, exit on shutdown.  Defaults to
            ``None`` (no-op).  The hook may do slow initialization (network,
            DB); the port signal is deferred until it completes.

    Returns:
        ``(mcp, log_path, logger)`` — the FastMCP instance ready for
        ``@mcp.tool`` decoration, the per-session log file path, and
        a configured logger.
    """
    from fastmcp import FastMCP

    # "slife-memdb" → suffix="memdb", logger_name="slife_memdb"
    service_suffix = name.split("-", 1)[-1] if "-" in name else name
    logger_name = name.replace("-", "_")

    log_path = setup_server_logging(service_suffix)
    plogger = logging.getLogger(logger_name)

    # Wrap the plugin's lifespan so the port signal is emitted only after the
    # app is ready to serve MCP (the contract: signal = "ready", see
    # ``signal_port``).  The parent connects the moment it reads the signal,
    # so its first ``initialize`` must always succeed — signalling before the
    # lifespan finished (e.g. ngrok / MQTT startup) raced the handshake and
    # hung the plugin load.
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
    """Start a Slife plugin server on Streamable HTTP transport.

    Handles the port-bind → signal-parent → run boilerplate so every
    plugin can start with a single call:::

        def main():
            run_plugin_server(mcp)

    Args:
        mcp_server: A ``FastMCP`` instance with tools already decorated.
        port: If 0 (default), the OS assigns a free port and the parent
            discovers it via stdout.  Pass a non-zero port for debugging.
        host: Bind address.  Always ``127.0.0.1`` for security — plugins
            are never exposed to the network.
        show_banner: Pass ``True`` only when debugging; FastMCP's ASCII
            art banner is suppressed in normal use.
        sockets: Optional pre-bound sockets to serve on.  A plugin that
            must know its own port *before* serving (e.g. memfiles, which
            owns the ngrok tunnel) binds it itself in ``main()`` and passes
            the socket here to skip re-binding.

    This call blocks until the server shuts down.  Set up any module-level
    global state (e.g. ``_db_path``) BEFORE calling.

    The port signal (``{"port": N}`` on stdout) is emitted by the server's
    lifespan once the app is ready to serve MCP — not here, before startup —
    so the parent's connection handshake always lands on a ready server
    (the plugin loading contract, see ``create_plugin_server`` / ``signal_port``).
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
