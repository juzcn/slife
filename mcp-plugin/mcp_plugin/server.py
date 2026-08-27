"""mcp-plugin wrapper server — FastMCP server with MCP connection management tools.

This is the entry point for the mcp-plugin child process. It:
  1. Starts a FastMCP server on Streamable HTTP transport (auto-assigned port)
  2. Exposes management tools (bare names) to manage external MCP connections
  3. Maintains persistent connections to external MCP servers
  4. Self-hosts its config: loads ``mcp-plugin.json5`` on startup and
     persists ``mcp_set`` / ``mcp_remove`` / ``mcp_set_enabled`` through
     ``mcp_plugin.config`` — no host involvement.

Spawned by Slife (or any host) via ``python -m mcp_plugin.server``.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any

from fastmcp.server.context import Context

from mcp_plugin import config as plugin_config
from mcp_plugin.connection import ConnectionPool, ServerConfig, ServerStatus
from mcp_plugin.logging import error_json, ok_json
from mcp_plugin.server_runtime import create_plugin_server


@asynccontextmanager
async def _mcp_lifespan(_app):
    """Self-host config; release all external MCP connections on shutdown.

    The lifespan schedules auto-connect and returns immediately, so the
    ready port signal (fired by the server runtime once the lifespan
    completes) is never blocked by a slow external server.  Runs on the
    server's event loop (uvicorn lifespan), so the pool's async HTTP/SSE
    clients, stdio processes and health-monitor tasks are closed on the
    same loop that created them — otherwise connections leak on exit.
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
    """Load mcp-plugin.json5 and connect every enabled server in parallel.

    Best-effort and fire-and-forget from the lifespan — a slow server must
    never delay the ready port signal.  Failures are logged per server; the
    agent discovers whichever tools actually connected (via mcp_list_tools
    or the tools/list_changed notifications fired on connect).
    """
    try:
        raw = plugin_config.load_config()
    except Exception as e:
        logger.warning("mcp_config_load_failed err=%s", e)
        return
    servers = raw.get("servers", {})
    if not isinstance(servers, dict):
        return
    enabled = [
        (name, entry) for name, entry in servers.items()
        if isinstance(entry, dict) and entry.get("enabled") is not False
    ]
    logger.info("mcp_auto_connect count=%d", len(enabled))

    async def _connect_one(name: str, entry: dict) -> None:
        try:
            cfg = plugin_config.resolve_server_config(name, entry)
            await _pool.add_server(cfg)
        except Exception as e:
            logger.warning("mcp_auto_connect_failed server=%s err=%s", name, e)

    await asyncio.gather(*(_connect_one(n, e) for n, e in enabled))


mcp, _log_path, logger = create_plugin_server(
    "mcp-plugin",
    instructions=(
        "mcp-plugin is a gateway that manages connections to external MCP "
        "servers. Use the management tools to add/remove servers, discover "
        "tools, and call tools on connected servers."
    ),
    lifespan=_mcp_lifespan,
)

# ── Global state ─────────────────────────────────────────────────────

# Client sessions that have made at least one request to this wrapper (the
# main agent, and any subagents sharing it).  A ServerSession is only
# reachable inside a request context (FastMCP's request_context raises
# LookupError in background tasks), so tools that run on the request path
# stash their session here for later use by the reconnect hook.
_active_sessions: set[Any] = set()


def _capture_session(ctx: Context | None) -> None:
    """Remember the caller's ServerSession for background notifications."""
    if ctx is not None and ctx.session is not None:
        _active_sessions.add(ctx.session)


async def _notify_tools_changed() -> None:
    """Push ``notifications/tools/list_changed`` to every known client.

    Invoked by the connection pool when an external MCP server (re)connects
    successfully — a listening host re-syncs its tool registry.  Best-effort:
    a dead/stale session is dropped; the rest are still served.
    """
    for sess in list(_active_sessions):
        try:
            await sess.send_tool_list_changed()
        except Exception:
            _active_sessions.discard(sess)


_pool = ConnectionPool(on_connected=_notify_tools_changed)

# Built-in Slife plugin server names — reserved: an external MCP server must
# not take one of these, or its tools would collide / misroute in the host's
# namespace.
_RESERVED_SERVER_NAMES = frozenset(
    {"mcp", "memdb", "wechat", "memfiles", "sharefile", "a2a", "media"}
)

# ═══════════════════════════════════════════════════════════════════════
# Management tools
# ═══════════════════════════════════════════════════════════════════════


# ── Config comparison for idempotency ──────────────────────────────

def _server_config_equal(a: ServerConfig, b: ServerConfig) -> bool:
    """Compare two ServerConfigs for equality (ignoring description/source)."""
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
    """Persist a server entry to mcp-plugin.json5 (merge semantics).

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
        "Add or update an external MCP server connection (upsert — add + update "
        "in one call). Provide either stdio (`command` + `args`) or http (`url`). "
        "Runtime enable/disable is handled by mcp_set_enabled."
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
    toggle enable/disable at runtime.  Persisted to mcp-plugin.json5.

    Args:
        name: Unique server name (not a reserved parent-plugin name).
        command: For stdio servers — the binary (npx, uvx, python).
        args: For stdio servers — command-line arguments (list).
        env: Environment overrides. Use ${VAR} refs for secrets, never plaintext.
        url: For http servers — the SSE or streamable endpoint (auto-detected).
        headers: HTTP headers. Use ${VAR} refs for secrets, never plaintext.
        description: What the server does, in its own language — don't translate.
        enabled: Initial state — true connects now, false adds but stays disconnected.
        source: Optional provenance (e.g. registry) for future updates.
        auth: Optional OAuth config for device code flow (auth type 'oauth').
    """
    # Remember the caller's session so the reconnect hook can push
    # tools/list_changed notifications (see _notify_tools_changed).
    _capture_session(ctx)

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

    config = ServerConfig(
        name=name,
        command=command,
        args=args or [],
        env=env,
        url=url,
        headers=headers,
        description=description,
        enabled=enabled,
        auth=auth,
    )

    try:
        existing = _pool.get_server(name)
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
        "Enable or disable an existing MCP server. enabled=true reconnects and "
        "loads tools; enabled=false disconnects and unloads tools. This toggles "
        "only the enabled flag — distinct from mcp_set, which configures the "
        "server definition."
    ),
)
async def mcp_set_enabled(name: str, enabled: bool) -> str:
    """Toggle enable/disable on an existing MCP server.

    Args:
        name: Server name (from mcp_list).
        enabled: true reconnects and loads tools; false disconnects and unloads tools.
    """
    existing = _pool.get_server(name)
    if existing is None:
        return error_json(
            f"Server '{name}' not found. Use mcp_set to add it first.",
            server=name,
        )
    existing.config.enabled = enabled
    if enabled:
        if existing.status != ServerStatus.CONNECTED:
            await existing.connect()
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
        return error_json(
            existing.error or "Unknown error",
            status=existing.status.value,
            server=name,
        )
    await _pool.disconnect_server(name)
    plugin_config.set_server_enabled(name, False)
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
async def mcp_remove(name: str) -> str:
    """Stop and remove an MCP server.

    Args:
        name: Server name to remove.
    """
    try:
        await _pool.remove_server(name)
        plugin_config.remove_server_entry(name)
        return ok_json(status="removed", server=name)
    except Exception as e:
        logger.exception("mcp_remove_failed server=%s", name)
        return error_json(str(e), server=name)


@mcp.tool(
    name="mcp_list",
    description=(
        "List configured MCP servers (static config: transport, command/url, "
        "enabled). For live status use __mcp_connection_status."
    ),
)
async def mcp_list() -> str:
    """List configured external MCP servers (static config view)."""
    servers = _pool.list_configured()
    return json.dumps(servers, ensure_ascii=False, indent=2)


@mcp.tool(
    name="__mcp_connection_status",
    description=(
        "Live connection status of MCP servers: running/stopped, tool counts, "
        "errors. Internal — consumed by the check_mcp tool."
    ),
)
async def __mcp_connection_status(ctx: Context | None = None) -> str:
    """Report live connection status of all external MCP servers.

    Authoritative for health: ``state=running`` means the server is connected
    and its tools are registered on the agent (the agent re-syncs on reconnect
    via ``notifications/tools/list_changed``)."""
    # Remember the caller's session for reconnect notifications.
    _capture_session(ctx)
    servers = _pool.list_servers()
    return json.dumps(servers, ensure_ascii=False, indent=2)


@mcp.tool(
    name="mcp_list_tools",
    description=(
        "List a connected MCP server's tools. Names are prefixed server__tool."
    ),
)
async def mcp_list_tools(server: str) -> str:
    """List tools from an MCP server.

    Args:
        server: Server name (required). Use mcp_list to discover server names.
    """
    try:
        tools = _pool.list_all_tools(server_name=server)
        if not tools:
            return ok_json(tools=[], server=server,
                           note=f"No tools from server '{server}'.")

        return ok_json(tools=tools)
    except Exception as e:
        logger.exception("mcp_list_tools_failed server=%s", server)
        return error_json(str(e))


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

    result = await _pool.call_tool(server, tool_name, args_dict)
    return result


# ── Entry point ──────────────────────────────────────────────────────


def main():
    """Run the mcp-plugin wrapper server on Streamable HTTP transport."""
    import argparse

    from mcp_plugin.server_runtime import run_plugin_server, shutdown_server_logging

    parser = argparse.ArgumentParser(prog="mcp-plugin-server")
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