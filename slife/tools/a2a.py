"""A2A / subagent tools — complete A2A-protocol toolset for the LLM.

All tools are proper :class:`Tool` subclasses, auto-discovered by
``create_tools_from_config`` and always registered.  They use module-level
transport references (set by ``AgentService``) to reach the live
:class:`A2AClient` and :class:`SubagentManager` at call time.

Tool inventory (13 tools, A2A protocol aligned)
----------------------------------------------
A2A method              slife tool                status
message/send            a2a_send_task             full
message/stream          a2a_subscribe_task        streaming via MQTT
tasks/get               a2a_get_task_result       full (TaskRecord)
tasks/list              a2a_list_tasks            full (filterable)
tasks/cancel            a2a_cancel_task           full
tasks/subscribe         a2a_subscribe_task        blocking wait + poll
—                       a2a_list_agents           MQTT peers only
—                       a2a_list_subagents        local workers only
—                       a2a_spawn_subagent        agent lifecycle
—                       a2a_stop_subagent         agent lifecycle
—                       a2a_agent_card            agent introspection
—                       a2a_notify_user           desktop alert
—                       a2a_broadcast             scatter/gather

Note: a2a_send_task_async internally registers an event-driven Future
(local subagents) or subscribes to a reply topic (MQTT), so
a2a_subscribe_task waits without polling.  No separate push-
notification tool is needed.
"""

from __future__ import annotations

import asyncio
import logging
import os as _os
import platform as _platform
import subprocess as _subprocess
from typing import TYPE_CHECKING

from slife.tools.base import Tool

if TYPE_CHECKING:
    from slife.config import Config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

# Sentinel for "no manager / no client"
_NO_TRANSPORT_MSG = (
    "Agent '{agent_id}' not found. "
    "Use a2a_list_agents or a2a_list_subagents to see available agents."
)


def _require_params(**params: str) -> str | None:
    """Check that all named params are non-empty. Returns error string or None."""
    missing = [k for k, v in params.items() if not v]
    if missing:
        return f"Error: {' and '.join(missing)} required."
    return None


def _get_transports():
    """Return (manager, client) — the live transport references.

    Imports are lazy (inside each tool's execute) to avoid circular
    dependencies at module-load time.  This helper consolidates the
    repeated three-line stanza into one call.
    """
    from slife.a2a.client import get_client
    from slife.subagent.process import get_manager

    return get_manager(), get_client()


def _desktop_notify(title: str, message: str) -> None:
    """Fire a best-effort desktop notification (cross-platform)."""
    system = _platform.system()
    try:
        if system == "Windows":
            _subprocess.run(
                ["powershell", "-Command",
                 f"Add-Type -AssemblyName System.Windows.Forms; "
                 f"$n = New-Object System.Windows.Forms.NotifyIcon; "
                 f"$n.Icon = [System.Drawing.SystemIcons]::Information; "
                 f"$n.BalloonTipTitle = '{title}'; "
                 f"$n.BalloonTipText = '{message}'; "
                 f"$n.Visible = $true; "
                 f"$n.ShowBalloonTip(5000);"],
                capture_output=True, timeout=10,
            )
        elif system == "Darwin":
            _subprocess.run(
                ["osascript", "-e",
                 f'display notification "{message}" with title "{title}"'],
                capture_output=True, timeout=5,
            )
        else:
            _subprocess.run(
                ["notify-send", title, message],
                capture_output=True, timeout=5,
            )
    except Exception:
        # Desktop notification is best-effort — never let it fail the tool
        pass


# ═══════════════════════════════════════════════════════════════════════════
# Discovery
# ═══════════════════════════════════════════════════════════════════════════


class A2AListAgentsTool(Tool):
    """List remote agents on the MQTT P2P mesh — remote-only, not local subagents."""

    name = "a2a_list_agents"
    requires_a2a = True
    description = (
        "List remote agents discovered on the MQTT P2P mesh. "
        "Shows agent_id, display name, and status (idle/busy). "
        "This is *remote only* — it never includes local subagents. "
        "Use a2a_list_subagents for local workers. "
        "Use before delegating tasks via a2a_send_task or a2a_send_task_async."
    )
    parameters: dict = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        from slife.a2a.client import get_client

        client = get_client()
        if client is None:
            return (
                "A2A MQTT mesh is not active. "
                "Start slife with --agent <agent-name> to join the P2P mesh."
            )

        peers = await client.list_agents()

        lines = [f"Agents ({len(peers) + 1}):"]
        # Include self
        name = f" ({client._config.agent_name})" if client._config.agent_name else ""
        lines.append(f"  - {client.agent_id}{name} [{client.status}] (you)")
        for c in peers:
            name = f" ({c.display_name})" if c.display_name else ""
            lines.append(f"  - {c.agent_id}{name} [{c.status}]")
        return "\n".join(lines)


class A2AListSubagentsTool(Tool):
    """List local subagents spawned by this instance."""

    name = "a2a_list_subagents"
    description = (
        "List locally-spawned subagent workers (stdin/stdout IPC). "
        "Shows agent_id, PID, and readiness. "
        "Use a2a_list_agents for remote MQTT peers. "
        "Use before delegating tasks via a2a_send_task or a2a_send_task_async."
    )
    parameters: dict = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        from slife.subagent.process import get_manager

        manager = get_manager()
        if manager is None:
            return (
                "Subagent support is not enabled. "
                "Add a [subagent] section to slife.json5."
            )

        agent_ids = manager.list()
        if not agent_ids:
            return (
                "No local subagents running. "
                "Use a2a_spawn_subagent to create one."
            )

        lines = [f"Local subagents ({len(agent_ids)}):"]
        for aid in sorted(agent_ids):
            p = manager.get(aid)
            pid = f" [pid={p.pid}]" if p and p.pid else ""
            ready = " [ready]" if p and p.is_ready else " [starting]"
            lines.append(f"  - {aid}{pid}{ready}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Task management — synchronous
# ═══════════════════════════════════════════════════════════════════════════


class A2ASendTaskTool(Tool):
    """Send a task to any agent and wait for the result (synchronous)."""

    name = "a2a_send_task"
    description = (
        "Send a task to any agent and wait for the result. "
        "Works with local subagents and remote MQTT peers — routing is "
        "automatic.  Use a2a_list_agents and a2a_list_subagents first "
        "to discover available agents.  "
        "Be specific — the remote agent has no context of your conversation. "
        "For parallel work, use a2a_send_task_async to send without waiting."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Target agent id (from a2a_list_agents or a2a_list_subagents).",
            },
            "task": {
                "type": "string",
                "description": "Self-contained task description for the agent.",
            },
        },
        "required": ["agent_id", "task"],
    }

    async def execute(self, agent_id: str = "", task: str = "", **kwargs) -> str:
        if err := _require_params(agent_id=agent_id, task=task):
            return err

        from slife.a2a.identity import AgentId
        manager, client = _get_transports()

        # Route to local subagent first
        if manager is not None and agent_id in manager.list():
            try:
                return await manager.send_task(agent_id, task)
            except TimeoutError:
                return f"Error: task to '{agent_id}' timed out."
            except Exception as e:
                return f"Error sending task to subagent '{agent_id}': {e}"

        # Route to MQTT peer
        if client is not None:
            try:
                return await client.send_task(AgentId(agent_id), task)
            except TimeoutError:
                return f"Error: task to '{agent_id}' timed out."
            except Exception as e:
                return f"Error sending task to agent '{agent_id}': {e}"

        return _NO_TRANSPORT_MSG.format(agent_id=agent_id)


# ═══════════════════════════════════════════════════════════════════════════
# Task management — asynchronous
# ═══════════════════════════════════════════════════════════════════════════


class A2ASendTaskAsyncTool(Tool):
    """Send a task without waiting — fire-and-forget."""

    name = "a2a_send_task_async"
    description = (
        "Send a task to an agent without waiting for the result. "
        "Returns a task_id immediately. "
        "Use a2a_subscribe_task(task_id, agent_id) to wait for completion "
        "(event-driven for local subagents — no polling overhead). "
        "Alternatively use a2a_get_task_result for a one-shot status check. "
        "Works with local subagents and remote MQTT peers. "
        "Use a2a_list_agents and a2a_list_subagents first to discover agents."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Target agent id (from a2a_list_agents or a2a_list_subagents).",
            },
            "task": {
                "type": "string",
                "description": "Self-contained task description.",
            },
        },
        "required": ["agent_id", "task"],
    }

    async def execute(self, agent_id: str = "", task: str = "", **kwargs) -> str:
        if err := _require_params(agent_id=agent_id, task=task):
            return err

        from slife.a2a.identity import AgentId
        manager, client = _get_transports()

        # Route to local subagent first
        if manager is not None and agent_id in manager.list():
            try:
                rpc_id = await manager.send_task_async(agent_id, task)
                # Register event-driven Future so a2a_subscribe_task
                # returns immediately when the result arrives (no polling).
                try:
                    await manager.set_push_notification(
                        agent_id, rpc_id,
                        f"slife:local:notify:{rpc_id}",
                    )
                except Exception:
                    pass  # notification setup is best-effort
                return (
                    f"Task sent asynchronously.\n"
                    f"  Task ID: {rpc_id}\n"
                    f"  Agent: {agent_id}\n"
                    f'  Use a2a_subscribe_task with task_id="{rpc_id}" '
                    f'and agent_id="{agent_id}" to wait for the result.'
                )
            except Exception as e:
                return f"Error sending async task to subagent '{agent_id}': {e}"

        # Route to MQTT peer
        if client is not None:
            try:
                corr_id = await client.send_task_async(AgentId(agent_id), task)
                # MQTT results arrive via the reply_to topic automatically —
                # no extra notification setup needed.
                return (
                    f"Task sent asynchronously.\n"
                    f"  Task ID: {corr_id}\n"
                    f"  Agent: {agent_id} (MQTT)\n"
                    f'  Use a2a_subscribe_task with task_id="{corr_id}" '
                    f'and agent_id="{agent_id}" to wait for the result.'
                )
            except Exception as e:
                return f"Error sending async task to agent '{agent_id}': {e}"

        return _NO_TRANSPORT_MSG.format(agent_id=agent_id)


class A2AGetTaskResultTool(Tool):
    """Poll for the result of an asynchronous task.

    Returns the full task record (status, timestamps, result) when available.
    Uses the shared TaskStore so results survive across calls.
    """

    name = "a2a_get_task_result"
    description = (
        "Check the status and result of a task once (non-blocking). "
        "Returns the task record including status (pending/completed/failed/cancelled), "
        "timestamps, and result text. "
        "Use after a2a_send_task_async to check if a task is done without waiting. "
        "To wait for completion, use a2a_subscribe_task instead. "
        "For a list of all tasks, use a2a_list_tasks."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "The agent the task was sent to.",
            },
            "task_id": {
                "type": "string",
                "description": "The task_id returned by a2a_send_task_async.",
            },
        },
        "required": ["agent_id", "task_id"],
    }

    async def execute(self, agent_id: str = "", task_id: str = "", **kwargs) -> str:
        if err := _require_params(agent_id=agent_id, task_id=task_id):
            return err

        from slife.a2a.identity import AgentId
        from slife.a2a.task_store import get_store

        manager, client = _get_transports()
        store = get_store()

        # Consume result from both transports (updates task store as side effect).
        # Order: subagent first (fast, local), then MQTT (network).
        if manager is not None:
            manager.get_task_result(agent_id, task_id)

        if client is not None:
            client.get_task_result(task_id)

        # Read full record from task store
        rec = store.get(task_id)
        if rec is None:
            # Task unknown — check if agent exists
            agent_exists = False
            if manager is not None and agent_id in manager.list():
                agent_exists = True
            if client is not None and client.get_agent_card(AgentId(agent_id)) is not None:
                agent_exists = True
            if agent_exists:
                return (
                    f"Task '{task_id}' not found. "
                    f"It may have been pruned from the task store. "
                    f"Use a2a_list_tasks to see active tasks."
                )
            return f"Agent '{agent_id}' not found. Use a2a_list_agents or a2a_list_subagents."

        # Format full task record
        status_emoji = {"pending": "⏳", "completed": "✅", "failed": "❌", "cancelled": "🚫"}
        emoji = status_emoji.get(rec.status, "❓")
        lines = [
            f"{emoji} Task: {rec.task_id}",
            f"  Status: {rec.status.upper()}",
            f"  Agent: {rec.agent_id}",
            f"  Transport: {rec.transport}",
            f"  Task: {rec.task_preview}",
        ]
        if rec.completed_at is not None:
            elapsed = rec.completed_at - rec.created_at
            lines.append(f"  Duration: {elapsed:.1f}s")
        if rec.result is not None:
            lines.append(f"  Result:\n{rec.result}")
        elif rec.status == "pending":
            lines.append("  Result: (still pending — use a2a_subscribe_task to wait)")

        return "\n".join(lines)


class A2ACancelTaskTool(Tool):
    """Cancel a pending or in-flight task."""

    name = "a2a_cancel_task"
    description = (
        "Cancel a pending task (synchronous or asynchronous). "
        "Sends a cancellation notice to the target agent. "
        "Returns whether the task was found and cancelled locally. "
        "Note: the remote agent may still complete the task — cancellation "
        "is best-effort."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "The agent the task was sent to.",
            },
            "task_id": {
                "type": "string",
                "description": "The task_id to cancel (from a2a_send_task_async, or correlation_id from a2a_send_task).",
            },
        },
        "required": ["agent_id", "task_id"],
    }

    async def execute(self, agent_id: str = "", task_id: str = "", **kwargs) -> str:
        if err := _require_params(agent_id=agent_id, task_id=task_id):
            return err

        from slife.a2a.identity import AgentId
        manager, client = _get_transports()
        cancelled = False

        # Try local subagent
        if manager is not None:
            # For subagents, just clear any stored result
            result = manager.get_task_result(agent_id, task_id)
            if result is not None:
                cancelled = True

        # Try MQTT client
        if client is not None and not cancelled:
            try:
                cancelled = await client.cancel_task(AgentId(agent_id), task_id)
            except Exception as e:
                return f"Error cancelling task: {e}"

        if cancelled:
            return f"Task '{task_id}' on agent '{agent_id}' cancelled."
        return (
            f"Task '{task_id}' not found on agent '{agent_id}'. "
            f"It may have already completed or the task_id is incorrect."
        )


# ═══════════════════════════════════════════════════════════════════════════
# Task listing (A2A tasks/list)
# ═══════════════════════════════════════════════════════════════════════════


class A2AListTasksTool(Tool):
    """List tasks with optional status and agent filters — A2A tasks/list."""

    name = "a2a_list_tasks"
    description = (
        "List tasks across all agents, with optional filters. "
        "Supports filtering by agent_id, status (pending/completed/failed/cancelled), "
        "and transport (mqtt/subagent). "
        "Returns task records sorted newest-first, with task_id, status, "
        "agent, transport, and preview. "
        "Use this to monitor task progress across your agent fleet."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Filter by target agent id (optional).",
            },
            "status": {
                "type": "string",
                "description": "Filter by status: pending, completed, failed, cancelled (optional).",
            },
            "transport": {
                "type": "string",
                "description": "Filter by transport: mqtt or subagent (optional).",
            },
        },
        "required": [],
    }

    async def execute(
        self, agent_id: str = "", status: str = "", transport: str = "", **kwargs,
    ) -> str:
        from slife.a2a.task_store import get_store

        store = get_store()

        filters = {}
        if agent_id.strip():
            filters["agent_id"] = agent_id.strip()
        if status.strip():
            filters["status"] = status.strip().lower()
        if transport.strip():
            filters["transport"] = transport.strip().lower()

        records = store.list_tasks(**filters, limit=50)
        counts = store.count_by_status()

        if not records:
            filter_desc = ", ".join(f"{k}={v}" for k, v in filters.items())
            prefix = f"No tasks found" + (f" matching {filter_desc}" if filter_desc else "") + "."
            if counts:
                prefix += f" Task summary: {counts}"
            return prefix

        lines = [f"Tasks ({len(records)} shown)"]
        if counts:
            summary = " | ".join(f"{s}: {c}" for s, c in sorted(counts.items()))
            lines.append(f"Summary: {summary}")
        lines.append("")

        status_icons = {
            "pending": "⏳", "completed": "✅",
            "failed": "❌", "cancelled": "🚫",
        }
        for r in records:
            icon = status_icons.get(r.status, "❓")
            age = f"{r.completed_at - r.created_at:.1f}s" if r.completed_at else "…"
            lines.append(
                f"  {icon} {r.task_id} [{r.status.upper():10s}] "
                f"→ {r.agent_id} ({r.transport}) "
                f"「{r.task_preview[:60]}」 {age}"
            )

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Task subscription (A2A tasks/subscribe)
# ═══════════════════════════════════════════════════════════════════════════


class A2ASubscribeTaskTool(Tool):
    """Subscribe to task completion — wait for a task to finish."""

    name = "a2a_subscribe_task"
    description = (
        "Wait for an async task to complete. "
        "Blocks until the task finishes (completed, failed, or cancelled) "
        "or the timeout is reached. "
        "Use after a2a_send_task_async to wait for the result. "
        "For local subagents: event-driven (no polling — returns as soon "
        "as the result arrives). "
        "For MQTT peers: waits for reply via the result topic. "
        "Prefer this over polling with a2a_get_task_result."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "The agent the task was sent to.",
            },
            "task_id": {
                "type": "string",
                "description": "The task_id to subscribe to.",
            },
            "timeout": {
                "type": "number",
                "description": "Max seconds to wait (default 120).",
            },
        },
        "required": ["agent_id", "task_id"],
    }

    async def execute(
        self, agent_id: str = "", task_id: str = "", timeout: float = 120.0, **kwargs,
    ) -> str:
        if err := _require_params(agent_id=agent_id, task_id=task_id):
            return err

        from slife.a2a.identity import AgentId
        from slife.a2a.task_store import get_store

        manager, client = _get_transports()
        store = get_store()

        # Check if already completed
        rec = store.get(task_id)
        if rec is not None and rec.status in ("completed", "failed", "cancelled"):
            return (
                f"Task already {rec.status}.\n"
                f"  Task ID: {task_id}\n"
                f"  Result: {rec.result or '(no result)'}"
            )

        # Try subscribing via transport
        result = None

        # Local subagent
        if manager is not None and agent_id in manager.list():
            try:
                result = await manager.subscribe_task(agent_id, task_id, timeout)
            except TimeoutError:
                return f"Subscribe timed out after {timeout}s. Task '{task_id}' is still pending."
            except Exception as e:
                return f"Error subscribing to task: {e}"

        # MQTT peer
        if result is None and client is not None:
            try:
                result = await client.subscribe_task(task_id, timeout)
            except TimeoutError:
                return f"Subscribe timed out after {timeout}s. Task '{task_id}' is still pending."
            except Exception as e:
                return f"Error subscribing to task: {e}"

        if result is not None:
            return f"Task completed.\n  Task ID: {task_id}\n  Result: {result}"

        # Fallback: wait via polling the task store
        import asyncio as _asyncio
        import time as _time
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            rec = store.get(task_id)
            if rec is not None and rec.status in ("completed", "failed", "cancelled"):
                return (
                    f"Task {rec.status}.\n"
                    f"  Task ID: {task_id}\n"
                    f"  Result: {rec.result or '(no result)'}"
                )
            await _asyncio.sleep(0.5)

        return f"Subscribe timed out after {timeout}s. Task '{task_id}' is still pending."


# ═══════════════════════════════════════════════════════════════════════════
# Agent lifecycle
# ═══════════════════════════════════════════════════════════════════════════


class SubagentSpawnTool(Tool):
    """Spawn a new local subagent worker process."""

    name = "a2a_spawn_subagent"
    description = (
        "Spawn a new local subagent — a copy of the current agent running "
        "in its own process with the same LLM config and tools. "
        "Use this to parallelize work: spawn multiple subagents, then send "
        "each a different task via a2a_send_task or a2a_send_task_async. "
        "Use a2a_list_subagents to see running workers."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    'Optional name for the subagent (e.g. "researcher", '
                    '"coder-1"). If omitted, an auto-generated name like '
                    '"sub-1" is used.'
                ),
            },
        },
        "required": [],
    }

    async def execute(self, name: str = "", **kwargs) -> str:
        from slife.subagent.process import get_manager

        manager = get_manager()
        if manager is None:
            return (
                "Error: Subagent support is not enabled. "
                "Add a [subagent] section to slife.json5."
            )

        agent_name = name.strip() if name else None
        logger.info("subagent_tool_spawn name=%s", agent_name or "<auto>")

        try:
            agent_id = await manager.spawn(name=agent_name)
            return (
                f"Subagent spawned successfully.\n"
                f"  Agent ID: {agent_id}\n"
                f"  Use a2a_list_subagents to see all local workers.\n"
                f'  Use a2a_send_task with agent_id="{agent_id}" to delegate work.'
            )
        except Exception as e:
            logger.error("subagent_tool_spawn_failed err=%s", e)
            return f"Error spawning subagent: {e}"


class SubagentStopTool(Tool):
    """Stop a locally-managed subagent process."""

    name = "a2a_stop_subagent"
    description = (
        "Stop a locally-managed subagent process. "
        "Only subagents spawned by this instance can be stopped. "
        "Use a2a_list_subagents to see which agents are local subagents."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "The agent_id of the subagent to stop.",
            },
        },
        "required": ["agent_id"],
    }

    async def execute(self, agent_id: str = "", **kwargs) -> str:
        if not agent_id:
            return "Error: agent_id is required."

        from slife.subagent.process import get_manager

        manager = get_manager()
        if manager is None:
            return (
                "Error: Subagent support is not enabled. "
                "Add a [subagent] section to slife.json5."
            )

        logger.info("subagent_tool_stop agent_id=%s", agent_id)

        ok = await manager.stop(agent_id)
        if ok:
            return (
                f"Subagent '{agent_id}' stopped successfully. "
                f"Use a2a_list_subagents to verify."
            )
        else:
            return (
                f"Subagent '{agent_id}' not found. "
                f"Use a2a_list_subagents to see managed subagents. "
                f"Only locally-managed subagents can be stopped."
            )


# ═══════════════════════════════════════════════════════════════════════════
# Introspection
# ═══════════════════════════════════════════════════════════════════════════


class A2AGetAgentCardTool(Tool):
    """Get the detailed card of a specific agent."""

    name = "a2a_agent_card"
    description = (
        "Get the detailed Agent Card for a specific agent. "
        "Shows agent_id, display name, status (idle/busy), and whether "
        "the agent is a local subagent or remote MQTT peer. "
        "Use this to check if an agent is idle before sending a task."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "The agent_id to look up.",
            },
        },
        "required": ["agent_id"],
    }

    async def execute(self, agent_id: str = "", **kwargs) -> str:
        if not agent_id:
            return "Error: agent_id is required."

        from slife.a2a.identity import AgentId
        manager, client = _get_transports()

        # Check local subagent first
        if manager is not None and agent_id in manager.list():
            p = manager.get(agent_id)
            pid = f"pid={p.pid}" if p and p.pid else "unknown"
            ready = "ready" if p and p.is_ready else "starting"
            return (
                f"Agent Card — {agent_id}\n"
                f"  Type: local subagent\n"
                f"  Status: {ready}\n"
                f"  PID: {pid}\n"
                f"  Running: {p.is_running if p else 'unknown'}"
            )

        # Check MQTT peer
        if client is not None:
            card = client.get_agent_card(AgentId(agent_id))
            if card is not None:
                name = f" ({card.display_name})" if card.display_name else ""
                return (
                    f"Agent Card — {card.agent_id}\n"
                    f"  Type: MQTT remote peer\n"
                    f"  Display name: {card.display_name or '(none)'}\n"
                    f"  Status: {card.status}"
                )

        return (
            f"Agent '{agent_id}' not found. "
            f"Use a2a_list_agents and a2a_list_subagents to see available agents."
        )


# ═══════════════════════════════════════════════════════════════════════════
# Messaging
# ═══════════════════════════════════════════════════════════════════════════


class A2ANotifyUserTool(Tool):
    """Push a desktop notification to the human operator."""

    name = "a2a_notify_user"
    description = (
        "Send a desktop notification to the human user. "
        "Use this when a subagent or remote agent needs to alert the human "
        "operator — e.g. a long-running task completed, an error occurred, "
        "or attention is needed.  "
        "This is the primary way background agents communicate results to "
        "the user."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short notification title (e.g. 'Task Complete', 'Alert').",
            },
            "message": {
                "type": "string",
                "description": "The notification body — be concise (one sentence).",
            },
        },
        "required": ["message"],
    }

    async def execute(self, title: str = "slife", message: str = "", **kwargs) -> str:
        if not message:
            return "Error: message is required."

        # Log prominently
        logger.warning("USER_NOTIFICATION title=%s message=%s", title, message)

        # Fire desktop notification (best-effort, non-blocking)
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _desktop_notify, title, message)

        return f"Notification sent: [{title}] {message}"


class A2ABroadcastTool(Tool):
    """Broadcast a task to all known agents."""

    name = "a2a_broadcast"
    description = (
        "Broadcast a task to all known agents (local subagents AND remote "
        "MQTT peers).  Tasks are sent asynchronously (fire-and-forget). "
        "Returns a list of correlation IDs so you can poll for results "
        "with a2a_get_task_result.  "
        "Use this for scatter/gather patterns — send the same question to "
        "everyone and collect answers."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Self-contained task to broadcast to all agents.",
            },
        },
        "required": ["task"],
    }

    async def execute(self, task: str = "", **kwargs) -> str:
        if not task:
            return "Error: task is required."

        manager, client = _get_transports()
        all_ids: list[str] = []

        # Broadcast to local subagents
        if manager is not None:
            ids = await manager.broadcast(task)
            all_ids.extend(ids)

        # Broadcast to MQTT peers
        if client is not None:
            ids = await client.broadcast(task)
            all_ids.extend(ids)

        if not all_ids:
            return (
                "No agents available to broadcast to. "
                "Use a2a_spawn_subagent to create a local worker, "
                "or start slife with --agent <agent-name> to join the P2P mesh."
            )

        lines = [f"Broadcast sent to {len(all_ids)} agent(s):"]
        for cid in all_ids:
            lines.append(f"  - {cid}")
        lines.append("")
        lines.append(
            "Use a2a_get_task_result with each task_id to collect results."
        )
        return "\n".join(lines)
