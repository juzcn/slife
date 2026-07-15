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
import os
import sys
from pathlib import Path

from typing import Literal

from fastmcp import FastMCP

from slife_mcp.connection import ConnectionPool, ServerConfig
from slife.server_utils import setup_server_logging, read_host_port_from_config

logger = logging.getLogger("slife_mcp")

_log_path = setup_server_logging("slife_mcp")

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
        "Connect to an external MCP server and make its tools available. "
        "Research the server's docs first — some servers (like anyapi-mcp-server) "
        "require user-provided flags (--spec, --base-url). ASK the user for "
        "these values; never pass empty strings for required args. "
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
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    description: str = "",
    activate: bool = True,
    source: dict | None = None,
) -> str:
    config = ServerConfig(
        name=name,
        command=command,
        args=args or [],
        env=env,
        description=description,
        active=activate,
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
        logger.exception("mcp_add_failed server=%s", name)
        return json.dumps({"status": "error", "server": name, "error": str(e)}, indent=2)


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
        return json.dumps({"status": "removed", "server": name}, indent=2)
    except Exception as e:
        logger.exception("mcp_remove_failed server=%s", name)
        return json.dumps({"status": "error", "server": name, "error": str(e)}, indent=2)


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
    return json.dumps(servers, indent=2)


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
            return json.dumps({"tools": [], "server": server, "note": f"No tools from server '{server}'."})

        return json.dumps({"tools": tools}, indent=2)
    except Exception as e:
        logger.exception("mcp_list_tools_failed")
        return json.dumps({"error": str(e)})


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
    return json.dumps(result, indent=2)


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
            return json.dumps(result, indent=2)
        else:
            conn = _pool.get_server(name)
            if conn is None:
                return json.dumps(
                    {"status": "error", "server": name, "error": f"Server '{name}' not found."},
                    indent=2,
                )
            return json.dumps(
                {
                    "status": "ok",
                    "server": name,
                    "disclosure": "lazy",
                    "tool_count": conn.tool_count,
                    "note": "Tools unregistered immediately. Server stays connected — switch back to eager to reload.",
                },
                indent=2,
            )
    except Exception as e:
        logger.exception("mcp_disclosure_failed server=%s", name)
        return json.dumps({"status": "error", "server": name, "error": str(e)}, indent=2)


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

    Auto-detects transport mode:
      - Piped stdin (slife child process) → stdio mode
      - Terminal → reads slife.json5 → HTTP mode

    Examples:
      python -m slife_mcp.server                           # auto-detect
      python -m slife_mcp.server --port 8888               # HTTP, override port
      python -m slife_mcp.server --host 0.0.0.0 --port 9876
    """
    import argparse

    parser = argparse.ArgumentParser(description="Slife-mcp wrapper server")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP port (overrides config)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="HTTP host (overrides config)",
    )
    args = parser.parse_args()

    logger.info("log_path=%s", _log_path)

    # Auto-detect: piped stdin → stdio (Slife child process), TTY → HTTP
    if not sys.stdin.isatty():
        logger.info("mcp_start transport=stdio")
        mcp.run(transport="stdio")
        return

    # Terminal mode — read host/port from slife.json5
    config_path = "slife.json5"
    if not Path(config_path).exists():
        logger.error(
            "slife.json5 not found. Either:\n"
            "  - Create slife.json5 with mcp.wrapper.url, or\n"
            "  - Use --host/--port to specify the HTTP endpoint."
        )
        sys.exit(1)

    cfg = read_host_port_from_config(config_path, config_key="mcp.wrapper", default_port=9876)
    if cfg is None:
        logger.error(
            "Cannot determine host/port. "
            "Set mcp.wrapper.url in slife.json5 or use --host/--port."
        )
        sys.exit(1)

    host = args.host if args.host is not None else cfg[0]
    port = args.port if args.port is not None else cfg[1]

    logger.info("mcp_start transport=http host=%s port=%s", host, port)
    mcp.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    main()
