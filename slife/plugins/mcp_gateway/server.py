"""mcp-gateway wrapper server — FastMCP server with MCP connection management tools.

This is the entry point for the mcp-gateway child process. It:
  1. Starts a FastMCP server on Streamable HTTP transport (auto-assigned port)
  2. Exposes management tools (bare names) to manage external MCP connections
  3. Maintains persistent connections to external MCP servers
  4. Self-hosts its config: loads ``tools.json5`` on startup and
     persists ``mcp_set`` / ``mcp_remove`` / ``mcp_set_enabled`` through
     ``mcp_gateway.config`` — no host involvement.

Spawned by Slife (or any host) via ``python -m slife.plugins.mcp_gateway.server``.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager

from fastmcp.server.context import Context

from slife.plugins.mcp_gateway import config as plugin_config
from slife.plugins.spec import mcp_child_reserved_names
from slife.plugins.mcp_gateway.connection import ConnectionPool, ServerConfig, ServerStatus
from slife.logfmt import error_json, ok_json
from slife.server_utils import create_plugin_server
from slife.server_utils import ToolsChangedNotifier, tools_changed_bus


@asynccontextmanager
async def _mcp_lifespan(_app):
    """Self-host config; release all external MCP connections on shutdown.

    The lifespan schedules auto-connect and returns immediately, so the ready
    port signal (fired by the server runtime once the lifespan completes) is
    never blocked by a slow external server.  Runs on the server's event loop
    (uvicorn lifespan), so the pool's async HTTP/SSE clients, stdio processes
    and health-monitor tasks are closed on the same loop that created them —
    otherwise connections leak on exit.
    """
    asyncio.ensure_future(_auto_connect_configured())
    try:
        yield
    finally:
        try:
            await _pool.shutdown()
        except Exception as e:
            logger.debug("mcp_pool_shutdown_error err=%s", e)


async def _auto_connect_configured() -> None:
    """Register every configured server, CONNECTING the enabled ones.

    ``tools.json5`` is the whole decision: enabled ⇒ bring it up now, disabled
    ⇒ register it (so ``mcp_list`` lists the same set as the config) and leave
    it down.  Nothing is remembered between sessions — no db, no snapshot: a
    server that was down when you quit is retried at the next boot like any
    other, which is exactly what the pre-catalog gateway did.

    Best-effort and fire-and-forget from the lifespan — a slow server must
    never delay the ready port signal.
    """
    try:
        raw = plugin_config.load_config()
    except Exception as e:
        logger.warning("mcp_config_load_failed err=%s", e)
        return
    # servers live in the mcp.servers / rest-api sections (tools.json5) —
    # use the merged view with the legacy top-level fallback, never the
    # raw ``servers`` key (gone since the section restructure).
    servers = plugin_config._servers_dict(raw)
    if not isinstance(servers, dict):
        return
    configured = [
        (name, entry) for name, entry in servers.items()
        if isinstance(entry, dict)
    ]
    logger.info("mcp_configured count=%d (connecting the enabled ones)", len(configured))

    async def _register_one(name: str, entry: dict) -> None:
        try:
            cfg = plugin_config.resolve_server_config(name, entry)
            if not cfg.enabled:
                await _pool.add_server(cfg, connect=False)
                return
            conn = await _pool.add_server(cfg)
            # ``add_server``'s connect() SWALLOWS connect exceptions (it lands
            # FAILED with ``_error`` and starts a health monitor).  Announce it
            # anyway: the host's reconcile turns an unreachable server into
            # `status='error'` on its tools, which only happens if it hears
            # about the failure.
            if conn.status == ServerStatus.FAILED:
                logger.warning(
                    "mcp_auto_connect_failed server=%s err=%s",
                    name, getattr(conn, "_error", "") or "connect failed",
                )
                _request_tools_changed()
        except Exception as e:
            logger.warning("mcp_auto_connect_failed server=%s err=%s", name, e)
            _request_tools_changed()

    await asyncio.gather(*(_register_one(n, e) for n, e in configured))


mcp, _log_path, logger = create_plugin_server(
    "mcp-gateway",
    instructions=(
        "mcp-gateway is a gateway that manages connections to external MCP "
        "servers. Use the management tools to add/remove servers, discover "
        "tools, and call tools on connected servers."
    ),
    lifespan=_mcp_lifespan,
)


# ── Global state ─────────────────────────────────────────────────────

#: Fan-out of ``tools/list_changed`` to this server's listen subscribers
#: (:class:`slife.server_utils.ToolsChangedNotifier`).  No session
#: bookkeeping: the modern era delivers change events only to streams a
#: client opened with ``subscriptions/listen``, so publishing on the
#: server's bus reaches all of them — from a request handler or a
#: background task alike.  ``tools_changed_bus`` also registers the listen
#: handler (fastmcp does not).
_notifier = ToolsChangedNotifier(tools_changed_bus(mcp))


def _request_tools_changed() -> None:
    """Publish ``tools/list_changed`` to the listen subscribers.

    Fire-and-forget — the publish is scheduled as a DETACHED task, so a
    slow ``tools/call`` handler (e.g. ``mcp_set_enabled`` (re)connecting a
    server) never blocks on the fan-out.  Callers MUST NOT rely on delivery
    ordering; hosts re-list on receipt.
    """
    _notifier.request_tools_changed()


async def _notify_tools_changed() -> None:
    """Eager-flush alias kept for tests/…: publish in this task (deterministic
    delivery).  Production paths should use :func:`_request_tools_changed`."""
    await _notifier.flush()


# ── Connection → host notification ──────────────────────────────────────
# The wrapper owns the CONNECTION, not a catalog: unified tool discovery /
# search / load lives in the shared host ``tools.db``
# (``slife.tools.catalog`` — the single catalog per DESIGNER_NOTES §8.5).
# On a successful connect the host is told to re-list, and reconciles server
# runtime + tool rows from ``mcp_list_tools`` / ``__check`` itself.

async def _on_connected(server_name: str) -> None:
    """A server connected — notify the host so it re-syncs tools/runtime."""
    _request_tools_changed()


_pool = ConnectionPool(on_connected=_on_connected)

# Built-in Slife plugin server names — reserved: an external MCP server must
# not take one of these, or its tools would collide / misroute in the host's
# namespace.  Derived from the central plugin contract so every built-in
# plugin (job-coding included) is always covered.
_RESERVED_SERVER_NAMES = mcp_child_reserved_names()

# ═══════════════════════════════════════════════════════════════════════
# Management tools
# ═══════════════════════════════════════════════════════════════════════


# ── Config comparison for idempotency ──────────────────────────────

def _server_config_equal(a: ServerConfig, b: ServerConfig) -> bool:
    """Compare two ServerConfigs for equality.

    ``description``/``source`` are deliberately ignored — metadata, not part
    of the connection definition — so a change must not trigger a spurious
    restart.
    """
    return (
        a.name == b.name
        and a.command == b.command
        and a.args == b.args
        and a.env == b.env
        and a.url == b.url
        and a.headers == b.headers
        and a.enabled == b.enabled
        and a.auth == b.auth
        and a.os_paths == b.os_paths
    )


def _persist_entry(
    name: str,
    command: str,
    args: list[str] | None,
    env: dict[str, str] | None,
    url: str,
    headers: dict[str, str] | None,
    description: str,
    source: dict | None,
    auth: dict | None,
    enabled: bool = True,
) -> None:
    """Persist a server entry to tools.json5 (merge semantics).

    ``enabled=True`` (the default) leaves the flag untouched — only
    ``mcp_set_enabled`` flips enable/disable; ``enabled=False`` is written
    so the server stays disconnected on the next wrapper start.
    """
    entry: dict = {
        "command": command,
        "args": args,
        "env": env,
        "url": url,
        "headers": headers,
        "description": description,
        "source": source,
        "auth": auth,
    }
    if not enabled:
        entry["enabled"] = False
    plugin_config.add_server_entry(name, entry)


@mcp.tool(
    name="mcp_set",
    description=(
        "Add or update an external MCP server connection (upsert; stdio via "
        "`command`/`args`, or http via `url`)."
    ),
)
async def mcp_set(
    name: str,
    command: str = "",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    url: str = "",
    headers: dict[str, str] | None = None,
    description: str = "",
    enabled: bool = True,
    source: dict | None = None,
    auth: dict | None = None,
    ctx: Context | None = None,
) -> str:
    """Add or update an MCP server (upsert — idempotent).

    Identical config → ``already_connected``, no restart.  Changed config →
    restart.  ``enabled`` sets the initial state; use ``mcp_set_enabled`` to
    toggle enable/disable at runtime.  Persisted to tools.json5.

    Args:
        name: Unique server name (not a reserved plugin name).
        command: For stdio servers — the binary (npx, uvx, python).
        args: For stdio servers — command-line arguments (list).
        env: Environment overrides. Use ${VAR} refs for secrets, never plaintext.
        url: For http servers — the SSE or streamable endpoint (auto-detected).
        headers: HTTP headers. Use ${VAR} refs for secrets, never plaintext.
        description: What the server does, in its own language — don't translate.
        enabled: Initial state: true connects now, false stays disconnected.
        source: Provenance (e.g. registry) for future updates.
        auth: OAuth config for device code flow (auth type 'oauth').
    """

    if not command and not url:
        return error_json(
            "Either 'command' (for stdio) or 'url' (for HTTP) must be provided.",
            server=name,
        )

    if name in _RESERVED_SERVER_NAMES:
        return error_json(
            f"Server name '{name}' is reserved by a built-in plugin. "
            f"Choose a different name.",
            server=name,
        )

    # os_paths / auto_load / source are config-file fields (not mcp_set
    # params) — preserve them across an upsert so a hand-edited flag isn't
    # silently reset on the running connection.
    existing = _pool.get_server(name)
    # Resolve ${VAR} refs in env/auth the SAME way resolve_server_config
    # does, so _server_config_equal compares resolved-vs-resolved.  The tool
    # takes raw refs (secrets must not come back through tool args); without
    # resolution every re-run of mcp_set with identical args compared
    # {"FOO": "${FOO}"} against the pool's resolved {"FOO": "secret"} and
    # never matched — tearing down + reconnecting (and re-running OAuth).
    resolved_env = None
    if env:
        resolved_env = {k: plugin_config._resolve_secret(str(v)) for k, v in env.items()}
    resolved_auth = None
    if auth:
        resolved_auth = dict(auth)
        for auth_key in ("client_id", "client_secret"):
            if auth_key in resolved_auth and isinstance(resolved_auth[auth_key], str):
                resolved_auth[auth_key] = plugin_config._resolve_secret(resolved_auth[auth_key])
    config = ServerConfig(
        name=name,
        command=command,
        args=args or [],
        env=resolved_env,
        url=url,
        headers=headers,
        description=description,
        enabled=enabled,
        auth=resolved_auth,
        os_paths=existing.config.os_paths if existing else False,
        auto_load=existing.config.auto_load if existing else False,
        source=(source if source is not None
                else (existing.config.source if existing else None)),
    )

    try:
        if existing is not None and _server_config_equal(existing.config, config):
            if existing.status == ServerStatus.CONNECTED:
                tools = existing.list_tools()
                return ok_json(
                    status="already_connected",
                    server=name,
                    transport=config.transport,
                    tool_count=len(tools),
                    tools=[t["name"] for t in tools],
                    note="Server config unchanged — no restart needed.",
                )

        conn = await _pool.add_server(config)
        _persist_entry(
            name, command, args, env, url, headers,
            description, source, auth, enabled,
        )

        if conn.status.value == "connected":
            tools = conn.list_tools()
            return ok_json(
                status="connected",
                server=name,
                transport=config.transport,
                tool_count=len(tools),
                tools=[t["name"] for t in tools],
            )
        elif not config.enabled:
            return ok_json(
                status="disabled",
                server=name,
                note="Server added to pool but not connected (enabled=false).",
            )
        else:
            return error_json(
                conn.error or "Unknown error",
                status=conn.status.value,
                server=name,
            )
    except Exception as e:
        logger.exception("mcp_set_failed server=%s", name)
        return error_json(str(e), server=name)


@mcp.tool(
    name="mcp_set_enabled",
    description=(
        "Enable or disable an MCP server (true reconnects + loads tools; "
        "false disconnects + unloads)."
    ),
)
async def mcp_set_enabled(name: str, enabled: bool, ctx: Context | None = None) -> str:
    """Toggle enable/disable on an existing MCP server.

    Args:
        name: Server name (from mcp_list).
        enabled: true = reconnect and load tools; false = disconnect and unload.
    """
    existing = _pool.get_server(name)
    if existing is None:
        return error_json(
            f"Server '{name}' not found. Use mcp_set to add it first.",
            server=name,
        )
    existing.config.enabled = enabled
    if enabled:
        # Persist the flag: enabled is the default, so this removes the
        # ``enabled: false`` a prior disable wrote — otherwise the re-enable
        # would be lost on the next restart (the server would load disabled).
        plugin_config.set_server_enabled(name, True)
        if existing.status == ServerStatus.CONNECTED:
            tools = existing.list_tools()
            return ok_json(
                status="connected",
                server=name,
                transport=existing.config.transport,
                tool_count=len(tools),
                tools=[t["name"] for t in tools],
                note="Server enabled.",
            )
        # Not connected — fan the (re)connect out behind a fast response.
        # connect() owns its per-connection lock and, on success, fires
        # ``_on_connected`` -> catalog sync + one coalesced list_changed the
        # host re-lists from.  Returning immediately keeps this control call
        # off the slow path that previously held the request open for seconds
        # while notification bursts interleaved with its cancel scope (the
        # mcp 2.1.1 dispatcher crash this module guards against).
        async def _connect_async() -> None:
            try:
                await existing.connect()
            except Exception as e:
                logger.warning("mcp_enable_connect_failed server=%s err=%s", name, e)

        asyncio.create_task(_connect_async())
        return ok_json(
            status="enabling",
            server=name,
            transport=existing.config.transport,
            tool_count=0,
            tools=[],
            note="Server enabling — tools register when it connects.",
        )
    await _pool.disconnect_server(name)
    plugin_config.set_server_enabled(name, False)
    # Notify so the host reconcile drops this server's loaded proxies.
    _request_tools_changed()
    return ok_json(
        status="disabled",
        server=name,
        note="Server disabled. Re-enable with mcp_set_enabled(name=..., enabled=true).",
    )


@mcp.tool(
    name="mcp_remove",
    description=(
        "Remove an MCP server: stop process, unregister tools, persist removal to config."
    ),
)
async def mcp_remove(name: str, ctx: Context | None = None) -> str:
    """Stop and remove an MCP server.

    Args:
        name: Server name to remove.
    """
    try:
        await _pool.remove_server(name)
        plugin_config.remove_server_entry(name)
        _request_tools_changed()
        return ok_json(status="removed", server=name)
    except Exception as e:
        logger.exception("mcp_remove_failed server=%s", name)
        return error_json(str(e), server=name)


@mcp.tool(
    name="mcp_list",
    description=(
        "List configured MCP servers (transport, command/url, enabled)."
    ),
)
async def mcp_list(ctx: Context | None = None) -> str:
    """List configured external MCP servers (static config view)."""
    servers = _pool.list_configured()
    return json.dumps(servers, ensure_ascii=False, indent=2)


@mcp.tool(
    name="__check",
    description=(
        "Live connection status of MCP servers: running/stopped, tool counts, "
        "errors. Internal — probed by the harness's system_health."
    ),
)
async def __check(ctx: Context | None = None) -> str:
    """Report live server connection status.

    Returns ``{"servers": [...]}``.  Authoritative for server health:
    ``state=running`` means the server is connected and its tools are
    registered on the agent (the agent re-syncs on reconnect via
    ``notifications/tools/list_changed``).  Catalog semantic-index status is
    reported host-side (the host owns the shared catalog's SemanticManager)."""
    servers = _pool.list_servers()
    return json.dumps({"servers": servers}, ensure_ascii=False, indent=2)


@mcp.tool(
    name="mcp_list_tools",
    description=(
        "List a connected server's tools (full_name server__tool). Use "
        "mcp_list to discover server names."
    ),
)
async def mcp_list_tools(server: str, ctx: Context | None = None) -> str:
    """List a connected server's tools (single, always-live read).

    The shared catalog is fed by the HOST (the agent's reconcile calls this
    tool on connect — ``mcp_list_tools`` is the live source), so there is no
    wrapper-side catalog branch anymore.  Each tool carries its full
    ``{name, description, inputSchema}`` descriptor.

    Args:
        server: Server name (from mcp_list).
    """
    conn = _pool.get_server(server)
    if conn is None or conn.status != ServerStatus.CONNECTED:
        return ok_json(
            server=server,
            connected=False,
            tools=[],
            tool_count=0,
            note=(
                f"Server '{server}' is not connected — its tools load when it "
                "connects. Use mcp_list to see configured servers."
            ),
        )

    try:
        live = _pool.list_all_tools(server_name=server)
    except Exception as e:
        logger.warning("mcp_list_tools_live_failed server=%s err=%s", server, e)
        return error_json(
            f"MCP unavailable for server '{server}' — live tool read failed: {e}",
            server=server,
        )

    return ok_json(
        server=server,
        connected=True,
        source="live",
        tools=live,
        tool_count=len(live),
        note="Live tool list from the connected server (built-in MCP tools/list).",
    )


@mcp.tool(
    name="__mcp_call_tool",
    description=(
        "Call a tool on a connected MCP server (internal — invoked by the "
        "server__tool proxies, not directly by the agent). "
        "arguments = JSON object string."
    ),
)
async def __mcp_call_tool(
    server: str,
    tool_name: str,
    arguments: str = "{}",
    ctx: Context | None = None,
) -> str:
    """Call a tool on a connected MCP server.

    Args:
        server: Server name.
        tool_name: Tool name (without server prefix).
        arguments: JSON string of tool arguments (e.g. '{"path": "/tmp"}').
    """
    try:
        args_dict = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(args_dict, dict):
            args_dict = {}
    except json.JSONDecodeError:
        return f"Error: arguments must be valid JSON. Got: {arguments}"

    # Call-time enforcement (per-mcp): a disabled server refuses the call.
    conn = _pool.get_server(server)
    if conn is not None and not conn.config.enabled:
        return error_json(
            f"Server '{server}' is disabled — enable it with mcp_set_enabled.",
            server=server, tool=tool_name,
        )

    result = await _pool.call_tool(server, tool_name, args_dict)
    return result


# ═══════════════════════════════════════════════════════════════════════
# Server lifecycle + discovery tools
# ═══════════════════════════════════════════════════════════════════════


# ── Entry point ──────────────────────────────────────────────────────


def main():
    """Run the mcp-gateway wrapper server on Streamable HTTP transport."""
    import argparse

    from slife.server_utils import run_plugin_server, shutdown_server_logging

    parser = argparse.ArgumentParser(prog="mcp-gateway-server")
    parser.add_argument(
        "--port", type=int, default=0,
        help="Port to serve on (default: auto-assign a free port).",
    )
    args = parser.parse_args()

    logger.info("mcp_start log=%s pid=%s", _log_path, os.getpid())
    try:
        run_plugin_server(mcp, port=args.port)
    finally:
        logger.info("mcp_stop log=%s pid=%s", _log_path, os.getpid())
        shutdown_server_logging()


if __name__ == "__main__":
    main()