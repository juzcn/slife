"""slife-mcp wrapper server — FastMCP server with MCP connection management tools.

This is the entry point for the slife-mcp child process. It:
  1. Starts a FastMCP server on Streamable HTTP transport (auto-assigned port)
  2. Exposes management tools for the slife agent to control external MCP connections
  3. Maintains persistent connections to external MCP servers
"""

import json
import os
from contextlib import asynccontextmanager
from typing import Any

from fastmcp.server.context import Context

from slife.plugins.mcp.connection import ConnectionPool, ServerConfig, ServerStatus
from slife.server_utils import create_plugin_server
from slife.logfmt import ok_json, error_json


@asynccontextmanager
async def _mcp_lifespan(_app):
    """Release all external MCP connections on server shutdown.

    Runs on the server's event loop (uvicorn lifespan), so the pool's async
    HTTP/SSE clients, stdio processes and health-monitor tasks are closed on
    the same loop that created them — otherwise connections leak on exit

    """
    try:
        yield
    finally:
        try:
            await _pool.shutdown()
        except Exception as e:
            logger.debug("mcp_pool_shutdown_error err=%s", e)


mcp, _log_path, logger = create_plugin_server(
    "slife-mcp",
    instructions=(
        "slife-mcp is a wrapper service that manages connections to external "
        "MCP servers. Use the management tools to add/remove servers, "
        "discover tools, and call tools on connected servers."
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

    Invoked by the connection pool when an external MCP server reconnects
    successfully — the agent listens for this and re-syncs its tool registry.
    Best-effort: a dead/stale session is dropped; the rest are still served.
    """
    for sess in list(_active_sessions):
        try:
            await sess.send_tool_list_changed()
        except Exception:
            _active_sessions.discard(sess)


_pool = ConnectionPool(on_connected=_notify_tools_changed)

# Built-in plugin server names — reserved: an external MCP server must not
# take one of these, or its tools would collide / misroute in the harness
# namespace.
_RESERVED_SERVER_NAMES = frozenset({"mcp", "memdb", "wechat", "memfiles", "a2a", "media"})

# ═══════════════════════════════════════════════════════════════════════
# Management tools
# ═══════════════════════════════════════════════════════════════════════


# ── Config comparison for idempotency ──────────────────────────────

def _server_config_equal(a: ServerConfig, b: ServerConfig) -> bool:
    """Compare two ServerConfigs for equality (ignoring description)."""
    return (
        a.name == b.name
        and a.command == b.command
        and a.args == b.args
        and a.env == b.env
        and a.url == b.url
        and a.headers == b.headers
        and a.enabled == b.enabled
        and a.auth == b.auth
    )


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
    toggle enable/disable at runtime.

    Args:
        name: Unique server name (not a built-in plugin name).
        command: For stdio servers — the binary (npx, uvx, python). Don't wrap in `cmd /c` on native-Windows.
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
        return ok_json(status="removed", server=name)
    except Exception as e:
        logger.exception("mcp_remove_failed server=%s", name)
        return error_json(str(e), server=name)


@mcp.tool(
    name="mcp_list",
    description=(
        "List configured MCP servers (static config: transport, command/url, "
        "enabled). For live status use check_mcp."
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
    via ``notifications/tools/list_changed`` and a periodic poll)."""
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
    """Run the slife-mcp wrapper server on Streamable HTTP transport."""
    from slife.server_utils import run_plugin_server, shutdown_server_logging

    logger.info("mcp_start log=%s pid=%s", _log_path, os.getpid())
    try:
        run_plugin_server(mcp)
    finally:
        logger.info("mcp_stop log=%s pid=%s", _log_path, os.getpid())
        shutdown_server_logging()


if __name__ == "__main__":
    main()
