"""Tool registry for managing and executing tools."""

import logging
import time as _time
from typing import TYPE_CHECKING

from slife.tools.base import Tool

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Module-level reference so meta-tools like list_native_tools can
# introspect the current registry without a circular dependency.
_current_registry: "ToolRegistry | None" = None


def get_registry() -> "ToolRegistry | None":
    """Return the live ToolRegistry, or None if not yet initialised."""
    return _current_registry


def set_registry(registry: "ToolRegistry") -> None:
    """Set the current ToolRegistry (called by the factory)."""
    global _current_registry
    _current_registry = registry


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

    def unregister(self, name: str) -> bool:
        """Remove a tool by name. Returns True if it existed."""
        if name in self._tools:
            del self._tools[name]
            logger.debug("tool_unregistered name=%s", name)
            return True
        return False

    def unregister_by_prefix(self, prefix: str) -> int:
        """Remove all tools whose name starts with prefix (e.g. 'anyapi__').

        Returns the number of tools removed.
        """
        to_remove = [name for name in self._tools if name.startswith(prefix)]
        for name in to_remove:
            self.unregister(name)
        return len(to_remove)

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
            logger.warning("tool_not_found name=%s", tool_name)
            return f"Error: Unknown tool '{tool_name}'"
        try:
            t0 = _time.monotonic()
            result = await tool.execute(**kwargs)
            elapsed = (_time.monotonic() - t0) * 1000
            logger.debug(
                "tool_done name=%s took_ms=%.0f result_len=%d",
                tool_name, elapsed, len(result),
            )
            return result
        except Exception as e:
            logger.warning("tool_error name=%s err=%s", tool_name, e)
            return f"Error executing {tool_name}: {e}"
