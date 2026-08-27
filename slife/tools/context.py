"""Tool context — typed container for runtime objects tools need.

Previously these were scattered module-level singletons.  All of them
are now fields on one object passed through :meth:`Tool.from_config()`
at startup — no global state, no implicit initialisation order.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slife.agent.message_history import MessageHistory
    from slife.config import Config
    from slife.tools.registry import ToolRegistry


@dataclass
class ToolContext:
    """Runtime objects that tools need but don't own.

    Created by :class:`~slife.agent.service.AgentService` at startup
    and threaded through :meth:`Tool.from_config()` to every registered
    tool.  Tools that need one of these objects store it in ``_ctx``
    and access fields directly — no more implicit global lookups.
    """

    registry: ToolRegistry | None = None
    """The live :class:`ToolRegistry` (needed by ``list_native_tools``, model
    switching, etc.)"""

    config: Config | None = None
    """The parsed :class:`Config` (needed by REST API / CLI tools, etc.)"""

    mcp_client: object | None = None
    """The slife-mcp wrapper client — used by ``rest_api_*`` tools for
    auto-connect and by ``check_mcp`` for live server status."""

    a2a_mcp_client: object | None = None
    """The mqtt plugin's MCP client — the A2A remote-mesh transport
    (needed by the unified ``a2a_*`` tools to reach remote peers via
    MQTT; local subagents go through the SubagentManager instead)."""

    sharefile_client: object | None = None
    """The sharefile plugin's MCP client — used by ``check_sharefile`` to
    query the file-sharing tunnel status via the plugin's internal tool."""

    memfiles_client: object | None = None
    """The memfiles plugin's MCP client — used by ``check_memfiles`` to
    query the cabinet status (store, index, semantic search) via the
    plugin's internal ``__cabinet_status`` tool."""

    message_history: MessageHistory | None = None
    """The active :class:`MessageHistory` (needed by ``clear_context``,
    ``attach_image`` and subagent context cloning)."""

    set_max_iterations: Callable[[int], str] | None = None
    """Runtime hook to change the agent loop's per-turn iteration cap
    (0 = unlimited).  Populated by AgentService after the loop is built;
    used by the ``set_max_iterations`` meta tool."""

    fire_schedule_now: Callable[[str], Awaitable[str]] | None = None
    """Run a scheduled task immediately (record a run + inject its trigger).
    Populated by AgentService (main agent only); used by the
    ``run_schedule_now`` tool to backfill a failed/missed run or fire a task
    now."""

    advance_context_start: Callable[[int], Awaitable[bool]] | None = None
    """Persist the live-context start boundary after a trim evicted
    *count* oldest turns, so restart rebuilds the exit-time context.
    Populated by AgentService; used by ``AgentLoop._trim_after_save``."""

    set_context_start_latest: Callable[[], Awaitable[bool]] | None = None
    """Flush the live-context boundary to the latest saved turn, so the
    next restore is a fresh start.  Populated by AgentService; used by
    ``clear_context``."""

    reset_context_time: Callable[[], None] | None = None
    """Clear the loop's tracked context time range so "Context covers"
    restarts from the fresh context.  Populated by AgentService; used by
    ``clear_context``."""
