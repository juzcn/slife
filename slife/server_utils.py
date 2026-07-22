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
  Call ``setup_server_logging("<_suffix>")`` at module level.  Returns the
  per-session log path.  The harness streams stderr to its own log.

Lazy-init rule (CRITICAL)
  Never call ``asyncio.run()`` — FastMCP's ``mcp.run()`` creates its own
  event loop and ``aiosqlite`` / ``aiohttp`` connections created in a
  prior loop will hang forever.  Instead, initialize resources lazily on
  the first tool call, or use FastMCP's lifespan hooks.

Entry point
  :func:`run_plugin_server(mcp) <.run_plugin_server>` is the single,
  one-line call that starts the server.  It handles port binding, parent
  signalling, and FastMCP startup correctly.

Tool registration
  The harness connects to the plugin via Streamable HTTP, calls
  ``tools/list``, and wraps every tool as an ``MCPProxyTool`` via
  ``slife.mcp.tool_adapter.create_proxy_tools``.  Tools with names in
  ``<server>__<tool>`` format are placed in the LLM's tool registry.

Minimal example
  See :file:`slife/plugins/mcp/server.py` (the simplest built-in plugin)::

      # server.py
      from fastmcp import FastMCP
      from slife.server_utils import setup_server_logging, run_plugin_server

      _log_path = setup_server_logging("_my_plugin")

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

import json
import logging
import os
import socket
import sys
from datetime import datetime
from pathlib import Path

from slife.logfmt import (
    SessionFormatter,
    FILE_LOG_FORMAT,
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
    """Configure shared logging for a server process (stderr + file).

    - Adopts ``SLIFE_SESSION_ID`` and ``SLIFE_AGENT_ID`` from the parent env.
    - stderr: DEBUG+ with plain formatter (parent adds session/request context).
    - File:    DEBUG+ with ``SessionFormatter``, one per session.
    - File name includes *agent_id* to avoid conflicts when multiple agents
      run in the same directory (e.g. ``logs/..._slife_mcp.log``).
    - Silences httpx/httpcore/openai/asyncio and FastMCP noise.

    Returns the log file path.
    """
    from slife.logfmt import configure_root_logging

    if log_dir is None:
        log_dir = resolve_log_dir()

    _sid = os.environ.get("SLIFE_SESSION_ID", "")
    if _sid:
        set_session_id(_sid)

    _agent_id = os.environ.get("SLIFE_AGENT_ID", "slife")

    stderr_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"{ts}_{_agent_id}_{service_name}.log"

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

    return log_path


def shutdown_server_logging(extra_logger_names: tuple[str, ...] = ()) -> None:
    """Close and remove all root handlers, releasing Windows file locks.

    Call this before process exit to ensure the log file can be rotated
    or inspected by the parent process.  Idempotent — safe to call even
    if ``setup_server_logging`` was never called.
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
    dynamically-assigned port before connecting via Streamable HTTP.
    """
    line = json.dumps({"port": port}, ensure_ascii=False)
    sys.stdout.buffer.write((line + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    sys.stdout.close()


# ── Plugin factory & runner ────────────────────────────────────────────


def create_plugin_server(name: str, instructions: str) -> tuple:
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

    Returns:
        ``(mcp, log_path, logger)`` — the FastMCP instance ready for
        ``@mcp.tool`` decoration, the per-session log file path, and
        a configured logger.
    """
    from fastmcp import FastMCP

    # "slife-memory" → suffix="_memory", logger_name="slife_memory"
    service_suffix = "_" + name.split("-", 1)[-1] if "-" in name else "_" + name
    logger_name = name.replace("-", "_")

    log_path = setup_server_logging(service_suffix)
    plogger = logging.getLogger(logger_name)
    server = FastMCP(name, instructions=instructions)

    # ── Patch: keep GET SSE alive across multiple requests ──────────────
    # FastMCP's _run_sse_writer uses ``async with sse_stream_writer``
    # which calls aclose() after each response, tearing down the GET SSE
    # TCP connection.  Subsequent requests have no channel for responses.
    # Also patches close_sse_stream to prevent writer.close().
    try:
        from mcp.server.streamable_http import StreamableHTTPServerTransport as _Mgr  # type: ignore[attr-defined]

        _original_run_sse = _Mgr._run_sse_writer

        async def _patched_run_sse_writer(
            self, request_id, sse_stream_writer,
            request_stream_reader, priming_event,
        ):
            try:
                # Use async with on reader only — NOT the writer.
                # The writer must stay alive so the GET SSE connection
                # persists for future requests.
                async with request_stream_reader:
                    if priming_event is not None:
                        await sse_stream_writer.send(priming_event)
                    async for event_message in request_stream_reader:
                        await sse_stream_writer.send(
                            self._create_event_data(event_message)
                        )
                        if isinstance(
                            event_message.message.root,
                            __import__("mcp.types").JSONRPCResponse,
                        ) or isinstance(
                            event_message.message.root,
                            __import__("mcp.types").JSONRPCError,
                        ):
                            break
            except Exception:
                pass
            finally:
                self._sse_stream_writers.pop(request_id, None)
                await self._clean_up_memory_streams(request_id)
            # Intentionally NO writer close — keeps SSE alive.

        _Mgr._run_sse_writer = _patched_run_sse_writer

        # Also patch close_sse_stream — no writer.close().
        def _patched_close_sse_stream(self, request_id):
            self._sse_stream_writers.pop(request_id, None)
            if request_id in self._request_streams:
                send_stream, receive_stream = self._request_streams.pop(request_id)
                try:
                    send_stream.close()
                except Exception:
                    pass
                try:
                    receive_stream.close()
                except Exception:
                    pass

        _Mgr.close_sse_stream = _patched_close_sse_stream
        plogger.debug("streamable_http patch applied — SSE stays alive")
    except Exception:
        plogger.debug("streamable_http patch skipped")

    return server, log_path, plogger


def run_plugin_server(
    mcp_server,
    *,
    port: int = 0,
    host: str = "127.0.0.1",
    show_banner: bool = False,
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

    This call blocks until the server shuts down.  Set up any module-level
    global state (e.g. ``_db_path``) BEFORE calling.
    """
    if port:
        logger.info("plugin_ready transport=streamable-http port=%s", port)
        mcp_server.run(
            transport="streamable-http", host=host, port=port,
            show_banner=show_banner,
            uvicorn_config={"log_config": None},
        )
    else:
        sock, port = bind_free_port()
        logger.info("plugin_ready transport=streamable-http port=%s", port)
        signal_port(port)
        mcp_server.run(
            transport="streamable-http", host=host, port=port, sockets=[sock],
            show_banner=show_banner,
            uvicorn_config={"log_config": None},
        )


# ── JSON response helpers (re-exported from logfmt) ────────────────────
# These were moved to slife.logfmt for better cohesion.
# Kept here for backward compatibility.

from slife.logfmt import ok_json, error_json  # noqa: F401
