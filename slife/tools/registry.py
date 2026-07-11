"""Tool registry for managing and executing tools."""

import logging

from slife.tools.base import Tool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry of available tools.

    Provides lookup, registration, and conversion to OpenAI function
    definitions for the LLM API.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool instance."""
        self._tools[tool.name] = tool
        logger.info("Tool registered: %s", tool.name)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name, or None if not found."""
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        """Return all registered tools."""
        return list(self._tools.values())

    def to_openai_functions(self) -> list[dict]:
        """Convert all registered tools to OpenAI function definitions."""
        return [t.to_openai_function() for t in self._tools.values()]

    async def execute(self, tool_name: str, /, **kwargs) -> str:
        """Execute a tool by name, with error handling.

        The tool_name parameter is positional-only (/) to prevent
        collisions with tool arguments that happen to share the name.

        Returns:
            Tool result string, or error message string if tool not found
            or execution fails.
        """
        tool = self.get(tool_name)
        if not tool:
            logger.warning("Tool not found: %s", tool_name)
            return f"Error: Unknown tool '{tool_name}'"
        try:
            logger.debug("Execute %s(%s)", tool_name, kwargs)
            result = await tool.execute(**kwargs)
            logger.debug("Result  %s → %.200s", tool_name, result)
            return result
        except Exception as e:
            logger.warning("Tool error: %s → %s", tool_name, e)
            return f"Error executing {tool_name}: {e}"
