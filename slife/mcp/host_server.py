"""slife-as-plugin — the running agent's live ToolRegistry served over MCP.

DESIGNER_NOTES §8 "turn slife to a plugin": the running slife hosts an
in-process FastMCP server that exposes its **live ToolRegistry**, so any MCP
client (another slife, an external tool runner) can consume slife's tools
like any other plugin.

Tool definition follows the repo's auto-discovery convention (a category per
module under ``slife/tools/``) — there is NO hand-written ``@mcp.tool`` per
tool.  The plugin wraps the registry the factory already built:

- It reuses ``create_tools_from_config`` so the exact same ``Tool`` objects
  the agent loop sees are what an external consumer gets.
- Each wrapped tool carries the slife ``Tool.parameters`` JSON Schema as its
  explicit MCP ``inputSchema`` (a ``Tool.execute(**kwargs)`` has no
  introspectable signature, so FastMCP cannot derive the schema from
  annotations — the schema must be attached explicitly).
- The handler routes every keyword argument through ``registry.execute`` —
  the same normalized execution path the agent loop uses, so errors surface
  as ``"Error: …"`` strings exactly as they do for the agent.

Plugin contract:
- LLM-visible tools keep their bare names.
- Internal (harness-only) tools use the ``__`` prefix (``is_internal_tool``
  filters it, per the plugin spec) — currently just ``__check``.
- Tool-set mutations (register/unregister on the live registry) push the
  standard MCP ``notifications/tools/list_changed`` to connected clients, so
  a consumer's tool list stays live without polling or restarts.

Placement is deliberately outside ``slife.plugins.*``: the server runs **in**
the main process (it must reach the live registry in-process), so it must
never be picked up by ``discover_plugins`` and spawned as a child plugin.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from fastmcp import FastMCP
from fastmcp.server.context import Context

from slife.server_utils import INTERNAL_TOOL_PREFIX, SessionNotifier, bind_free_port
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

if TYPE_CHECKING:
    from slife.tools.catalog_service import ToolCatalogService
    from slife.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

#: Tools excluded from the outward face — harness/context controls that mutate
#: the *host* agent's own state (a foreign client must not reset the host's
#: turns or change its iteration cap).  Everything else in the registry is a
#: consumer-usable capability — including plugin proxy tools, which execute in
#: the owning plugin process just as for the agent itself.
_EXCLUDED_NAMES = frozenset({
    "_turn_prompt",        # harness marker injector — mutates the host context
    "_check_new_input",    # mid-turn input injector — reads the host's queue
    "_model_config_tool",  # internal base class, not a real tool
    "clear_context",       # resets the host's loaded turns
    "set_max_iterations",  # changes the host loop's iteration cap
})


def is_exposed(tool) -> bool:
    """Whether a registry tool is safe to expose to external consumers.

    ``__``-prefixed tools never reach the registry (the factory skips them),
    so the only filter here is the harness/context-control exclusion list plus
    a defensive internal-prefix check.
    """
    name = getattr(tool, "name", "")
    if name in _EXCLUDED_NAMES:
        return False
    return not name.startswith(INTERNAL_TOOL_PREFIX)


#: Connected client sessions to notify on tool-set change.  Captured from the
#: handler's injected ``Context`` — the same pattern job-coding / mcp use.
_active_sessions: set = set()
#: Bound on tracked sessions.  A client that connects and disconnects cleanly
#: (send never raising) would otherwise leak a session entry forever; the
#: bound makes the set bounded and forces a sweep of dead sessions via a
#: failed send.  Real deployments see a handful of consumers.
_MAX_TRACKED_SESSIONS = 64
#: Coalescing fan-out lives in :class:`slife.server_utils.SessionNotifier`;
#: the module keeps the session set + the tested wrapper names below.  The
#: notifier reads the set through a getter, so a fixture rebinding
#: ``_active_sessions`` (``srv._active_sessions = …``) is honored.
_notifier = SessionNotifier(lambda: _active_sessions)


def _capture_session(ctx: Context | None) -> None:
    """Remember the caller's session so a registry change can notify it."""
    _notifier.capture(ctx, max_sessions=_MAX_TRACKED_SESSIONS)


def _request_tools_changed() -> None:
    """Coalesce-and-schedule ``notifications/tools/list_changed`` to all clients.

    Fire-and-forget — the send runs in a DETACHED task (the mcp 2.1.1
    dispatcher cancel-scope rationale lives on :class:`SessionNotifier`).
    Coalescing folds bursts into a single send.  Hosts re-list on receipt,
    so no ordering guarantees are assumed.
    """
    _notifier.request_tools_changed()


async def _notify_tools_changed() -> None:
    """Eager-flush alias kept for tests/…: run one full notification round
    now, in this task (deterministic delivery — no coalescing).  Production
    notification paths should use :func:`_request_tools_changed`."""
    await _notifier.flush()


def build_registry_mcp(
    registry: "ToolRegistry",
    catalog: "ToolCatalogService | None" = None,
    instructions: str = "",
) -> FastMCP:
    """Build the in-process FastMCP server exposing *registry*.

    Returns an unstarted ``FastMCP`` with the current registry synced and a
    ``__check`` internal tool.  Start it with :func:`start_host_server` (an
    asyncio task); it is NOT a child plugin, so ``run_plugin_server`` is
    deliberately not used.  When *catalog* is given (the main agent's shared
    tools.db service), ``__check`` also reports the unified tool catalog's
    live facts — the host-as-plugin is the catalog owner, so its harness
    probe carries the catalog view (tool/server counts + semantic index).
    """
    server = FastMCP(
        "slife",
        instructions=instructions or (
            "slife-as-plugin — the running slife agent's live tool registry. "
            "Consume these tools as you would any MCP server; they execute in "
            "the slife instance that serves this endpoint."
        ),
    )

    @server.tool(
        name="__check",
        description=("slife live facts: exposed tools + unified tool catalog. "
                     "Internal — harness probe, never exposed to the LLM."),
    )
    async def __check(ctx: Context | None = None) -> str:
        _capture_session(ctx)
        exposed = [t.name for t in registry.list_tools() if is_exposed(t)]
        payload = {
            "exposed_count": len(exposed),
            "exposed_tools": sorted(exposed),
        }
        if catalog is not None:
            payload["catalog"] = await _host_catalog_facts(catalog)
        return json.dumps(payload, ensure_ascii=False)

    _sync_registry(server, registry)
    return server


async def _host_catalog_facts(catalog: "ToolCatalogService") -> dict:
    """Live facts for the host-as-plugin ``__check`` catalog block.

    Mirrors the memdb-style semantic surface so a harness/consumer probe can
    tell the unified catalog's readiness from its degradation — counts are raw
    facts, the semantic index reports configured/available/state ready/reason.
    Never raises — a broken catalog reports an ``error`` field, not a crash.
    """
    facts: dict = {}
    try:
        store = catalog.store
        facts["tools"] = len(await store.scan_effective())
        facts["servers"] = len(await store.list_server_names())
        facts["loaded"] = await store.count_loaded()
    except Exception as e:
        facts["error"] = f"catalog unavailable: {e}"
        return facts
    sem = getattr(catalog, "semantic_manager", None)
    emb = sem.embedder if sem is not None else None
    facts["semantic"] = {
        "configured": bool(emb is not None and emb.available),
        "available": bool(emb is not None and emb.available),
        "semantic_ready": bool(sem is not None and sem.semantic_ready),
        "state": sem.state if sem is not None else "disabled",
        "reason": getattr(sem, "reason", None) or "",
        "model": emb.model if emb is not None else "",
        "dimension": emb.dimension if emb is not None else 0,
        "unembedded": await sem.unembedded() if sem is not None else 0,
    }
    return facts


def _current_exposed(server: FastMCP) -> set[str]:
    """Names of LLM-visible tools currently registered on *server*."""
    from fastmcp.tools.function_tool import FunctionTool

    out: set[str] = set()
    for comp in getattr(server.local_provider, "_components", {}).values():
        if isinstance(comp, FunctionTool) and not comp.name.startswith(
            INTERNAL_TOOL_PREFIX
        ):
            out.add(comp.name)
    return out


def _sync_registry(server: FastMCP, registry: "ToolRegistry") -> None:
    """Make the server's exposed tool set match the live registry.

    Idempotent full diff: registers tools the registry has that the server
    doesn't, removes the reverse.  Called on startup and after every registry
    mutation (the change listener).
    """
    from fastmcp.tools.function_tool import FunctionTool

    exposed = {
        t.name: t for t in registry.list_tools()
        if is_exposed(t)
    }
    current = _current_exposed(server)
    for name in current - exposed.keys():
        try:
            server.local_provider.remove_tool(name)
        except KeyError:
            pass
    for name, slife_tool in exposed.items():
        if name in current:
            continue

        # Bind the name as a default argument so each _run closes over ITS OWN
        # value of `name` — a bare `async def` here would close over the shared
        # loop variable, making every tool execute the last-registered one.
        # `_name` never appears in the tool's inputSchema (parameters are set
        # explicitly below), so it is not client-injectable.
        async def _run(
            _name=name, ctx: Context | None = None, **kwargs
        ):
            _capture_session(ctx)
            return await registry.execute(_name, **kwargs)

        _run.__name__ = name
        _run.__doc__ = slife_tool.description
        ft = FunctionTool(
            name=name,
            description=slife_tool.description,
            parameters=dict(slife_tool.parameters),
            fn=_run,
            run_in_thread=False,
        )
        try:
            server.add_tool(ft)
        except Exception as e:
            logger.warning("host_tool_register_failed name=%s err=%s", name, e)


#: Guard so the change listener is wired once per server instance.
_started_servers: set[int] = set()


async def _serve_host(server, host: str, sockets, port: int) -> None:
    """Serve one Streamable-HTTP run of *server* (blocking until it stops)."""
    if sockets is not None:
        await server.run_async(
            transport="streamable-http",
            host=host,
            sockets=sockets,
            show_banner=False,
        )
    else:
        await server.run_async(
            transport="streamable-http",
            host=host,
            port=port,
            show_banner=False,
        )


def start_host_server(
    registry: "ToolRegistry",
    *,
    catalog: "ToolCatalogService | None" = None,
    port: int = 0,
    host: str = "127.0.0.1",
    instructions: str = "",
):
    """Start the slife-as-plugin server as an in-process asyncio task.

    Each instance binds its own OS-assigned free port (``port=0``) so
    concurrent agents on one host never collide — the actual bound port is
    returned (and published by the caller as ``SLIFE_HOST_PORT``).  An
    explicit non-zero *port* is honored for tests/debugging only.

    Returns ``(server, task, stop_coro, port)``:

    - ``server`` — the FastMCP instance (for introspection/tests).
    - ``task`` — the running asyncio task serving Streamable HTTP.
    - ``stop_coro`` — ``await`` it to shut the server down cleanly.
    - ``port`` — the bound port (OS-assigned when *port* was 0).

    Must run from inside a running event loop (the main agent's).  Subscribes
    to the registry's change listener so a live tool-set mutation pushes
    ``notifications/tools/list_changed`` to connected consumers.
    """
    server = build_registry_mcp(registry, catalog=catalog, instructions=instructions)

    # Bind a free socket up front (race-free — no gap between port discovery
    # and serve) unless an explicit port was requested.  Mirror of the plugin
    # child path (``run_plugin_server`` with ``sockets=[sock]``).
    sockets = None
    if not port:
        sock, port = bind_free_port(host)
        sockets = [sock]

    async def _serve() -> None:
        # The host-as-plugin runs IN the main process, so the child-process
        # watchdog doesn't cover it — self-heal here instead: an unexpected
        # death of the serve task is logged and the same server rebinds with
        # backoff (the plugin-induced restart symmetry: the host is also a
        # plugin).  Port stays stable (same socket).  A clean stop (shutdown)
        # returns; cancellation propagates.
        backoff = _timeouts.timeouts.ready.watchdog_backoff_initial
        while True:
            try:
                await _serve_host(server, host, sockets, port)
                return  # clean stop (shutdown path)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "host_server_serve_died — respawning in %.0fs", backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(
                    backoff * _timeouts.timeouts.ready.watchdog_backoff_multiplier,
                    _timeouts.timeouts.ready.watchdog_backoff_max,
                )

    task = asyncio.create_task(_serve())
    sid = id(server)

    if sid not in _started_servers:
        _started_servers.add(sid)

        async def _on_registry_changed() -> None:
            _sync_registry(server, registry)
            _request_tools_changed()

        def _listen() -> None:
            try:
                asyncio.get_running_loop().create_task(_on_registry_changed())
            except RuntimeError:
                pass  # no running loop — skip (startup-safe)

        registry.add_change_listener(_listen)

    async def _stop() -> None:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        _started_servers.discard(sid)

    return server, task, _stop, port