"""slife-mcp wrapper server — FastMCP server with MCP connection management tools.

This is the entry point for the slife-mcp child process. It:
  1. Starts a FastMCP server on stdio transport
  2. Exposes management tools for the slife agent to control external MCP connections
  3. Maintains persistent connections to external MCP servers

Usage:
    uv run python -m slife_mcp.server
"""

import json
import logging
import sys

from fastmcp import FastMCP

from slife_mcp.connection import ConnectionPool, ServerConfig

logger = logging.getLogger("slife_mcp")

# ── Logging setup ───────────────────────────────────────────────────

# Log to stderr so stdout (the MCP transport) stays clean.
_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.DEBUG)
_stderr_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
_root = logging.getLogger()
_root.setLevel(logging.DEBUG)
_root.addHandler(_stderr_handler)

# Silence noisy third-party loggers
for _noisy in ("httpx", "httpcore", "openai", "asyncio", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ── Global state ─────────────────────────────────────────────────────

_pool = ConnectionPool()

# ── FastMCP server ──────────────────────────────────────────────────

mcp = FastMCP(
    "slife-mcp",
    instructions=(
        "slife-mcp is a wrapper service that manages connections to external "
        "MCP servers. Use the management tools to add/remove servers, "
        "discover tools, and call tools on connected servers."
    ),
)

# ═══════════════════════════════════════════════════════════════════════
# Management tools
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="mcp_add_server",
    description=(
        "Add and connect to an external MCP server using standard MCP "
        "configuration format. Returns the server status and discovered tools."
    ),
)
async def mcp_add_server(
    name: str,
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Add and connect to an MCP server.

    Args:
        name: Unique name for this server (e.g. 'filesystem', 'brave-search').
        command: Executable to run (e.g. 'npx', 'python', 'uv').
        args: Command-line arguments (e.g. ['-y', '@modelcontextprotocol/server-filesystem', '/path']).
        env: Optional environment variables to pass to the server process.

    Returns:
        Status message with list of discovered tools.
    """
    config = ServerConfig(
        name=name,
        command=command,
        args=args or [],
        env=env,
    )

    try:
        conn = await _pool.add_server(config)

        if conn.status.value == "connected":
            tools = conn.list_tools()
            tool_names = [t["name"] for t in tools]
            return json.dumps(
                {
                    "status": "connected",
                    "server": name,
                    "tool_count": len(tools),
                    "tools": tool_names,
                },
                indent=2,
            )
        else:
            return json.dumps(
                {
                    "status": conn.status.value,
                    "server": name,
                    "error": conn.error or "Unknown error",
                },
                indent=2,
            )
    except Exception as e:
        logger.exception("Failed to add server '%s'", name)
        return json.dumps({"status": "error", "server": name, "error": str(e)}, indent=2)


@mcp.tool(
    name="mcp_remove_server",
    description=(
        "Disconnect and remove an external MCP server by name. "
        "Use mcp_list_servers to see connected servers."
    ),
)
async def mcp_remove_server(name: str) -> str:
    """Disconnect and remove an MCP server.

    Args:
        name: Server name to remove.
    """
    try:
        await _pool.remove_server(name)
        return json.dumps({"status": "removed", "server": name}, indent=2)
    except Exception as e:
        logger.exception("Failed to remove server '%s'", name)
        return json.dumps({"status": "error", "server": name, "error": str(e)}, indent=2)


@mcp.tool(
    name="mcp_list_servers",
    description="List all configured MCP servers with their connection status and tool counts.",
)
async def mcp_list_servers() -> str:
    """List all configured MCP servers."""
    servers = _pool.list_servers()
    if not servers:
        return "No MCP servers configured."
    return json.dumps(servers, indent=2)


@mcp.tool(
    name="mcp_list_tools",
    description=(
        "List all tools from connected MCP servers. "
        "Optionally filter by server name. "
        "Each tool's full_name includes the server prefix (e.g. 'filesystem__read_file')."
    ),
)
async def mcp_list_tools(server: str | None = None) -> str:
    """List tools from connected MCP servers.

    Args:
        server: Optional server name to filter by. If omitted, lists tools from all servers.
    """
    try:
        tools = _pool.list_all_tools(server_name=server)
        if not tools:
            if server:
                return f"No tools available from server '{server}'. Check if it is connected (use mcp_list_servers)."
            return "No tools available. Add and connect to MCP servers first (use mcp_add_server)."

        # Return a readable summary
        lines = [f"Tools ({len(tools)} total):", ""]
        for t in tools:
            lines.append(f"  [{t['server']}] {t['name']}")
            desc = t.get("description", "")
            if desc:
                # Truncate long descriptions
                if len(desc) > 100:
                    desc = desc[:97] + "..."
                lines.append(f"      {desc}")
        return "\n".join(lines)
    except Exception as e:
        logger.exception("Failed to list tools")
        return f"Error listing tools: {e}"


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
        "Reconnect to a server (or all servers) to refresh the tool list. "
        "Useful after a server is updated or restarted."
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
            return json.dumps({"status": "error", "server": server, "error": "Server not found"}, indent=2)

        config = conn.config
        await _pool.remove_server(server)
        new_conn = await _pool.add_server(config)

        return json.dumps(
            {
                "status": new_conn.status.value,
                "server": server,
                "tool_count": new_conn.tool_count,
            },
            indent=2,
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

        return json.dumps(results, indent=2)


# ── Entry point ──────────────────────────────────────────────────────


def main():
    """Run the slife-mcp wrapper server.

    Supports both stdio (for child-process mode) and HTTP (for standalone mode).
    Use --transport http --port 9876 for standalone mode.
    """
    import argparse

    parser = argparse.ArgumentParser(description="slife-mcp wrapper server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="Transport mode: stdio (child process) or http (standalone)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9876,
        help="HTTP port for standalone mode (default: 9876)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="HTTP host for standalone mode (default: 127.0.0.1)",
    )
    args = parser.parse_args()

    logger.info(
        "Starting slife-mcp wrapper server (transport=%s)...", args.transport
    )

    if args.transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
