"""Tool registry for managing and executing tools."""

import logging
import time as _time
from typing import Callable

from slife.tools.base import Tool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry of available tools.

    Provides lookup, registration, and conversion to OpenAI function
    definitions for the LLM API.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        #: Observers notified after any register/unregister mutation.  The
        #: slife-as-plugin host server subscribes here to push the standard
        #: MCP ``notifications/tools/list_changed`` to connected consumers —
        #: the registry stays the single live source of the tool list.
        self._on_change: list[Callable[[], None]] = []

    def add_change_listener(self, listener: Callable[[], None]) -> None:
        """Register *listener*, called after every tool-set mutation."""
        self._on_change.append(listener)

    def _notify_changed(self) -> None:
        for cb in list(self._on_change):
            try:
                cb()
            except Exception:
                logger.debug("tool_change_listener_error", exc_info=True)

    def register(self, tool: Tool) -> None:
        """Register a tool instance.

        Same-owner re-registration replaces (a plugin reconnect, an idempotent
        re-load).  A tool from a DIFFERENT origin is never silently replaced:
        a bare-named plugin tool (e.g. a job function named ``mcp_set``) must
        not displace a live native tool or another plugin's tool, or the LLM's
        ``mcp_set`` would run the job.  ``server`` (the owning plugin / MCP
        server) is the owner key; native tools carry no ``server``.
        """
        existing = self._tools.get(tool.name)
        if existing is not None and existing is not tool:
            if getattr(existing, "server", None) != getattr(tool, "server", None):
                logger.error(
                    "tool_register_collision name=%s existing_owner=%r new_owner=%r "
                    "— refusing to replace a differently-owned tool",
                    tool.name,
                    getattr(existing, "server", None),
                    getattr(tool, "server", None),
                )
                return
            logger.warning(
                "tool_register_duplicate name=%s — replacing existing tool", tool.name,
            )
        self._tools[tool.name] = tool
        self._notify_changed()

    def unregister(self, name: str) -> bool:
        """Remove a tool by name. Returns True if it existed."""
        if name in self._tools:
            del self._tools[name]
            logger.debug("tool_unregistered name=%s", name)
            self._notify_changed()
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
            logger.debug("tool_start name=%s", tool_name)
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
