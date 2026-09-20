"""Tool registry for managing and executing tools.

The registry is the EXECUTION-instance pool: every materialized ``Tool``
(a builtin created via ``from_config``, a plugin/mcp proxy holding a live
client) registers here.  The loaded/unloaded *state* lives in the shared
catalog (``slife.tools.catalog``) — this registry deliberately keeps no
second bookkeeping, and that state governs what a turn INJECTS, never what a
call may do: a name with an instance here runs whatever its row says.

When a catalog is attached via :meth:`set_catalog`, ``execute`` names a row's
state for a name that has NO instance here (``not loaded`` / ``disabled`` /
``error``) instead of the bare ``Unknown tool`` string.  That is the only
refusal left, because every other one would be policy about a tool that could
already run.
"""

import logging
import time as _time
from typing import TYPE_CHECKING, AbstractSet, Callable

from slife.tools.base import Tool, validate_args
from slife.tools.catalog import STATUS_DISABLED, STATUS_ERROR
from slife.tools.catalog_service import disabled_refusal, status_error_refusal

if TYPE_CHECKING:
    from slife.tools.catalog_service import ToolCatalogService

logger = logging.getLogger(__name__)


def not_loaded_refusal(tool_name: str) -> str:
    """The ONE refusal for a call to a name the catalog knows but the pool cannot run.

    It fires when there is no execution instance behind the name: a
    server-backed tool whose proxy was never materialized, or a row that
    remembers ``loaded`` from a previous session while this process — whose
    registry starts empty — has not been rejoined yet.  The row exists; the
    object to invoke does not.  Whether a load could create one is
    ``func-tool-load``'s answer to give, and it gives it.

    ``load_status`` is NOT this function's business.  A tool the model never
    loaded executes normally as long as an instance is registered: the row
    decides what a turn injects, never what a call may do.

    So: name the state and stop.  A remedy would be a guess about a cause this
    code has not checked — the same rule as :func:`status_error_refusal`.
    """
    return f"Error: tool '{tool_name}' is not loaded."


class ToolRegistry:
    """Registry of available tools.

    Provides lookup, registration, and conversion to OpenAI function
    definitions for the LLM API.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        #: Optional shared catalog — names the state of a name this pool cannot run.
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

        Nothing here gates on ``load_status``: that state decides what a turn
        injects, not what a call may do, so an unloaded tool with a registered
        instance runs like any other.  When a catalog is attached, a name with
        NO instance behind it gets its row's state named (``not loaded`` /
        ``disabled`` / ``error``) instead of the bare ``Unknown tool``
        string.  With no catalog the historical exact behavior is kept.

        Returns:
            Tool result string, or error message string if tool not found
            or execution fails.
        """
        tool = self.get(tool_name)
        if not tool:
            # No execution instance — the only thing left to refuse.  Refusal
            # texts live in the catalog service, one wording per state, so the
            # same situation reads the same here and at func-tool-load.
            if self._catalog is not None:
                eff = await self._catalog.effective_status(tool_name)
                if eff is not None:
                    if eff == STATUS_ERROR:
                        logger.info("tool_error_status name=%s", tool_name)
                        return status_error_refusal(tool_name, "called")
                    if eff == STATUS_DISABLED:
                        # Switched off in the config AND nothing registered for
                        # it: "not loaded" would send the model to load a tool
                        # the user has switched off.
                        logger.info("tool_disabled_called name=%s", tool_name)
                        return disabled_refusal(tool_name)
                    logger.info("tool_known_not_loaded name=%s eff=%s", tool_name, eff)
                    return not_loaded_refusal(tool_name)
            logger.warning("tool_not_found name=%s", tool_name)
            return f"Error: Unknown tool '{tool_name}'"

        # The tool's own schema is the contract for the call: a required
        # parameter that never arrived, or a name the tool does not declare
        # (a guessed `prompt` for `description`), is refused here rather than
        # silently absorbed by the tool's `**kwargs`.
        if err := validate_args(tool.parameters, tool_name, kwargs):
            logger.info("tool_args_invalid name=%s err=%s", tool_name, err)
            return err
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
