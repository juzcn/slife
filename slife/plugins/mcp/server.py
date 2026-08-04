"""slife-mcp wrapper server — FastMCP server with MCP connection management tools.

This is the entry point for the slife-mcp child process. It:
  1. Starts a FastMCP server on Streamable HTTP transport (auto-assigned port)
  2. Exposes management tools for the slife agent to control external MCP connections
  3. Maintains persistent connections to external MCP servers
"""

import json
import os

from slife.plugins.mcp.connection import ConnectionPool, ServerConfig, ServerStatus
from slife.server_utils import create_plugin_server
from slife.logfmt import ok_json, error_json

mcp, _log_path, logger = create_plugin_server(
    "slife-mcp",
    instructions=(
        "slife-mcp is a wrapper service that manages connections to external "
        "MCP servers. Use the management tools to add/remove servers, "
        "discover tools, and call tools on connected servers."
    ),
)

# ── Global state ─────────────────────────────────────────────────────

_pool = ConnectionPool()

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
        and a.active == b.active
        and a.auth == b.auth
    )


@mcp.tool(
    name="mcp_add_server",
    description=(
        "Add, update, or reconnect an external MCP server (upsert — idempotent). "
        "Three transports are supported:\n"
        "- stdio: provide `command` and `args` to spawn a local process.\n"
        "- http (SSE): provide `url` pointing to the server's SSE endpoint "
        "(e.g. http://host:port/sse). The gateway auto-detects SSE vs "
        "streamable HTTP.\n"
        "- http (streamable): provide `url` for a stateless MCP endpoint.\n"
        "Research the server's docs first. "
        "For the `env` parameter: use ${VAR} references for secrets "
        "(e.g. SERPER_API_KEY: '${SERPER_API_KEY}'). NEVER pass plaintext "
        "API keys or tokens — tell the user to run 'credstore set <KEY>' "
        "in their terminal first, then use the ${VAR} reference here. "
        "Set activate=false to connect without loading tools (lazy disclosure). "
        "Returns the list of discovered tools on success; on failure the error "
        "includes the server's stderr. "
        "Include source provenance when the server is installed from a known "
        "registry — helps track where tools came from for future maintenance."
    ),
)
async def mcp_add_server(
    name: str,
    command: str = "",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    url: str = "",
    headers: dict[str, str] | None = None,
    description: str = "",
    activate: bool = True,
    enabled: bool = True,
    source: dict | None = None,
    auth: dict | None = None,
) -> str:
    """Add or update an MCP server (upsert — idempotent).

    If a server with *name* already exists and its config is identical,
    returns ``already_connected`` without restarting.  If config differs,
    the server is restarted with the new settings.
    """
    if not command and not url:
        return error_json(
            "Either 'command' (for stdio) or 'url' (for HTTP) must be provided.",
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
        active=activate,
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
        logger.exception("mcp_add_failed server=%s", name)
        return error_json(str(e), server=name)


@mcp.tool(
    name="mcp_remove_server",
    description=(
        "Remove an MCP server: stop its process, unregister all its tools, "
        "and persist the removal to config so it won't auto-connect next startup."
    ),
)
async def mcp_remove_server(name: str) -> str:
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
    name="mcp_list_servers",
    description=(
        "List all configured MCP servers with their connection status, "
        "tool counts, disclosure mode (eager/lazy), and enabled/disabled state."
    ),
)
async def mcp_list_servers() -> str:
    """List all configured MCP servers."""
    servers = _pool.list_servers()
    if not servers:
        return "No MCP servers configured."
    return json.dumps(servers, ensure_ascii=False, indent=2)


@mcp.tool(
    name="mcp_list_tools",
    description=(
        "List all tools an MCP server provides, even if the server is inactive. "
        "Browse tools before deciding whether to activate the server. "
        "Each tool name includes the server prefix (e.g. 'filesystem__read_file')."
    ),
)
async def mcp_list_tools(server: str) -> str:
    """List tools from an MCP server.

    Args:
        server: Server name (required). Use mcp_list_servers to discover server names.
    """
    try:
        tools = _pool.list_all_tools(server_name=server)
        if not tools:
            return ok_json(tools=[], server=server,
                           note=f"No tools from server '{server}'.")

        return ok_json(tools=tools)
    except Exception as e:
        logger.exception("mcp_list_tools_failed")
        return error_json(str(e))


@mcp.tool(
    name="mcp_call_tool",
    description=(
        "Call a tool on a connected MCP server. "
        "Use mcp_list_tools first to discover available tools and their names. "
        "Arguments should be passed as a JSON object string."
    ),
)
async def mcp_call_tool(
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


@mcp.tool(
    name="mcp_set_server",
    description=(
        "Enable, disable, or set disclosure mode for an MCP server. "
        "When enabled, connects to the server and returns discovered tools. "
        "When disabled, disconnects and marks it for skip on next startup. "
        "Use disclosure=\"lazy\" to keep the server connected but unload its "
        "tools from context (switch back with disclosure=\"eager\"). "
        "The server config is preserved either way."
    ),
)
async def mcp_set_server(
    name: str,
    enabled: bool | None = None,
    disclosure: str | None = None,
) -> str:
    conn = _pool.get_server(name)
    if conn is None:
        return error_json(
            f"Server '{name}' not found. Use mcp_add_server to add it first.",
            server=name,
        )

    changed: list[str] = []

    # ── Handle enable / disable ──────────────────────────────────────
    if enabled is True:
        conn.config.enabled = True
        changed.append("enabled")
        try:
            if conn.status.value != "connected":
                await conn.connect()

            if conn.status.value == "connected":
                tools = conn.list_tools()
                return ok_json(
                    status="connected" if "enabled" in changed else "already_connected",
                    server=name,
                    tool_count=len(tools),
                    tools=[t["name"] for t in tools],
                    changed=changed,
                )
            else:
                return error_json(
                    conn.error or "Unknown error",
                    status=conn.status.value,
                    server=name,
                )
        except Exception as e:
            logger.exception("mcp_set_enable_failed server=%s", name)
            return error_json(str(e), server=name)
    elif enabled is False:
        conn.config.enabled = False
        changed.append("disabled")
        await _pool.disconnect_server(name)

    # ── Handle disclosure change ─────────────────────────────────────
    if disclosure == "eager":
        if not conn.config.enabled:
            return error_json(
                "Cannot set eager disclosure on a disabled server. Enable it first.",
                server=name,
            )
        result = await _pool.activate_server(name)
        result["disclosure"] = "eager"
        result["changed"] = changed + ["disclosure"]
        return json.dumps(result, ensure_ascii=False, indent=2)
    elif disclosure == "lazy":
        conn.set_active(False)
        changed.append("disclosure")
        return ok_json(
            server=name,
            disclosure="lazy",
            tool_count=conn.tool_count,
            changed=changed,
            note="Tools unregistered. Server stays connected — set disclosure=eager to reload.",
        )
    elif disclosure is not None:
        return error_json(
            f"Invalid disclosure value: '{disclosure}'. Must be 'eager' or 'lazy'.",
            server=name,
        )

    if not changed:
        return ok_json(
            status="unchanged",
            server=name,
            note="No changes requested. Pass enabled=true/false or disclosure=eager/lazy.",
        )

    return ok_json(
        status="ok",
        server=name,
        changed=changed,
    )


# ── Entry point ──────────────────────────────────────────────────────


def main():
    """Run the slife-mcp wrapper server on Streamable HTTP transport."""
    from slife.server_utils import run_plugin_server

    logger.info("mcp_start log=%s pid=%s", _log_path, os.getpid())
    run_plugin_server(mcp)
    logger.info("mcp_stop")


if __name__ == "__main__":
    main()
