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
  harness's first request always lands on a ready server (see
  :func:`signal_port`).

Readiness (protocol negotiation)
  Readiness is defined by MCP itself: the harness's connect-time era
  negotiation (``server/discover`` on a modern peer, the ``initialize``
  handshake on a legacy one) completes only when the server is up and can
  respond, so a completed negotiation IS the plugin's ready declaration —
  no ``__ready`` tool.  The lifespan must therefore stay **connect-fast**:
  establish only the minimum needed to serve (memdb/memfiles open their
  store) and
  nothing that could stall the loop while the wrapper is connecting — a
  GIL-holding model load, slow I/O, or long connect belongs in
  :func:`warm_after_ready`, never in the lifespan.  A failing lifespan
  before the negotiation reports the plugin FAILED (its watchdog backs off
  and retries); the port signal fires only when the app is ready, so the
  wrapper's first connect exchange always lands on a serving server.
  Every built-in plugin shares this complete lifecycle shape (all declare a
  lifespan).  Dependencies that are NOT required to serve (external MCP
  servers, ngrok tunnel, MQTT broker, login state, media providers) are
  deliberately left out of the lifespan and surfaced separately via status
  tools — they never gate readiness.

Required plugins (core components)
  Whether a plugin is *required* is a **host-side contract decision,
  configured per instance** — named in the ``plugins.required`` list of
  ``slife.yaml`` (default: empty = every plugin optional).  A required
  plugin that fails to become ready — lifespan failure (FAILED), a raised
  spawn, or the harness's bounded 30 s spawn hang-guard — **aborts
  startup**: red message, all plugins stopped, non-zero exit.  The app
  must never run without a core component.  ``memdb`` and ``memfiles``
  are required in the standard configuration because memory is core;
  everything else defaults to non-required (load failure warns, the
  session continues, and the watchdog backs off and retries).

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
  harness concept: a single ``_`` prefix (e.g. the builtin ``_turn_prompt``)
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
  See :file:`slife/plugins/memdb/server.py` (a built-in plugin)::

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

import asyncio
import atexit
import json
import logging
import os
import socket
import sys
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from fastmcp.server.middleware import Middleware
from mcp.shared.subscriptions import ToolsListChanged

from slife.logfmt import (
    SessionFormatter,
    FILE_LOG_FORMAT,
    log_stamp,
    resolve_log_dir,
    set_session_id,
    silence_noisy_loggers,
)
from slife.paths import agent_name
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mcp.server.subscriptions import SubscriptionBus

# ── Internal-tool marker ──────────────────────────────────────────────────

#: Plugin internal-tool prefix.  ``__``-prefixed MCP tools are internal to
#: the plugin — called programmatically by the main process via
#: ``client.call_tool()``, never exposed to the LLM.  (Single ``_`` =
#: harness, LLM-visible-but-reserved, e.g. the builtin ``_turn_prompt``.)
INTERNAL_TOOL_PREFIX = "__"


def is_internal_tool(name: str) -> bool:
    """Return True for plugin internal tools (``__``-prefixed).

    The plugin contract's marker for "not exposed to the LLM": both
    registration paths filter these out by the ``__`` prefix, and the main
    process reaches them via ``client.call_tool()``.  Distinct from harness
    (single ``_``) — see the module docstring.
    """
    return name.startswith(INTERNAL_TOOL_PREFIX)


class ToolsChangedNotifier:
    """Publish ``tools/list_changed`` to this server's listen subscribers.

    At the modern protocol era (2026-07-28) a change notification reaches a
    client ONLY through a ``subscriptions/listen`` stream that the client
    opened: the spec forbids pushing an unrequested notification, and the
    SDK drops a bare ``ServerSession.send_tool_list_changed()`` outright
    (``mcp/server/connection.py`` — "delivered via subscriptions/listen at
    this era").  Publishing on a ``SubscriptionBus`` is the supported path —
    every live listen stream picks the event up, stamped and filtered.

    This replaces the retired session-set notifier, which had to remember
    each caller's request ``session`` and fan out N network sends.  A bus
    publish needs no per-request bookkeeping (it is equally valid from a
    request handler and a background task), so ``capture()`` and the
    ``_active_sessions`` sets are gone with it.  One instance per server
    process.
    """

    def __init__(self, bus: "SubscriptionBus | None" = None) -> None:
        self._bus = bus

    def bind(self, bus: "SubscriptionBus") -> None:
        """Attach the bus to publish on (see :func:`tools_changed_bus`).

        For callers whose server is built after module import (the host
        server builds its ``FastMCP`` inside ``build_registry_mcp`` and
        job-coding builds it at the bottom of its module); a publish before
        :meth:`bind` is a logged no-op.
        """
        self._bus = bus

    def request_tools_changed(self) -> None:
        """Schedule one publish — fire-and-forget, no ordering guarantee.

        Detached on purpose: callers are request handlers and background
        callbacks that must not await a fan-out, and the event is
        payload-less (a listener re-lists on receipt).
        """
        asyncio.create_task(self.publish())

    async def publish(self) -> None:
        """Publish one tools-list-changed event to the listen subscribers.

        Best-effort: the bus fan-out is in-process, and a delivery failure
        belongs to that stream (``ListenHandler`` ends a stream whose client
        stopped reading, and the client re-listens — there is no replay).
        """
        if self._bus is None:
            logger.debug("tools_changed_no_bus — nothing to publish to")
            return
        try:
            await self._bus.publish(ToolsListChanged())
        except Exception:
            logger.debug("tools_changed_publish_failed", exc_info=True)

    async def flush(self) -> None:
        """Eager alias (tests/deterministic paths): publish in this task.

        Kept so fixtures can await the round-trip deterministically; the
        production paths use :meth:`request_tools_changed`.
        """
        await self.publish()


def tools_changed_bus(server) -> "SubscriptionBus":
    """The change-notification bus of *server*, serving ``subscriptions/listen``.

    fastmcp 4.0.1 predates the 2026-07-28 change-notification model: it never
    registers ``subscriptions/listen`` (the SDK's own ``MCPServer`` does), so
    a client opening a listen stream gets JSON-RPC "Method not found" — and
    without a stream a modern client has NO way to learn that a tool list
    changed.  This closes that gap by registering the SDK's ``ListenHandler``
    over a bus we own: the server capability advertisement follows from the
    handler registry, so clients are told about the subscription capability
    exactly when it is actually served.

    Idempotent per server — the bus is stashed on the instance, so a second
    call (or a module-level call plus a lazy rebind) reuses one bus and never
    double-registers the method.
    """
    from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
    from mcp_types import SubscriptionsListenRequestParams

    cached = getattr(server, "_slife_subscriptions", None)
    if cached is not None:
        return cached
    bus = InMemorySubscriptionBus()
    # fastmcp's FastMCP wraps the SDK's low-level Server; the handler registry
    # (and the capabilities derived from it) lives on that inner object.
    inner = getattr(server, "_mcp_server", server)
    try:
        inner.add_request_handler(
            "subscriptions/listen", SubscriptionsListenRequestParams, ListenHandler(bus),
        )
    except Exception:
        logger.debug("listen_handler_attach_failed", exc_info=True)
    setattr(server, "_slife_subscriptions", bus)
    logger.debug("listen_handler_attached server=%s", getattr(server, "name", "?"))
    return bus


def request_tools_changed(notifier: "ToolsChangedNotifier") -> None:
    """Fire-and-forget ``tools/list_changed`` publish via *notifier*.

    The shared body every plugin server's module-level
    ``_request_tools_changed`` used to spell out: schedule a detached
    publish so a slow ``tools/call`` handler never blocks on the fan-out.
    Callers MUST NOT rely on delivery ordering; hosts re-list on receipt.
    """
    notifier.request_tools_changed()


async def flush_tools_changed(notifier: "ToolsChangedNotifier") -> None:
    """Eager-flush via *notifier*: publish in this task, deterministically.

    The deterministic alias tests use (await the full round-trip); production
    paths should fire-and-forget with :func:`request_tools_changed`.
    """
    await notifier.flush()


class _WarmAfterReady(Middleware):
    """Middleware that runs a background warm-up after the first tools/list."""

    def __init__(self, factory: "Callable[[], Awaitable[None]]", delay: float, name: str):
        self._factory = factory
        self._delay = delay
        self._name = name
        self._started = False

    async def on_list_tools(self, context, call_next):
        result = await call_next(context)
        if not self._started:
            self._started = True
            asyncio.get_running_loop().create_task(self._go())
        return result

    async def _go(self) -> None:
        await asyncio.sleep(self._delay)
        try:
            await self._factory()
        except Exception:
            logger.debug("%s_warm_failed", self._name, exc_info=True)


def warm_after_ready(
    mcp,
    factory: "Callable[[], Awaitable[None]]",
    *,
    delay: float | None = None,
    name: str = "warmup",
) -> None:
    """Run a heavyweight coroutine AFTER the first MCP ``tools/list``.

    The plugin contract declares readiness as the connect-time era
    negotiation completing (see the module docstring) — a plugin must be
    connect-fast, so anything that could stall the loop while the
    wrapper is still connecting (a GIL-holding model load, slow I/O, long
    connects) must NOT run in the lifespan.  Use this to warm up such
    resources in the background.

    ``factory`` is awaited once (plus the grace period) on an event-loop task;
    an exception is logged, never fatal.  *delay* defaults to the registry
    cadence ``pacing.warm_delay``.

    The grace period exists so the wrapper's first ``tools/list`` response
    is ALWAYS flushed before the warm-up starts: the first embedding
    config read can cold-import the API SDK (a multi-second synchronous
    import that blocks this loop).  If the warm-up began inside that window
    the response would be delayed by the import and the harness's spawn
    guard could time out a perfectly healthy plugin on a slow machine, so
    the registry default is generous rather than a tight 0.25s.
    """
    if delay is None:
        delay = _timeouts.timeouts.pacing.warm_delay
    mcp.add_middleware(_WarmAfterReady(factory, delay, name))


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
        # resolve_log_dir() → get_logs_dir() prefers SLIFE_LOG_DIR (exported
        # by the main process for plugin children), else <data_dir>/logs.
        log_dir = resolve_log_dir()

    _sid = os.environ.get("SLIFE_SESSION_ID", "")
    if _sid:
        set_session_id(_sid)

    _agent_name = agent_name()

    stderr_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    ts = log_stamp()
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
    full traceback only to the session log file — never a raw stderr dump.

    Installed by ``setup_server_logging`` so every plugin child fails with a
    one-line reason on any uncaught startup/config/bind error.  Idempotent.
    """
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
    parent's first request always finds a serving server.  The signal
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

    # "slife-memdb" → suffix="memdb", logger_name="slife_memdb".  When a host
    # spawned us it exports SLIFE_PLUGIN_NAME (the plugin's key), which wins —
    # e.g. mcp-gateway's name-derived suffix would be "plugin", but the parent
    # names the log after the plugin ("_mcp.log", not "_plugin.log").
    service_suffix = os.environ.get("SLIFE_PLUGIN_NAME") or (
        name.split("-", 1)[-1] if "-" in name else name
    )
    logger_name = name.replace("-", "_")

    log_path = setup_server_logging(service_suffix)
    plogger = logging.getLogger(logger_name)

    # Wrap the plugin's lifespan so the port signal is emitted only after the
    # app is ready to serve MCP (the contract: signal = "ready", see
    # ``signal_port``).  The parent connects the moment it reads the signal,
    # so its first request must always succeed — signalling before the
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
                # SSE mode (NOT a single JSON body per POST): the modern
                # era's change notifications ride a `subscriptions/listen`
                # stream, and a JSON response has nowhere to carry one —
                # the SDK drops them ("removes the request-scoped
                # back-channel").  See ToolsChangedNotifier.
                json_response=False,
                uvicorn_config={"log_config": None},
            )
        else:
            logger.info("plugin_ready transport=streamable-http port=%s", port)
            mcp_server.run(
                transport="streamable-http", host=host, port=port,
                show_banner=show_banner,
                json_response=False,   # see the sockets branch above
                uvicorn_config={"log_config": None},
            )
    finally:
        _ready_callback = None
        logger.info("plugin_shutdown port=%s", port)
