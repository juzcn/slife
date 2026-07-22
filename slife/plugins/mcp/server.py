"""slife-mcp wrapper server — FastMCP server with MCP connection management tools.

This is the entry point for the slife-mcp child process. It:
  1. Starts a FastMCP server on Streamable HTTP transport (auto-assigned port)
  2. Exposes management tools for the slife agent to control external MCP connections
  3. Maintains persistent connections to external MCP servers
"""

import json
import os

from typing import Literal

from slife.plugins.mcp.connection import ConnectionPool, ServerConfig
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


@mcp.tool(
    name="mcp_add_server",
    description=(
        "Connect to an external MCP server and make its tools available. "
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
        "Set activate=false to connect without loading tools (use "
        "mcp_set_disclosure later to load them on demand). "
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
    source: dict | None = None,
) -> str:
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
    )

    try:
        conn = await _pool.add_server(config)

        if conn.status.value == "connected":
            tools = conn.list_tools()
            tool_names = [t["name"] for t in tools]
            return ok_json(
                status="connected",
                server=name,
                transport=config.transport,
                tool_count=len(tools),
                tools=tool_names,
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
        "tool counts, and active/inactive state. "
        "Inactive servers (active=false) need mcp_set_disclosure to load their tools."
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
    name="mcp_check_server",
    description=(
        "Check a single MCP server's status. "
        "Returns connection state, active flag (active=true means tools are loaded), "
        "tool count, and description. "
        "Use before activating an inactive server to confirm it's connected."
    ),
)
async def mcp_check_server(name: str) -> str:
    result = _pool.check_server(name)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool(
    name="mcp_set_disclosure",
    description=(
        "Switch an MCP server between eager and lazy mode. "
        "eager: immediately load and register all tools (default). "
        "lazy: immediately unregister tools to free context, persisted to config. "
        "The server stays connected — switch back to eager to reload tools."
    ),
)
async def mcp_set_disclosure(name: str, disclosure: Literal["eager", "lazy"]) -> str:
    try:
        if disclosure == "eager":
            result = await _pool.activate_server(name)
            result["disclosure"] = "eager"
            return json.dumps(result, ensure_ascii=False, indent=2)
        else:
            conn = _pool.get_server(name)
            if conn is None:
                return error_json(f"Server '{name}' not found.", server=name)
            return ok_json(
                server=name,
                disclosure="lazy",
                tool_count=conn.tool_count,
                note="Tools unregistered immediately. Server stays connected — switch back to eager to reload.",
            )
    except Exception as e:
        logger.exception("mcp_disclosure_failed server=%s", name)
        return error_json(str(e), server=name)


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
    name="mcp_reload",
    description=(
        "Reconnect to an MCP server to refresh its tool list. "
        "Use after a server is updated or restarted. "
        "If no server name given, reloads all connected servers."
    ),
)
async def mcp_reload(server: str | None = None) -> str:
    """Reconnect to refresh tool lists.

    Args:
        server: Optional server name. If omitted, reloads all servers.
    """
    if server:
        conn = _pool.get_server(server)
        if conn is None:
            return error_json(f"Server '{server}' not found.", server=server)

        config = conn.config
        await _pool.remove_server(server)
        new_conn = await _pool.add_server(config)

        return ok_json(
            status=new_conn.status.value,
            server=server,
            tool_count=new_conn.tool_count,
        )
    else:
        servers = _pool.list_servers()
        configs = []
        for s in servers:
            conn = _pool.get_server(s["name"])
            if conn:
                configs.append(conn.config)

        # Disconnect all
        for config in configs:
            await _pool.remove_server(config.name)

        # Reconnect all
        results = []
        for config in configs:
            conn = await _pool.add_server(config)
            results.append({
                "server": config.name,
                "status": conn.status.value,
                "tool_count": conn.tool_count,
            })

        return json.dumps(results, ensure_ascii=False, indent=2)


@mcp.tool(
    name="mcp_enable_server",
    description=(
        "Connect to a pre-configured but disabled MCP server. "
        "Use this to start a server that was added with enabled=false "
        "or was previously disabled with mcp_disable_server. "
        "Returns the list of discovered tools on success."
    ),
)
async def mcp_enable_server(name: str) -> str:
    conn = _pool.get_server(name)
    if conn is None:
        # Server not in pool — look it up in config and add
        return error_json(
            f"Server '{name}' not found. Use mcp_add_server to add it first.",
            server=name,
        )

    conn.config.enabled = True
    try:
        # If already connected, just return current state
        if conn.status.value == "connected":
            tools = conn.list_tools()
            return ok_json(
                status="already_connected",
                server=name,
                tool_count=len(tools),
                tools=[t["name"] for t in tools],
            )

        await conn.connect()
        if conn.status.value == "connected":
            tools = conn.list_tools()
            return ok_json(
                status="connected",
                server=name,
                tool_count=len(tools),
                tools=[t["name"] for t in tools],
            )
        else:
            return error_json(
                conn.error or "Unknown error",
                status=conn.status.value,
                server=name,
            )
    except Exception as e:
        logger.exception("mcp_enable_failed server=%s", name)
        return error_json(str(e), server=name)


@mcp.tool(
    name="mcp_disable_server",
    description=(
        "Disconnect and disable an MCP server. The server config is preserved "
        "but it won't auto-connect on next startup. Use mcp_enable_server to "
        "reconnect it later."
    ),
)
async def mcp_disable_server(name: str) -> str:
    conn = _pool.get_server(name)
    if conn is None:
        return error_json(f"Server '{name}' not found.", server=name)

    conn.config.enabled = False
    await _pool.remove_server(name)
    return ok_json(
        status="disabled",
        server=name,
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
