"""MCP on-demand tool loading — ``mcp_tool_load`` (legacy alias of ``tool_load``).

Kept registered so old callers and subagents keep working during the wrapper
retirement; executes by delegating to the unified :class:`~slife.tools.meta_tools.ToolLoadTool`.
The old ``__mcp_get_tool`` live-schema lookup is superseded by the shared
catalog (synced by the reconcile on connect).
"""

import logging

from slife.tools.base import Tool

logger = logging.getLogger(__name__)


class McpToolLoadTool(Tool):
    name = "mcp_tool_load"
    category = "mcp"
    description = (
        "Load an external MCP tool by full_name '{server}__{tool}' into the "
        "LLM's tool list (find it with mcp_tool_search)."
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
        """Delegate to the unified ``tool_load`` (mcp/rest-api materialization).

        The mcp-specific lookup (``__mcp_get_tool``) is superseded by the
        catalog: ``tool_load`` reads the tool's schema from the shared
        catalog row (synced by the reconcile on connect) and materializes the
        proxy from it.  This name stays registered for old callers and
        subagents during the wrapper retirement.
        """
        from slife.tools.meta_tools import ToolLoadTool

        full_name: str = kwargs.get("full_name", "")
        delegate = ToolLoadTool()
        object.__setattr__(delegate, "_ctx", getattr(self, "_ctx", None))
        return await delegate.execute(full_name=full_name)
