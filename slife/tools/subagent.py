"""Subagent tools — local worker lifecycle and task delegation.

A subagent (described as an *agent worker*) is a local child process
spawned by this agent — **not** an A2A peer.  It runs a full agent loop
with the same config and tools, but has no independent network identity:
it is invisible to the mesh and, when it does reach the mesh, it sends as
this agent (via the a2a plugin).

This module is fully decoupled from A2A:
  * lifecycle — ``spawn_subagent`` / ``list_subagents`` / ``stop_subagent``
  * delegation — ``subagent_send_task`` (sync wait),
    ``subagent_send_task_async`` (async, result auto-pushed to the parent),
    ``subagent_get_task_result`` (poll an async result)

There is deliberately no ``subagent_subscribe_task`` — async results are
auto-subscribed: when a worker finishes, the result is pushed to the
parent's conversation automatically.  ``subagent_cancel_task`` cancels a
queued or running worker task (drops the queued task, preempts the running
agent loop via the unified inbox).
"""

from __future__ import annotations

import logging
from typing import ClassVar

from slife.tools.base import Tool, make_params, require_params

logger = logging.getLogger(__name__)


def _manager_or_hint() -> tuple:
    """Return (manager, "") or (None, error_hint)."""
    from slife.subagent.process import get_manager

    manager = get_manager()
    if manager is None:
        # Subagents are full-fidelity workers and may spawn their own
        # descendants (each level has its own manager) — no subagent-specific
        # gate here. The manager only appears uninitialised mid-startup.
        return None, (
            "Subagent manager is not running yet — it starts automatically "
            "when the agent service initializes."
        )
    return manager, ""


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


class ListSubagentsTool(Tool):
    """List local subagent workers spawned by this instance."""

    name = "list_subagents"
    category = "Subagent"
    description = (
        "List local subagent workers with their state: agent_id, PID, "
        "readiness, context (pure/cloned), busy/in-flight count, and pending "
        "async task count. Local workers are not A2A peers — remote mesh "
        "peers use a2a_list_agents."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint

        agent_ids = manager.list()
        if not agent_ids:
            return (
                "No local subagents running. "
                "Use spawn_subagent to create one."
            )

        lines = [f"Local subagents ({len(agent_ids)}):"]
        for aid in sorted(agent_ids):
            p = manager.get(aid)
            pid = f" [pid={p.pid}]" if p and p.pid else ""
            ready = " [ready]" if p and p.is_ready else " [starting]"
            busy = f" [busy: {p.queued} in flight]" if p and p.is_busy else ""
            ctx = f" [context: {p.context_source}]" if p else ""
            async_n = p.pending_async_count if p else 0
            async_info = f" [async: {async_n}]" if async_n else ""
            lines.append(f"  - {aid}{pid}{ready}{ctx}{busy}{async_info}")
        return "\n".join(lines)


class SpawnSubagentTool(Tool):
    """Spawn a new local subagent worker process."""

    name = "spawn_subagent"
    category = "Subagent"
    description = (
        "Spawn a local subagent worker (agent worker — same LLM config and "
        "tools) to parallelize work; then delegate via subagent_send_task / "
        "subagent_send_task_async. Workers are local and not A2A peers."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    'Optional name for the worker (e.g. "researcher", '
                    '"coder-1"). If omitted, an auto-generated name like '
                    '"sub-1" is used.'
                ),
            },
            "context": {
                "type": "string",
                "enum": ["pure", "cloned"],
                "description": (
                    'Context for the worker: "pure" (default, a fresh '
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
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint

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
                f"  Use list_subagents to see all local workers.\n"
                f'  Use subagent_send_task with agent_id="{agent_id}" to delegate work.'
            )
        except Exception as e:
            logger.error("subagent_tool_spawn_failed err=%s", e)
            return f"Error spawning subagent: {e}"


class StopSubagentTool(Tool):
    """Stop a locally-managed subagent process."""

    name = "stop_subagent"
    category = "Subagent"
    description = (
        "Stop a locally-spawned subagent worker process. id from list_subagents."
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

        manager, hint = _manager_or_hint()
        if manager is None:
            return hint

        logger.info("subagent_tool_stop agent_id=%s", agent_id)

        ok = await manager.stop(agent_id)
        if ok:
            return (
                f"Subagent '{agent_id}' stopped successfully. "
                f"Use list_subagents to verify."
            )
        else:
            return (
                f"Subagent '{agent_id}' not found. "
                f"Use list_subagents to see managed subagents."
            )


class SubagentSendTaskTool(Tool):
    """Send a task to a local subagent worker and wait for the result.

    The worker processes one task at a time (serially).  If it is already
    busy, the task is NOT refused and the caller is NOT asked to resend —
    it is queued automatically as an async task and this is reported, so
    the result arrives via the auto-push later.
    """

    name = "subagent_send_task"
    category = "Subagent"
    description = (
        "Delegate a task to a local subagent worker and wait for the result. "
        "If the worker is busy, the task is queued and converted to async "
        "automatically (the result is delivered later). agent_id from "
        "spawn_subagent / list_subagents."
    )
    parameters: ClassVar[dict] = make_params(
        agent_id={"type": "string", "description": "agent_id of the local subagent worker."},
        task={"type": "string", "description": "Self-contained task for the worker."},
    )

    async def execute(self, agent_id: str = "", task: str = "", **kwargs) -> str:
        if err := require_params(agent_id=agent_id, task=task):
            return err
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint

        # The worker processes tasks serially.  When busy, queue the task as
        # async and tell the caller — never make it resend.
        if manager.is_busy(agent_id):
            n = manager.queued_count(agent_id)
            try:
                rpc_id = await manager.send_task_async(agent_id, task)
            except Exception as e:
                return f"Error queueing task to subagent '{agent_id}': {e}"
            return (
                f"Subagent '{agent_id}' is busy ({n} task(s) in flight) — the "
                f"task was queued and converted to async.\n"
                f"task_id: {rpc_id}\n"
                "The result will be delivered automatically when complete; "
                "poll with subagent_get_task_result "
                f"(agent_id={agent_id}, task_id={rpc_id})."
            )

        try:
            return await manager.send_task(agent_id, task)
        except TimeoutError:
            return (
                f"Timed out waiting for task to '{agent_id}' after the worker "
                "timeout. The task is still running on the worker — its result "
                "will be delivered automatically when complete."
            )
        except Exception as e:
            return f"Error sending task to subagent '{agent_id}': {e}"


class SubagentSendTaskAsyncTool(Tool):
    """Send a task to a local subagent worker without waiting.

    Returns a task_id.  The result is auto-subscribed: when the worker
    finishes it is pushed to this agent's conversation automatically; it
    can also be polled with ``subagent_get_task_result``.
    """

    name = "subagent_send_task_async"
    category = "Subagent"
    description = (
        "Delegate a task to a local subagent worker without waiting — returns "
        "a task_id. The result is delivered automatically when complete; poll "
        "with subagent_get_task_result. agent_id from spawn_subagent / list_subagents."
    )
    parameters: ClassVar[dict] = make_params(
        agent_id={"type": "string", "description": "agent_id of the local subagent worker."},
        task={"type": "string", "description": "Self-contained task for the worker."},
    )

    async def execute(self, agent_id: str = "", task: str = "", **kwargs) -> str:
        if err := require_params(agent_id=agent_id, task=task):
            return err
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint
        try:
            rpc_id = await manager.send_task_async(agent_id, task)
            return (
                f"Task sent to subagent '{agent_id}' (task_id: {rpc_id}). "
                "The result will be delivered automatically when complete."
            )
        except Exception as e:
            return f"Error sending task to subagent '{agent_id}': {e}"


class SubagentGetTaskResultTool(Tool):
    """Return the result of an async subagent task, or 'pending'."""

    name = "subagent_get_task_result"
    category = "Subagent"
    description = (
        "Return an async subagent task's result, or 'pending' if not ready. "
        "task_id comes from subagent_send_task_async."
    )
    parameters: ClassVar[dict] = make_params(
        agent_id={"type": "string", "description": "agent_id of the local subagent worker."},
        task_id={"type": "string", "description": "task_id from subagent_send_task_async."},
    )

    async def execute(self, agent_id: str = "", task_id: str = "", **kwargs) -> str:
        if err := require_params(agent_id=agent_id, task_id=task_id):
            return err
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint
        result = manager.get_task_result(agent_id, task_id)
        return result if result is not None else "pending"


class SubagentListTasksTool(Tool):
    """List worker task records (task management across subagents)."""

    name = "subagent_list_tasks"
    category = "Subagent"
    description = (
        "List worker task records across local subagents (task_id, worker, "
        "status, preview, result) — filterable by agent_id/status. Useful to "
        "track multiple async tasks sent via subagent_send_task_async."
    )
    parameters: ClassVar[dict] = make_params(
        agent_id={"type": "string", "description": "Optional worker agent_id to filter on.", "default": ""},
        status={"type": "string", "description": "Optional status filter (pending/completed/failed).", "default": ""},
    )

    async def execute(self, agent_id: str = "", status: str = "", **kwargs) -> str:
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint
        records = manager.list_tasks(
            agent_id=agent_id or None, status=status or None,
        )
        if not records:
            return "No subagent task records found."
        lines = [f"Subagent tasks ({len(records)}):"]
        for r in records:
            mode = r.get("mode", "sync")
            lines.append(
                f"  - {r['task_id']} [{r['agent_id']}] {mode}/{r['status']}: "
                f"{r['preview'][:60]}"
            )
        return "\n".join(lines)


class SubagentCancelTaskTool(Tool):
    """Cancel a pending/queued subagent task (best-effort)."""

    name = "subagent_cancel_task"
    category = "Subagent"
    description = (
        "Cancel a subagent task (task_id from subagent_send_task_async). "
        "Preempts a running task at the next safe point (like Esc) and "
        "drops a still-queued task; the worker moves on to the next task."
    )
    parameters: ClassVar[dict] = make_params(
        agent_id={"type": "string", "description": "agent_id of the local subagent worker."},
        task_id={"type": "string", "description": "task_id from subagent_send_task_async."},
    )

    async def execute(self, agent_id: str = "", task_id: str = "", **kwargs) -> str:
        if err := require_params(agent_id=agent_id, task_id=task_id):
            return err
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint
        try:
            cancelled = await manager.cancel_task(agent_id, task_id)
        except Exception as e:
            return f"Error cancelling task '{task_id}' on subagent '{agent_id}': {e}"
        if cancelled:
            return f"Task '{task_id}' on subagent '{agent_id}' cancelled."
        return (
            f"Task '{task_id}' not found on subagent '{agent_id}' or already "
            "completed/failed."
        )
