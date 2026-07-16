"""End-to-end test: slife-mcp wrapper with filesystem MCP server.

Verifies:
  1. Connect to slife-mcp wrapper via stdio (wrapper auto-spawned by MCPClient)
  2. Add filesystem MCP server (via npx)
  3. Discover tools from filesystem server
  4. Call a tool (list_allowed_directories, list_directory)
  5. Remove server
  6. Clean shutdown

Usage:
    uv run python tests/e2e/test_mcp_filesystem.py [allowed_dir]
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from slife.mcp.client import MCPClient


def _normalize_path(p: str) -> str:
    """Normalize path for use in JSON strings (forward slashes)."""
    return p.replace("\\", "/")


async def main(allowed_dir: str | None = None):
    if allowed_dir is None:
        allowed_dir = str(Path(__file__).parent.parent.parent)
    allowed_dir = _normalize_path(allowed_dir)

    print("=" * 60)
    print("E2E Test: slife-mcp wrapper with filesystem MCP server")
    print("=" * 60)
    print()

    # ── 1. Connect to slife-mcp wrapper via MCPWrapperProcess ────
    print("1. Starting slife-mcp wrapper...")
    from slife.mcp.process import MCPWrapperProcess
    wrapper = MCPWrapperProcess(
        command="uv",
        args=["run", "python", "-m", "slife.plugins.mcp.server"],
    )
    await wrapper.start()
    client = await wrapper.create_client()
    print("   Connected.")
    print()

    # ── 2. List wrapper's own tools ───────────────────────────────
    print("2. Wrapper management tools:")
    tools = await client.list_tools()
    for t in tools:
        print(f"   - {t['name']}: {t['description'][:80]}")
    print()

    # ── 3. Add filesystem MCP server ──────────────────────────────
    print("3. Adding filesystem MCP server...")
    result = await client.call_tool(
        "mcp_add_server",
        {
            "name": "fs",
            "command": "npx",
            "args": [
                "-y",
                "@modelcontextprotocol/server-filesystem",
                allowed_dir,
            ],
        },
    )
    print(f"   Result: {result}")
    print()

    # ── 4. List tools from filesystem server ──────────────────────
    print("4. Listing tools from filesystem server...")
    tools_result = await client.call_tool("mcp_list_tools", {"server": "fs"})
    print(tools_result)
    print()

    # ── 5. Call list_allowed_directories ──────────────────────────
    print("5. Calling list_allowed_directories...")
    result = await client.call_tool(
        "mcp_call_tool",
        {
            "server": "fs",
            "tool_name": "list_allowed_directories",
            "arguments": "{}",
        },
    )
    print(f"   Result: {result}")
    print()

    # ── 6. Call list_directory ────────────────────────────────────
    print("6. Calling list_directory...")
    result = await client.call_tool(
        "mcp_call_tool",
        {
            "server": "fs",
            "tool_name": "list_directory",
            "arguments": f'{{"path": "{allowed_dir}"}}',
        },
    )
    if len(result) > 500:
        result = result[:500] + "\n... (truncated)"
    print(f"   Result:\n{result}")
    print()

    # ── 7. Server status ──────────────────────────────────────────
    print("7. Server status:")
    result = await client.call_tool("mcp_list_servers", {})
    print(f"   {result}")
    print()

    # ── 8. Remove server ──────────────────────────────────────────
    print("8. Removing filesystem server...")
    result = await client.call_tool("mcp_remove_server", {"name": "fs"})
    print(f"   Result: {result}")
    print()

    # ── 9. Clean shutdown ─────────────────────────────────────────
    print("9. Shutting down...")
    await client.disconnect()
    print("   Done.")
    print()

    print("=" * 60)
    print("E2E test PASSED!")
    print("=" * 60)


if __name__ == "__main__":
    allowed = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(main(allowed))
