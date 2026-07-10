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

    async def execute(self, name: str, **kwargs) -> str:
        """Execute a tool by name, with error handling.

        Returns:
            Tool result string, or error message string if tool not found
            or execution fails.
        """
        tool = self.get(name)
        if not tool:
            logger.warning("Tool not found: %s", name)
            return f"Error: Unknown tool '{name}'"
        try:
            logger.debug("Execute %s(%s)", name, kwargs)
            result = await tool.execute(**kwargs)
            logger.debug("Result  %s → %.200s", name, result)
            return result
        except Exception as e:
            logger.warning("Tool error: %s → %s", name, e)
            return f"Error executing {name}: {e}"
