"""Subagent lifecycle tools — local subagent management (native).

Local subagent workers are stdin/stdout processes managed by the
:class:`SubagentManager` — they need no MQTT broker.  Remote mesh peers
go through the a2a plugin (``a2a__*`` tools), which only exists when the
broker (Mosquitto) is running.

Tool inventory (9 tools)
------------------------
a2a_spawn_subagent       spawn a local worker
a2a_list_subagents       list local workers
a2a_stop_subagent        stop a local worker
a2a_send_task            send a task to a local worker (sync)
a2a_send_task_async      send a task to a local worker (fire-and-forget)
a2a_get_task_result      poll a local worker's async result
a2a_cancel_task          cancel a local worker's pending task
a2a_subscribe_task       wait for a local worker's task to complete
a2a_notify_user          desktop alert
"""

from __future__ import annotations

import logging
from typing import ClassVar

from slife.tools.base import Tool, require_params

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Subagent lifecycle
# ═══════════════════════════════════════════════════════════════════════════


class A2AListSubagentsTool(Tool):
    """List local subagents spawned by this instance."""

    name = "a2a_list_subagents"
    category = "A2A"
    description = (
        "List locally-spawned subagent workers (stdin/stdout IPC). "
        "Shows agent_id, PID, and readiness. "
        "Use a2a_list_agents for remote MQTT peers. "
        "Use before delegating tasks via a2a_send_task or a2a_send_task_async."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        from slife.subagent.process import get_manager

        manager = get_manager()
        if manager is None:
            import os as _os
            if _os.environ.get("SLIFE_SUBAGENT_NAME"):
                return (
                    "Subagent operations are not available inside a subagent "
                    "process (recursion guard)."
                )
            return (
                "Subagent manager is not running yet — it starts "
                "automatically when the agent service initializes."
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


# ═══════════════════════════════════════════════════════════════════════════
# Task management — asynchronous
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# Task listing (A2A tasks/list)
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# Task subscription (A2A tasks/subscribe)
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# Agent lifecycle
# ═══════════════════════════════════════════════════════════════════════════


def _serialize_cloned_context(ctx) -> list[dict] | None:
    """Return the parent conversation messages for a cloned subagent.

    The parent's messages are cloned as-is (the subagent rebuilds its own
    system prompt).  No upfront trimming: context overflow is handled by
    the loop's own ``_sys_trim`` on the first turn.  Returns None when no
    conversation is available.
    """
    conversation = getattr(ctx, "conversation", None)
    if conversation is None:
        return None
    # Drop the parent's system message (incl. its dynamic context footer).
    return [m for m in conversation.messages if m.get("role") != "system"]


class SubagentSpawnTool(Tool):
    """Spawn a new local subagent worker process."""

    name = "a2a_spawn_subagent"
    category = "A2A"
    description = (
        "Spawn a new local subagent — a copy of the current agent running "
        "in its own process with the same LLM config and tools. "
        "Use this to parallelize work: spawn multiple subagents, then send "
        "each a different task via a2a_send_task or a2a_send_task_async. "
        "Use a2a_list_subagents to see running workers."
    )
    parameters: ClassVar[dict] = {
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
            "context": {
                "type": "string",
                "enum": ["pure", "cloned"],
                "description": (
                    'Context for the subagent: "pure" (default, a fresh '
                    'independent context) or "cloned" (inherit a trimmed '
                    "copy of your current conversation)."
                ),
            },
        },
        "required": [],
    }

    async def execute(
        self, name: str = "", context: str = "pure", **kwargs,
    ) -> str:
        from slife.subagent.process import get_manager

        manager = get_manager()
        if manager is None:
            return (
                "Subagent manager is not running yet — it starts "
                "automatically when the agent service initializes."
            )

        context_source = context if context in ("pure", "cloned") else "pure"
        context_messages = (
            _serialize_cloned_context(getattr(self, "_ctx", None))
            if context_source == "cloned" else None
        )
        if context_source == "cloned" and not context_messages:
            logger.warning("subagent_clone_ctx_unavailable — falling back to pure")
            context_source = "pure"

        agent_name = name.strip() if name else None
        logger.info(
            "subagent_tool_spawn name=%s context=%s",
            agent_name or "<auto>", context_source,
        )

        try:
            agent_id = await manager.spawn(
                name=agent_name,
                context_source=context_source,
                context_messages=context_messages,
            )
            return (
                f"Subagent spawned successfully.\n"
                f"  Agent ID: {agent_id}\n"
                f"  Context: {context_source}\n"
                f"  Use a2a_list_subagents to see all local workers.\n"
                f'  Use a2a_send_task with agent_id="{agent_id}" to delegate work.'
            )
        except Exception as e:
            logger.error("subagent_tool_spawn_failed err=%s", e)
            return f"Error spawning subagent: {e}"


class SubagentStopTool(Tool):
    """Stop a locally-managed subagent process."""

    name = "a2a_stop_subagent"
    category = "A2A"
    description = (
        "Stop a locally-managed subagent process. "
        "Only subagents spawned by this instance can be stopped. "
        "Use a2a_list_subagents to see which agents are local subagents."
    )
    parameters: ClassVar[dict] = {
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
            import os as _os
            if _os.environ.get("SLIFE_SUBAGENT_NAME"):
                return (
                    "Cannot stop subagent: this is already a subagent "
                    "process — subagent management is only available in "
                    "the main agent."
                )
            return (
                "Subagent manager is not running yet — it starts "
                "automatically when the agent service initializes."
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


# ═══════════════════════════════════════════════════════════════════════════
# Messaging
# ═══════════════════════════════════════════════════════════════════════════


class A2ANotifyUserTool(Tool):
    """Push a desktop notification to the human operator."""

    name = "a2a_notify_user"
    category = "A2A"
    description = (
        "Send a desktop notification to the human user. "
        "Use this when a subagent or remote agent needs to alert the human "
        "operator — e.g. a long-running task completed, an error occurred, "
        "or attention is needed.  "
        "This is the primary way background agents communicate results to "
        "the user."
    )
    parameters: ClassVar[dict] = {
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

        # Fire desktop notification (best-effort, non-blocking).
        # Daemon thread: a hung notify backend must never block shutdown.
        from slife.platform import desktop_notify
        from slife.threads import run_daemon
        run_daemon(desktop_notify, title, message, name="desktop-notify")

        return f"Notification sent: [{title}] {message}"



# ═══════════════════════════════════════════════════════════════════════════
# Local subagent task routing (native — no MQTT/Mosquitto needed).
# Remote mesh peers go through the a2a plugin (a2a__* tools), which only
# exist when the broker (Mosquitto) is running.
# ═══════════════════════════════════════════════════════════════════════════


def _manager_or_hint() -> tuple:
    """Return (manager, "") or (None, error_hint)."""
    from slife.subagent.process import get_manager

    manager = get_manager()
    if manager is None:
        return None, (
            "Subagent manager is not running yet — it starts "
            "automatically when the agent service initializes."
        )
    return manager, ""


def _not_local_hint(agent_id: str) -> str:
    return (
        f"'{agent_id}' is not a local subagent. "
        "For local subagents use a2a_list_subagents first; for remote mesh "
        "peers use mqtt__send_task (requires the MQTT broker running)."
    )


class A2ASendTaskTool(Tool):
    """Send a task to a local subagent and wait for the result."""

    name = "a2a_send_task"
    category = "A2A"
    description = (
        "Send a task to a LOCAL subagent and wait for the result. "
        "Use a2a_list_subagents first.  For remote mesh peers, use "
        "mqtt__send_task (requires the MQTT broker running)."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Local subagent id."},
            "task": {"type": "string", "description": "Self-contained task for the subagent."},
        },
        "required": ["agent_id", "task"],
    }

    async def execute(self, agent_id: str = "", task: str = "", **kwargs) -> str:
        if err := require_params(agent_id=agent_id, task=task):
            return err
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint
        if agent_id not in manager.list():
            return _not_local_hint(agent_id)
        try:
            return await manager.send_task(agent_id, task)
        except TimeoutError:
            return f"Error: task to '{agent_id}' timed out."
        except Exception as e:
            return f"Error sending task to subagent '{agent_id}': {e}"


class A2ASendTaskAsyncTool(Tool):
    """Send a task to a local subagent without waiting."""

    name = "a2a_send_task_async"
    category = "A2A"
    description = (
        "Send a task to a LOCAL subagent without waiting — returns a task_id. "
        "Poll with a2a_get_task_result.  For remote mesh peers use "
        "mqtt__send_task_async."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Local subagent id."},
            "task": {"type": "string", "description": "Self-contained task for the subagent."},
        },
        "required": ["agent_id", "task"],
    }

    async def execute(self, agent_id: str = "", task: str = "", **kwargs) -> str:
        if err := require_params(agent_id=agent_id, task=task):
            return err
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint
        if agent_id not in manager.list():
            return _not_local_hint(agent_id)
        try:
            rpc_id = await manager.send_task_async(agent_id, task)
            return f"Task sent to '{agent_id}' (task_id: {rpc_id})."
        except Exception as e:
            return f"Error sending task to subagent '{agent_id}': {e}"


class A2AGetTaskResultTool(Tool):
    """Return the result of a local subagent async task."""

    name = "a2a_get_task_result"
    category = "A2A"
    description = (
        "Return the result of a LOCAL subagent async task, or 'pending' if "
        "not ready.  For remote mesh results use a2a__get_task_result."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Local subagent id."},
            "task_id": {"type": "string", "description": "The task_id from a2a_send_task_async."},
        },
        "required": ["agent_id", "task_id"],
    }

    async def execute(self, agent_id: str = "", task_id: str = "", **kwargs) -> str:
        if err := require_params(agent_id=agent_id, task_id=task_id):
            return err
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint
        if agent_id not in manager.list():
            return _not_local_hint(agent_id)
        result = manager.get_task_result(agent_id, task_id)
        return result if result is not None else "pending"


class A2ACancelTaskTool(Tool):
    """Cancel a pending local subagent task (best-effort)."""

    name = "a2a_cancel_task"
    category = "A2A"
    description = (
        "Cancel a pending LOCAL subagent task (best-effort — clears any "
        "stored result).  For remote mesh tasks use a2a__cancel_task."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Local subagent id."},
            "task_id": {"type": "string", "description": "The task_id to cancel."},
        },
        "required": ["agent_id", "task_id"],
    }

    async def execute(self, agent_id: str = "", task_id: str = "", **kwargs) -> str:
        if err := require_params(agent_id=agent_id, task_id=task_id):
            return err
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint
        if agent_id not in manager.list():
            return _not_local_hint(agent_id)
        # Local subagents: best-effort cancel — clear any stored result.
        if manager.get_task_result(agent_id, task_id) is not None:
            return f"Task '{task_id}' on agent '{agent_id}' cancelled."
        return (
            f"Task '{task_id}' not found on agent '{agent_id}'. "
            "It may have already completed or the task_id is incorrect."
        )


class A2ASubscribeTaskTool(Tool):
    """Wait for a local subagent async task to complete."""

    name = "a2a_subscribe_task"
    category = "A2A"
    description = (
        "Wait for a LOCAL subagent async task to complete and return its "
        "result (with a timeout).  For remote mesh tasks use "
        "a2a__subscribe_task."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Local subagent id."},
            "task_id": {"type": "string", "description": "The task_id from a2a_send_task_async."},
            "timeout": {"type": "number", "description": "Seconds to wait.", "default": 120.0},
        },
        "required": ["agent_id", "task_id"],
    }

    async def execute(self, agent_id: str = "", task_id: str = "", timeout: float = 120.0, **kwargs) -> str:
        if err := require_params(agent_id=agent_id, task_id=task_id):
            return err
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint
        if agent_id not in manager.list():
            return _not_local_hint(agent_id)
        try:
            result = await manager.subscribe_task(agent_id, task_id, timeout=timeout)
            return result if result is not None else "pending"
        except TimeoutError:
            return f"Error: task to '{agent_id}' timed out after {timeout}s."
        except Exception as e:
            return f"Error waiting for task on '{agent_id}': {e}"
