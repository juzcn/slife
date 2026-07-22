"""list_native_tools — enumerate native Slife tools.

Gives the agent a reliable way to discover its own built-in
capabilities at runtime.  MCP-proxied tools (from external servers
like filesystem, fetch, duckduckgo-search, etc.) are reported in a
separate section so the agent can tell which tools are native and
which come from configured servers.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import ClassVar

from slife.tools.base import Tool

logger = logging.getLogger(__name__)

# ── Server name → display label for well-known built-in plugins ─────
_PLUGIN_LABELS: dict[str, str] = {
    "memory": "Memory (built-in plugin)",
    "wechat": "WeChat (built-in plugin)",
}


def _classify(name: str) -> str:
    """Map a native tool name to a category label."""
    if name.startswith("a2a_"):
        return "Agent Communication (A2A)"
    if name.startswith("mcp_"):
        return "MCP Server Management"
    if name.startswith("cli_"):
        return "CLI Tools"
    if name.startswith("config_env") or name.startswith("config_secret"):
        return "Configuration & Secrets"
    if name.startswith("add_skill") or name.startswith("remove_skill"):
        return "Skills"
    if name.startswith("system_") or name.startswith("get_os"):
        return "System"
    if name.startswith("execute_") or name.startswith("install_") or name.startswith("run_"):
        return "Code Execution"
    if name.startswith("list_"):
        return "Meta"
    if name.startswith("credential_") or name.startswith("inject_") or name.startswith("uninject_"):
        return "Credentials"
    return "Other"


class ListNativeToolsTool(Tool):
    """Enumerate native Slife tools, with a separate section for MCP servers."""

    name: ClassVar[str] = "list_native_tools"
    description: ClassVar[str] = (
        "List your built-in (native) tools, grouped by category. "
        "Also reports tools from connected MCP servers in a separate section "
        "so you can tell what is native vs what comes from external servers. "
        "Use when asked 'what tools do you have?' or 'what can you do?' — "
        "far more reliable than recalling tools from memory."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        from slife.tools.registry import get_registry
        from slife.mcp.tool_adapter import MCPProxyTool

        registry = get_registry()
        if registry is None:
            return "Tool registry is not available (called before initialization)."

        all_tools = registry.list_tools()
        if not all_tools:
            return "No tools are currently registered."

        # Split native vs MCP-proxied
        natives: list[Tool] = []
        mcp_proxies: dict[str, list[Tool]] = defaultdict(list)

        for t in all_tools:
            if isinstance(t, MCPProxyTool):
                mcp_proxies[t._server].append(t)
            else:
                natives.append(t)

        lines: list[str] = []

        # ── Native tools ────────────────────────────────────────────
        lines.append(f"## Native Tools ({len(natives)} total)\n")

        native_groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for t in sorted(natives, key=lambda t: t.name):
            category = _classify(t.name)
            desc = t.description.split(".")[0].strip() + "."
            native_groups[category].append((t.name, desc))

        for category in sorted(native_groups):
            items = native_groups[category]
            lines.append(f"### {category} ({len(items)})")
            for name, desc in items:
                lines.append(f"- **`{name}`** — {desc}")
            lines.append("")

        # ── MCP-proxied tools ──────────────────────────────────────
        if mcp_proxies:
            lines.append(f"## MCP-Connected Servers ({len(mcp_proxies)} servers)\n")
            for server in sorted(mcp_proxies):
                tools = mcp_proxies[server]
                label = _PLUGIN_LABELS.get(server, f"MCP: {server}")
                tool_names = sorted(t.name for t in tools)
                lines.append(
                    f"- **{label}** ({len(tools)} tools): "
                    + ", ".join(f"`{n}`" for n in tool_names)
                )
            lines.append("")

        return "\n".join(lines)
