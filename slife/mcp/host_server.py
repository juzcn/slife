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

from slife.server_utils import (
    INTERNAL_TOOL_PREFIX,
    ToolsChangedNotifier,
    bind_free_port,
    flush_tools_changed,
    request_tools_changed,
    tools_changed_bus,
)
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


#: Fan-out of ``tools/list_changed`` to this server's listen subscribers
#: (:class:`slife.server_utils.ToolsChangedNotifier`).  The ``FastMCP``
#: instance is built later (inside ``build_registry_mcp``), so the server is
#: bound there; a publish before that is a logged no-op.
_notifier = ToolsChangedNotifier()


def _request_tools_changed() -> None:
    """Publish ``tools/list_changed`` to the listen subscribers.

    Fire-and-forget — see :func:`slife.server_utils.request_tools_changed`
    (hosts re-list on receipt, so no ordering guarantees are assumed).
    """
    request_tools_changed(_notifier)


async def _notify_tools_changed() -> None:
    """Eager-flush alias kept for tests/…: run one full notification round
    now, in this task (deterministic delivery — no coalescing).  Production
    notification paths should use :func:`_request_tools_changed`."""
    await flush_tools_changed(_notifier)


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
    # The server exists only now — bind the notifier to its subscription bus
    # so registry changes reach the listen subscribers (`tools_changed_bus`
    # also registers the listen handler, which fastmcp does not).
    _notifier.bind(tools_changed_bus(server))

    @server.tool(
        name="__check",
        description=("slife live facts: exposed tools + unified tool catalog. "
                     "Internal — harness probe, never exposed to the LLM."),
    )
    async def __check(ctx: Context | None = None) -> str:
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
        # Servers are no longer a table — count the ones that own tool rows.
        # ONE number, not split by family: the catalog holds tools, and an MCP
        # server vs a REST API is a distinction of how the CONFIG implements
        # something, not of what the catalog contains.  Splitting it here put
        # an implementation detail on a surface the agent reads, and the "0
        # rest-api" it produced contradicted the `rest-api` component beside
        # it — which counts CONFIGURED servers, a different population.
        facts["servers"] = len(await store.list_source_ids())
        facts["loaded"] = await store.count_loaded()

        sem = getattr(catalog, "semantic_manager", None)
        # The state comes from whichever source has it: the manager, when this
        # process runs the drainer, or the row the owner published into this
        # db's ``meta`` table (``_set_state`` → ``_publish_state``) when it does
        # not.  Both carry the SAME shape — ``semantic_facts()`` is the one
        # builder — so a reader cannot tell the two apart, which is the point:
        # the index is shared, and its state is the same fact in either process.
        facts["semantic"] = {
            **(sem.semantic_facts() if sem is not None
               else await _published_semantic_facts(store)),
            # The pending count is a DB fact, so it is answerable without a
            # drainer, and it is never published (a copy would go stale against
            # the rows it counts).
            "unembedded": (
                await sem.unembedded() if sem is not None
                else await store.count_unembedded()
            ),
        }
    except Exception as e:
        # "Never raises" — a broken catalog OR a broken semantic surface both
        # report an error field instead of crashing the probe.
        facts["error"] = f"catalog probe failed: {e}"
    return facts


async def _published_semantic_facts(store) -> dict:
    """The shared index's state as published by its drainer (``meta`` row).

    The parse lives with the key it reads (``slife/tools/semantic.py``) because
    the query-side ``SemanticReader`` must agree with this health block about
    whether the index is usable — two parsers of one row is two answers.
    """
    from slife.tools.semantic import read_published_state

    return await read_published_state(store)


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
        # plugin).  A clean stop (shutdown) returns; cancellation propagates.
        nonlocal sockets, port
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
                # The failed run may have closed the pre-bound socket — reusing
                # it would fail every retry identically and the "self-heal"
                # would spin forever, permanently dead.  Bind a fresh socket
                # each retry (the new port is re-published by the caller's
                # healthy-port path, so holdouts on the old port were already
                # pointed at a dead server).
                if sockets is not None:
                    sock, port = bind_free_port(host)
                    sockets = [sock]

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