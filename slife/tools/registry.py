"""Tool registry for managing and executing tools."""

from slife.tools.base import Tool


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
            return f"Error: Unknown tool '{name}'"
        try:
            return await tool.execute(**kwargs)
        except Exception as e:
            return f"Error executing {name}: {e}"
