"""Tool registry for managing and executing tools.

The registry is the EXECUTION-instance pool: every materialized ``Tool``
(a builtin created via ``from_config``, a plugin/mcp proxy holding a live
client) registers here.  The loaded/unloaded *state* lives in the shared
catalog (``slife.tools.catalog``) — this registry deliberately keeps no
second bookkeeping.  When a catalog is attached via :meth:`set_catalog`,
``execute`` consults it for the "known but not loaded" hint instead of the
bare ``Unknown tool`` string.
"""

import logging
import time as _time
from typing import TYPE_CHECKING, AbstractSet, Callable

from slife.tools.base import Tool
from slife.tools.catalog import EFF_ERROR
from slife.tools.whitelist import is_meta_tool

if TYPE_CHECKING:
    from slife.tools.catalog_service import ToolCatalogService

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry of available tools.

    Provides lookup, registration, and conversion to OpenAI function
    definitions for the LLM API.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        #: Optional shared catalog — drives the "known but not loaded" hint.
        self._catalog: "ToolCatalogService | None" = None
        #: Observers notified after any register/unregister mutation.  The
        #: slife-as-plugin host server subscribes here to push the standard
        #: MCP ``notifications/tools/list_changed`` to connected consumers —
        #: the registry stays the single live source of the tool list.
        self._on_change: list[Callable[[], None]] = []

    def set_catalog(self, catalog: "ToolCatalogService | None") -> None:
        """Attach the shared catalog (state) to this execution pool."""
        self._catalog = catalog

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
        not displace a live builtin tool or another plugin's tool, or the LLM's
        ``mcp_set`` would run the job.  ``server`` (the owning plugin / MCP
        server) is the owner key; builtin tools carry no ``server``.
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
        """Remove all tools whose name starts with prefix (e.g. 'github__').

        Returns the number of tools removed.
        """
        to_remove = [name for name in self._tools if name.startswith(prefix)]
        for name in to_remove:
            self.unregister(name)
        return len(to_remove)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name, or None if not found."""
        return self._tools.get(name)

    @staticmethod
    def _server_down_message(tool_name: str) -> str:
        """The refusal for a tool whose server is currently unusable.

        Both gates (registered-but-down, and known-to-the-catalog-only) answer
        with this, so the model is told what is actually wrong instead of
        being sent to ``func-tool-load``, which refuses for the same reason.
        """
        return (
            f"Error: tool '{tool_name}' is unavailable — its server is not up "
            f"right now (its tools are marked error). Check it with mcp_list "
            f"(or rest_api_list): it reconnects on its own, or re-enable it "
            f"with the matching *_set_enabled."
        )

    def list_tools(self) -> list[Tool]:
        """Return all registered tools."""
        return list(self._tools.values())

    def to_openai_functions(self, projection: AbstractSet[str] | None = None) -> list[dict]:
        """Convert registered tools to OpenAI function definitions.

        With ``projection`` (the per-turn loaded snapshot), only tools whose
        name is in the set are serialized; ``None`` keeps the historical
        all-registered behavior (used by ``host_server._sync_registry`` and
        tests).  The whitelist semantics live in the catalog service's
        snapshot, so a scheme-only caller doesn't need to know them.
        """
        if projection is None:
            return [t.to_openai_function() for t in self._tools.values()]
        return [
            t.to_openai_function()
            for t in self._tools.values() if t.name in projection
        ]

    async def execute(self, tool_name: str, /, **kwargs) -> str:
        """Execute a tool by name, with error handling.

        The tool_name parameter is positional-only (/) to prevent
        collisions with tool arguments that happen to share the name.

        When a catalog is attached, a known-but-unloaded tool gets an
        actionable hint instead of the bare ``Unknown tool`` string (the
        meta whitelist always executes).  With no catalog the historical
        exact behavior is kept.

        Returns:
            Tool result string, or error message string if tool not found
            or execution fails.
        """
        tool = self.get(tool_name)
        if not tool:
            if self._catalog is not None:
                eff = await self._catalog.effective_status(tool_name)
                if eff is not None:
                    if eff == EFF_ERROR:
                        logger.info("tool_server_down name=%s", tool_name)
                        return self._server_down_message(tool_name)
                    logger.info("tool_known_not_loaded name=%s eff=%s", tool_name, eff)
                    return (
                        f"Error: tool '{tool_name}' is known but not loaded — "
                        f"use tool_search + func-tool-load."
                    )
            logger.warning("tool_not_found name=%s", tool_name)
            return f"Error: Unknown tool '{tool_name}'"

        # Unloaded gate for materialized tools: config-disabled / server-down
        # / evicted tools must not silently execute — give the recovery hint
        # (except the meta whitelist, which always runs).
        if self._catalog is not None and not is_meta_tool(tool_name):
            eff = await self._catalog.effective_status(tool_name)
            if eff == EFF_ERROR:
                # Its server is down: saying "not loaded" would send the model
                # to func-tool-load, which refuses for the same unreachable
                # reason.
                logger.info("tool_server_down name=%s", tool_name)
                return self._server_down_message(tool_name)
            if eff is not None and eff != "loaded":
                logger.info("tool_unloaded_called name=%s eff=%s", tool_name, eff)
                return (
                    f"Error: tool '{tool_name}' is not loaded — "
                    f"use tool_search + func-tool-load."
                )
        try:
            t0 = _time.monotonic()
            logger.debug("tool_start name=%s", tool_name)
            result = await tool.execute(**kwargs)
            # Bump LRU recency on a real, completed use — the eviction policy
            # orders by last_loaded, so without this a just-used tool can be
            # the next eviction victim (alphabetical-NULL sort).
            if self._catalog is not None:
                await self._catalog.touch(tool_name)
            elapsed = (_time.monotonic() - t0) * 1000
            logger.debug(
                "tool_done name=%s took_ms=%.0f result_len=%d",
                tool_name, elapsed, len(result),
            )
            return result
        except Exception as e:
            logger.warning("tool_error name=%s err=%s", tool_name, e)
            return f"Error executing {tool_name}: {e}"
