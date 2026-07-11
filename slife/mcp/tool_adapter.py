"""MCP tool adapter — bridges MCP tools into slife's Tool interface.

Enables MCP tools (discovered via the slife-mcp wrapper) to be registered
in slife's ToolRegistry and called like native tools.
"""

import json
import logging

from slife.tools.base import Tool

logger = logging.getLogger(__name__)


class MCPProxyTool(Tool):
    """Adapts a single MCP tool to slife's Tool ABC.

    An instance represents one tool from a connected external MCP server,
    made available to the LLM via slife's standard tool system.

    The tool name is prefixed with the server name to avoid collisions
    (e.g. 'filesystem__read_file').

    Class-level attributes are placeholders — real values are set at
    instance level via object.__setattr__ for each instance.
    """

    # Placeholder class attrs to pass Tool.__init_subclass__ validation.
    # Real values are set per-instance in __init__.
    name = "_mcp_proxy"
    description = "MCP proxy tool (placeholder)"
    parameters: dict = {"type": "object", "properties": {}}

    def __init__(self, mcp_client, tool_info: dict):
        """
        Args:
            mcp_client: MCPClient instance connected to the slife-mcp wrapper.
            tool_info: Dict with server, name, description, inputSchema.
        """
        self._mcp_client = mcp_client
        self._server = tool_info["server"]
        self._tool_name = tool_info["name"]

        # Namespaced tool name: "server__toolname"
        full_name = f"{self._server}__{self._tool_name}"

        # Override class-level attrs at instance level with real values
        object.__setattr__(self, "name", full_name)

        desc = tool_info.get("description", "")
        server_prefix = f"[{self._server}] "
        object.__setattr__(self, "description", server_prefix + desc)

        schema = tool_info.get("inputSchema", {})
        # Ensure valid JSON Schema object type
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema = {
                "type": "object",
                "properties": schema.get("properties", {}),
                "required": schema.get("required", []),
            }
        object.__setattr__(self, "parameters", schema)

        logger.debug(
            "Created proxy tool: %s (server=%s, args=%d)",
            full_name,
            self._server,
            len(schema.get("properties", {})),
        )

    async def execute(self, **kwargs) -> str:
        """Execute the tool by calling through the MCP wrapper.

        Routes the call: slife → wrapper (mcp_call_tool) → external MCP server.
        """
        logger.debug(
            "MCP proxy: %s/%s(%s)",
            self._server,
            self._tool_name,
            kwargs,
        )
        result = await self._mcp_client.call_tool(
            "mcp_call_tool",
            {
                "server": self._server,
                "tool_name": self._tool_name,
                "arguments": json.dumps(kwargs, ensure_ascii=False),
            },
        )
        return result


def create_proxy_tools(
    mcp_client, tools: list[dict]
) -> list[MCPProxyTool]:
    """Create MCPProxyTool instances from a list of tool info dicts.

    Args:
        mcp_client: MCPClient instance.
        tools: List of tool info dicts, each with:
            server, name, description, inputSchema.

    Returns:
        List of MCPProxyTool instances ready for ToolRegistry registration.
    """
    return [MCPProxyTool(mcp_client, t) for t in tools]
