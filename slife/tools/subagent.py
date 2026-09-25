"""Subagent tools — local worker lifecycle and task delegation.

A subagent (described as an *agent worker*) is a local child process
spawned by this agent — **not** an A2A peer.  It runs a full agent loop
with the same config and tools, but has no independent network identity:
it is invisible to the mesh and, when it does reach the mesh, it sends as
this agent (via the a2a plugin).

This module is fully decoupled from A2A:
  * lifecycle — ``spawn_subagent`` / ``list_subagents`` / ``stop_subagent``
  * delegation — ``subagent_send_task`` (sync wait),
    ``subagent_send_task_async`` (async; mode=auto result auto-pushed to the
    parent, mode=poll not pushed),
    ``subagent_get_task_result`` (poll an async result — works for both
    modes: an auto-pushed result stays retrievable)

There is deliberately no ``subagent_subscribe_task`` — async results are
auto-subscribed: when a worker finishes, the result is pushed to the
parent's history automatically.  ``subagent_cancel_task`` cancels a
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
        return None, "Subagent manager is not running."
    return manager, ""


def _serialize_cloned_context(ctx) -> list[dict] | None:
    """Return the parent history messages for a cloned subagent.

    The parent's messages are cloned as-is (the subagent rebuilds its own
    system prompt) and repaired on arrival — the snapshot is taken *inside* the
    tool call that spawns the worker, so its last message is the
    ``assistant(tool_calls=…)`` whose results do not exist yet
    (``MessageHistory.from_history`` is the one place that invariant is
    restored).  No upfront trimming either: the worker's own window bound
    (``AgentLoop._trim_context`` at the request boundary) compacts the clone
    once it is over the ceiling.  Returns None when no history is available.
    """
    history = getattr(ctx, "message_history", None)
    if history is None:
        return None
    # Drop the parent's system message — the worker renders its own.
    return [m for m in history.messages if m.get("role") != "system"]


class ListSubagentsTool(Tool):
    """List local subagent workers spawned by this instance."""

    name = "list_subagents"
    category = "Subagent"
    description = (
        "List local subagent workers and their state (PID, readiness, "
        "context, busy/async counts)."
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

        subagent_names = manager.list()
        if not subagent_names:
            return (
                "No local subagents running. "
                "Use spawn_subagent to create one."
            )

        lines = [f"Local subagents ({len(subagent_names)}):"]
        for aid in sorted(subagent_names):
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
        "Spawn a local subagent worker (agent worker — same LLM config and tools)."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "subagent_name": {
                "type": "string",
                "description": (
                    'Worker name (e.g. "researcher") — its identity for '
                    "list_subagents / subagent_send_task."
                ),
            },
            "clone_context": {
                "type": "boolean",
                "default": False,
                "description": (
                    "True = start with the main agent's loaded turns; "
                    "false = clean (empty) context."
                ),
            },
        },
        "required": ["subagent_name"],
    }

    async def execute(
        self, subagent_name: str = "", clone_context: bool = False, **kwargs,
    ) -> str:
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint

        # The worker's name is its identity — never auto-generate an id.
        worker_name = subagent_name.strip()
        if err := require_params(
            subagent_name=worker_name,
            _hints={"subagent_name": 'give the worker a name (e.g. "researcher", "coder-1").'},
        ):
            return err

        context_source = "cloned" if clone_context else "clean"
        context_messages = (
            _serialize_cloned_context(getattr(self, "_ctx", None))
            if context_source == "cloned" else None
        )
        if context_source == "cloned" and not context_messages:
            logger.warning("subagent_clone_ctx_unavailable — falling back to clean")
            context_source = "clean"

        logger.info(
            "subagent_tool_spawn subagent_name=%s context=%s",
            worker_name, context_source,
        )

        try:
            # spawn() is idempotent for a running worker — check up-front so
            # the caller is told the worker was reused, not "spawned".
            existed = manager.spawned_running(worker_name)
            spawned = await manager.spawn(
                name=worker_name,
                context_source=context_source,
                context_messages=context_messages,
            )
            action = "already running — reused" if existed else "spawned"
            # A reused worker keeps the context it was STARTED with: the spawn
            # request is not applied to it.  Report the live value, never the
            # requested one — "Context: cloned" for a clean worker is worse
            # than useless, it is a fact the caller would act on.
            proc = manager.get(spawned)
            actual = proc.context_source if proc is not None else context_source
            reused_note = (
                f"  Context: {actual} (kept from its original spawn — a running "
                f"worker is not re-contexted)\n"
                if existed and actual != context_source
                else f"  Context: {actual}\n"
            )
            return (
                f"Subagent {action}.\n"
                f"  Subagent Name: {spawned}\n"
                f"{reused_note}"
                f"  Use list_subagents to see all local workers.\n"
                f'  Use subagent_send_task with subagent_name="{spawned}" to delegate work.'
            )
        except Exception as e:
            logger.error("subagent_tool_spawn_failed err=%s", e)
            return f"Error spawning subagent: {e}"


class StopSubagentTool(Tool):
    """Stop a locally-managed subagent process."""

    name = "stop_subagent"
    category = "Subagent"
    description = (
        "Stop a locally-spawned subagent worker process, dropping any task it "
        "still holds."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "subagent_name": {
                "type": "string",
                "description": "subagent_name of the worker to stop, from list_subagents.",
            },
        },
        "required": ["subagent_name"],
    }

    async def execute(self, subagent_name: str = "", **kwargs) -> str:
        if err := require_params(subagent_name=subagent_name):
            return err

        manager, hint = _manager_or_hint()
        if manager is None:
            return hint

        logger.info("subagent_tool_stop subagent_name=%s", subagent_name)

        ok = await manager.stop(subagent_name)
        if ok:
            return (
                f"Subagent '{subagent_name}' stopped successfully. "
                f"Use list_subagents to verify."
            )
        else:
            return (
                f"Subagent '{subagent_name}' not found. "
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
        "Send a task to a local subagent worker and wait for the result; "
        "if the worker is busy, queues it as async."
    )
    parameters: ClassVar[dict] = make_params(
        subagent_name={"type": "string", "description": "subagent_name of the local subagent worker."},
        task={"type": "string", "description": "Self-contained task for the worker."},
        timeout={
            "type": "integer",
            "description": "Worker task timeout in seconds. Omit to use the default (registry work.task_budget); only a positive integer overrides, 0/negative fall back to the default.",
            "default": 0,
        },
    )

    async def execute(self, subagent_name: str = "", task: str = "", **kwargs) -> str:
        if err := require_params(subagent_name=subagent_name, task=task):
            return err
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint
        # Per-call timeout override — only a positive int overrides; the LLM
        # omits the param to use the worker default (registry work.task_budget,
        # resolved at call time on the worker).  None / 0 / negative → default.
        _t = kwargs.get("timeout")
        timeout_override = _t if isinstance(_t, int) and not isinstance(_t, bool) and _t > 0 else None

        # The worker processes tasks serially.  When busy, queue the task as
        # async and tell the caller — never make it resend.
        if manager.is_busy(subagent_name):
            n = manager.queued_count(subagent_name)
            try:
                rpc_id = await manager.send_task_async(subagent_name, task)
            except Exception as e:
                return f"Error queueing task to subagent '{subagent_name}': {e}"
            return (
                f"Subagent '{subagent_name}' is busy ({n} task(s) in flight) — the "
                f"task was queued and converted to async.\n"
                f"task_id: {rpc_id}\n"
                "The result will be delivered automatically when complete; "
                "poll with subagent_get_task_result "
                f"(subagent_name={subagent_name}, task_id={rpc_id})."
            )

        try:
            return await manager.send_task(subagent_name, task, timeout=timeout_override)
        except TimeoutError as e:
            # The worker's TaskTimeout carries the task id (imported here, like
            # the manager: this module must not import the subagent package at
            # module level — the package's __init__ imports these tools).
            from slife.subagent.process import TaskTimeout

            if isinstance(e, TaskTimeout):
                # The id is what makes the timeout survivable: the worker was
                # preempted, so a reply may still arrive — and this is the only
                # place the caller is told which task to poll for it.
                return (
                    f"Timed out waiting for task to '{subagent_name}' after the "
                    "worker timeout. The task was preempted on the worker so it "
                    "does not block later tasks; its result, if one arrives, is "
                    "NOT pushed automatically — poll it with "
                    "subagent_get_task_result "
                    f"(subagent_name={subagent_name}, task_id={e.task_id})."
                )
            return (
                f"Timed out waiting for task to '{subagent_name}' after the worker "
                "timeout. The task was preempted on the worker so it does not "
                "block later tasks; its eventual result, if any, is NOT "
                "delivered automatically."
            )
        except Exception as e:
            return f"Error sending task to subagent '{subagent_name}': {e}"


class SubagentSendTaskAsyncTool(Tool):
    """Send a task to a local subagent worker without waiting.

    Returns a task_id.  Delivery of the result is chosen at send time:
    ``mode="auto"`` (default) pushes the result to this agent's
    history when the worker finishes — the result ALSO stays retrievable
    with ``subagent_get_task_result``; ``mode="poll"`` suppresses the
    push — the caller retrieves it with ``subagent_get_task_result``.
    """

    name = "subagent_send_task_async"
    category = "Subagent"
    description = (
        "Send a task to a local subagent worker without waiting — returns a "
        "task_id; mode 'auto' (default) auto-pushes the result and stays "
        "pollable, mode 'poll' disables auto-push (retrieve with "
        "subagent_get_task_result)."
    )
    parameters: ClassVar[dict] = make_params(
        subagent_name={"type": "string", "description": "subagent_name of the local subagent worker."},
        task={"type": "string", "description": "Self-contained task for the worker."},
        mode={
            "type": "string",
            "enum": ["auto", "poll"],
            "default": "auto",
            "description": "'auto' (default) auto-push the result (also pollable); 'poll' — no push, retrieve with subagent_get_task_result.",
        },
    )

    async def execute(
        self, subagent_name: str = "", task: str = "", mode: str = "auto", **kwargs,
    ) -> str:
        if err := require_params(subagent_name=subagent_name, task=task):
            return err
        if mode not in ("auto", "poll"):
            return f"Error: mode must be 'auto' or 'poll', got {mode!r}."
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint
        try:
            rpc_id = await manager.send_task_async(subagent_name, task, mode=mode)
            if mode == "poll":
                return (
                    f"Task sent to subagent '{subagent_name}' (task_id: {rpc_id}). "
                    "Auto-push disabled (mode=poll) — retrieve the result with "
                    "subagent_get_task_result."
                )
            return (
                f"Task sent to subagent '{subagent_name}' (task_id: {rpc_id}). "
                "Auto-push enabled (mode=auto): the result will be delivered "
                "automatically when complete — it can also be polled at any "
                "time with subagent_get_task_result."
            )
        except Exception as e:
            return f"Error sending task to subagent '{subagent_name}': {e}"


class SubagentGetTaskResultTool(Tool):
    """Return the result of a worker task, or 'pending' while it runs."""

    name = "subagent_get_task_result"
    category = "Subagent"
    description = (
        "Return a worker task's result. Reading does not consume it: the "
        "result stays retrievable for as long as the task is recorded."
    )
    parameters: ClassVar[dict] = make_params(
        subagent_name={"type": "string", "description": "subagent_name of the local subagent worker."},
        task_id={"type": "string", "description": "task_id from subagent_send_task_async (or from a timed-out subagent_send_task)."},
    )

    async def execute(self, subagent_name: str = "", task_id: str = "", **kwargs) -> str:
        if err := require_params(subagent_name=subagent_name, task_id=task_id):
            return err
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint
        state, result = manager.get_task_result(subagent_name, task_id)
        if state == "unknown":
            # Never "pending": a task that does not exist is not a task that is
            # still running, and the difference is what the caller acts on
            # (wait versus fix the id).
            return (
                f"No task '{task_id}' on subagent '{subagent_name}' — unknown id "
                "(check it, or list with subagent_list_tasks)."
            )
        if state == "pending":
            return "pending"
        return result or f"Task {state} (no result text recorded)."


class SubagentListTasksTool(Tool):
    """List worker task records (task management across subagents)."""

    name = "subagent_list_tasks"
    category = "Subagent"
    description = (
        "List worker task records across local subagents (task_id, worker, "
        "status, preview)."
    )
    parameters: ClassVar[dict] = make_params(
        subagent_name={"type": "string", "description": "Filter by worker name (omitted = all).", "default": ""},
        status={"type": "string", "description": "pending/completed/failed/cancelled", "default": ""},
    )

    async def execute(self, subagent_name: str = "", status: str = "", **kwargs) -> str:
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint
        records, total = manager.list_tasks(
            agent_name=subagent_name or None, status=status or None,
        )
        if not records:
            return "No subagent task records found."
        # Say how many are NOT shown — a truncated list presented as the total
        # is a fact the caller cannot check.
        shown = f"{len(records)}" if total == len(records) else f"{len(records)} of {total}"
        lines = [f"Subagent tasks ({shown}):"]
        for r in records:
            mode = r.get("mode", "sync")
            lines.append(
                f"  - {r['task_id']} [{r['agent_name']}] {mode}/{r['status']}: "
                f"{r['preview'][:60]}"
            )
        return "\n".join(lines)


class SubagentCancelTaskTool(Tool):
    """Cancel a pending/queued subagent task (best-effort)."""

    name = "subagent_cancel_task"
    category = "Subagent"
    description = (
        "Cancel a subagent task (queued or running)."
    )
    parameters: ClassVar[dict] = make_params(
        subagent_name={"type": "string", "description": "subagent_name of the local subagent worker."},
        task_id={"type": "string", "description": "task_id from subagent_send_task_async."},
    )

    async def execute(self, subagent_name: str = "", task_id: str = "", **kwargs) -> str:
        if err := require_params(subagent_name=subagent_name, task_id=task_id):
            return err
        manager, hint = _manager_or_hint()
        if manager is None:
            return hint
        try:
            cancelled = await manager.cancel_task(subagent_name, task_id)
        except Exception as e:
            return f"Error cancelling task '{task_id}' on subagent '{subagent_name}': {e}"
        if cancelled:
            return f"Task '{task_id}' on subagent '{subagent_name}' cancelled."
        return (
            f"Task '{task_id}' not found on subagent '{subagent_name}' or already "
            "completed/failed."
        )
