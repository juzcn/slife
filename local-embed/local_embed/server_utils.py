"""Standalone plugin-server utilities — the port-signal + FastMCP run loop.

local-embed must not import slife, so this is a minimal copy of the two
pieces of slife's ``server_utils`` that a spawned plugin needs:

1. **Port signal** — the host's ``MCPWrapperProcess`` discovers the port by
   reading a single ``{"port": N}`` JSON line from the child's stdout
   (30 s bound), then closes stdout.  We emit it ONLY after the FastMCP
   app is ready to serve (its lifespan completed) — the contract: the
   signal means "ready to serve MCP on this port", so the host's first
   ``initialize`` always lands on a ready server.

2. **Port binding** — bind a free port on 127.0.0.1 with a pre-bound socket
   so there is no race between discovery and server startup.
"""

from __future__ import annotations

import json
import socket
import sys
from typing import Any


def bind_free_port(host: str = "127.0.0.1") -> "tuple[socket.socket, int]":
    """Bind a socket to *host*:0 and return ``(socket, port)``.

    The OS assigns a free port; the pre-bound socket is passed straight to
    FastMCP via ``sockets=[sock]`` — no race between port discovery and
    server startup.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    port = sock.getsockname()[1]
    return sock, port


def bind_port(host: str, port: int) -> "tuple[socket.socket, int]":
    """Bind a socket to *host*:*port*; fall back to a free port on failure.

    Returns ``(socket, actual_port)`` — when the requested port is taken,
    the OS assigns a free one so the server still starts (the caller
    reports the actual port via the stdout signal).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        return bind_free_port(host)
    return sock, port


def signal_port(port: int) -> None:
    """Write ``{"port": N}`` to stdout as a JSON line and close stdout.

    The host's ``MCPWrapperProcess`` reads this single line to discover the
    dynamically-assigned port, then connects via Streamable HTTP.
    """
    line = json.dumps({"port": port}, ensure_ascii=False)
    sys.stdout.buffer.write((line + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    sys.stdout.close()


#: Set by ``run_plugin_server`` just before serving; invoked once the app is
#: ready (the wrapped lifespan in ``create_plugin_server``).  Module-level is
#: safe — every plugin runs in its own process.
_ready_callback: "Any | None" = None


def run_plugin_server(
    mcp_server: Any,
    *,
    port: int = 0,
    host: str = "127.0.0.1",
    show_banner: bool = False,
    sockets: "list[socket.socket] | None" = None,
) -> int:
    """Start a FastMCP server on Streamable HTTP and block until shutdown.

    Binds the port up front, emits the ready port signal after the app's
    lifespan completes, then serves.  Returns the process exit code.
    """
    import logging

    logger = logging.getLogger(__name__)

    global _ready_callback

    if sockets:
        port = sockets[0].getsockname()[1]
    elif not port:
        sock, port = bind_free_port()
        sockets = [sock]

    _ready_callback = lambda: signal_port(port)
    try:
        logger.info("plugin_ready transport=streamable-http port=%s", port)
        mcp_server.run(
            transport="streamable-http",
            host=host,
            sockets=sockets,
            show_banner=show_banner,
            json_response=True,
            uvicorn_config={"log_config": None},
        )
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error("server_error err=%s", e)
        return 1
    finally:
        _ready_callback = None
    return 0


def create_plugin_server(name: str, instructions: str, *, lifespan=None) -> "tuple[Any, Any]":
    """Create a FastMCP server that signals readiness after its lifespan.

    Mirrors slife's ``create_plugin_server`` (without the slife logging):
    wraps *lifespan* so the port signal fires only after the app is ready
    to serve MCP — signalling early (before the lifespan finished) races
    the host's first ``initialize`` and hangs the plugin load.
    """
    from contextlib import asynccontextmanager

    from fastmcp import FastMCP

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
    return server, None
