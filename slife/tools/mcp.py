"""MCP on-demand tool loading — ``mcp_tool_load``.

External MCP tools are on-demand by default (per-server ``auto_load``; see
``mcp-plugin``).  This is the single per-tool load entry: it fetches the
tool's live schema + enabled status from the mcp-plugin wrapper
(``__mcp_get_tool``) and registers an :class:`~slife.mcp.tool_adapter.MCPProxyTool`,
so the tool appears in the LLM's tool list on the next LLM call.

There is deliberately NO ``mcp_tool_unload`` — loaded tools are released at
server granularity (``mcp_remove`` / ``mcp_set_enabled``) or when a tool is
disabled (the reconcile in :meth:`slife.agent.service.AgentService._sync_mcp_proxies`
drops the proxy).
"""

import json
import logging

from slife.mcp.tool_adapter import create_proxy_tools
from slife.tools.base import Tool

logger = logging.getLogger(__name__)


class McpToolLoadTool(Tool):
    name = "mcp_tool_load"
    category = "mcp"
    description = (
        "Load an external MCP tool into the LLM's tool list by full_name "
        "'{server}__{tool}' (find it with mcp_tool_search). Tools of a disabled "
        "server are refused — enable the server first with mcp_set_enabled."
    )
    parameters = {
        "type": "object",
        "properties": {
            "full_name": {
                "type": "string",
                "description": (
                    "The tool's full name '{server}__{tool}', e.g. 'github__search'."
                ),
            },
        },
        "required": ["full_name"],
    }

    async def execute(self, **kwargs) -> str:
        full_name: str = kwargs.get("full_name", "")
        ctx = getattr(self, "_ctx", None)
        mcp = getattr(ctx, "mcp_client", None) if ctx is not None else None
        registry = getattr(ctx, "registry", None) if ctx is not None else None
        if mcp is None:
            return "Error: MCP gateway client not available."
        if registry is None:
            return "Error: tool registry not available."
        if not full_name:
            return "Error: full_name required."

        try:
            raw = await mcp.call_tool("__mcp_get_tool", {"full_name": full_name})
            data = json.loads(raw)
        except Exception as e:
            return f"Error: failed to look up '{full_name}': {e}"
        if data.get("status") != "ok":
            return raw
        if not data.get("enabled", True):
            return (
                f"Error: tool '{full_name}' is disabled (its server is "
                f"disabled). Enable it with mcp_set_enabled, then load again."
            )

        proxy = create_proxy_tools(mcp, [{
            "server": data["server"],
            "name": data["name"],
            "description": data.get("description", ""),
            "inputSchema": data.get("inputSchema", {"type": "object", "properties": {}}),
        }])[0]
        # ToolRegistry.register replaces an existing tool with the same name —
        # loading twice is idempotent.
        registry.register(proxy)
        logger.info("mcp_tool_loaded full_name=%s", full_name)
        return f"[OK] Loaded '{full_name}'."
