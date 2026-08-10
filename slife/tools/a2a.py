"""A2A task tools — one namespace, two transports.

A2A is a single tool family over two transports, auto-selected from the
agent_id:

  - a local worker (listed by ``list_subagents``) is routed over stdin
    (stdin/JSON-RPC via the :class:`SubagentManager`).  Always available.
  - any other id is routed to the remote mesh over MQTT (the mqtt plugin;
    only when the broker is running).

Local subagent lifecycle (``spawn_subagent`` / ``list_subagents`` /
``stop_subagent``) carries no prefix.  All A2A tools share one uniform
``a2a_`` prefix — task routing (both transports) and mesh discovery
(``a2a_list_agents``, ``a2a_list_tasks``, ``a2a_agent_card``,
``a2a_broadcast``, agent/MQTT only).

Tool inventory
--------------
spawn_subagent            spawn a local worker
list_subagents            list local workers
stop_subagent             stop a local worker
a2a_send_task             send a task (subagent or remote peer) + wait
a2a_send_task_async       send a task without waiting
a2a_get_task_result       poll an async result
a2a_cancel_task           cancel a pending task
a2a_subscribe_task        wait for an async task to complete
a2a_list_agents           list remote mesh peers (agent/MQTT)
a2a_list_tasks            list the A2A task store (agent/MQTT)
a2a_agent_card            fetch a peer's agent card (agent/MQTT)
a2a_broadcast             send to every peer (agent/MQTT, extension)
notify_user               desktop alert (not A2A)
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from slife.tools.base import NO_PARAMS, Tool, make_params, require_params

logger = logging.getLogger(__name__)

#: A2A transport-support annotation — every ``a2a_*`` tool's schema must state
#: which transports are wired: subagent (stdin) and/or agent (MQTT).
_TRANSPORT_BOTH = "Transport support: subagent (stdin) + agent (MQTT) — both."
_TRANSPORT_AGENT_ONLY = "Transport support: agent (MQTT) only."


# ═══════════════════════════════════════════════════════════════════════════
# Subagent lifecycle
# ═══════════════════════════════════════════════════════════════════════════


class ListSubagentsTool(Tool):
    """List local subagents spawned by this instance."""

    name = "list_subagents"
    category = "Subagent"
    description = (
        "List local subagent workers (agent_id, PID, readiness). "
        "For remote mesh peers use a2a_list_agents."
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
                "Use spawn_subagent to create one."
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


class SpawnSubagentTool(Tool):
    """Spawn a new local subagent worker process."""

    name = "spawn_subagent"
    category = "Subagent"
    description = (
        "Spawn a local subagent worker (same LLM config and tools) to "
        "parallelize work; then delegate via a2a_send_task / a2a_send_task_async."
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
                f"  Use list_subagents to see all local workers.\n"
                f'  Use a2a_send_task with agent_id="{agent_id}" to delegate work.'
            )
        except Exception as e:
            logger.error("subagent_tool_spawn_failed err=%s", e)
            return f"Error spawning subagent: {e}"


class StopSubagentTool(Tool):
    """Stop a locally-managed subagent process."""

    name = "stop_subagent"
    category = "Subagent"
    description = (
        "Stop a locally-spawned subagent process. id from list_subagents."
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
                f"Use list_subagents to verify."
            )
        else:
            return (
                f"Subagent '{agent_id}' not found. "
                f"Use list_subagents to see managed subagents. "
                f"Only locally-managed subagents can be stopped."
            )


# ═══════════════════════════════════════════════════════════════════════════
# Introspection
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# Messaging
# ═══════════════════════════════════════════════════════════════════════════


class NotifyUserTool(Tool):
    """Push a desktop notification to the human operator.

    Not part of the A2A protocol standard — no ``a2a`` prefix.
    """

    name = "notify_user"
    category = "System"
    description = (
        "Send a desktop notification to the human user (e.g. task done, "
        "attention needed)."
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
# Unified task routing — one tool per action, `target` picks the transport:
#   "subagent" → local worker over stdin (SubagentManager, always available)
#   "agent"    → remote mesh peer over MQTT (mqtt plugin, broker must be up)
#   "auto"     → local worker wins if the id matches, else the mesh
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


def _mesh_client(ctx) -> object | None:
    """Return the mqtt plugin's MCP client (remote-mesh transport), or None."""
    return getattr(ctx, "a2a_mcp_client", None) if ctx is not None else None


def _resolve_backend(agent_id: str, ctx) -> tuple[str, Any]:
    """Resolve which A2A transport handles *agent_id* — automatic.

    A local worker (in ``list_subagents``) goes over stdin; any other id
    goes to the remote mesh when it is connected (``a2a_list_agents``).
    Returns ``("subagent", manager)``, ``("agent", mcp_client)``, or
    ``("error", reason)``.  The backend is ``Any`` — the two transports
    have disjoint interfaces (SubagentManager vs MCPClient), used on
    separate branches.
    """
    manager, hint = _manager_or_hint()
    if manager is not None and agent_id in manager.list():
        return "subagent", manager
    client = _mesh_client(ctx)
    if client is not None:
        return "agent", client
    if manager is not None:
        return "error", (
            f"'{agent_id}' is neither a local subagent (list_subagents) nor a "
            f"remote mesh peer (a2a_list_agents)."
        )
    return "error", hint


class A2ASendTaskTool(Tool):
    """Send a task to a subagent or remote agent and wait for the result."""

    name = "a2a_send_task"
    category = "A2A"
    description = (
        "Send a task and wait for the result (A2A message/send). Auto-routes by "
        "agent_id: a local subagent (list_subagents) over stdin, else a remote "
        "mesh peer (a2a_list_agents) over MQTT. "
        + _TRANSPORT_BOTH
    )
    parameters: ClassVar[dict] = make_params(
        agent_id={"type": "string", "description": "agent_id of a local subagent or remote mesh peer."},
        task={"type": "string", "description": "Self-contained task for the recipient."},
    )

    async def execute(self, agent_id: str = "", task: str = "", **kwargs) -> str:
        if err := require_params(agent_id=agent_id, task=task):
            return err
        kind, backend = _resolve_backend(agent_id, getattr(self, "_ctx", None))
        if kind == "error":
            return backend
        if kind == "subagent":
            try:
                return await backend.send_task(agent_id, task)
            except TimeoutError:
                return f"Error: task to '{agent_id}' timed out."
            except Exception as e:
                return f"Error sending task to subagent '{agent_id}': {e}"
        try:
            return await backend.call_tool("__send_task", {"agent_id": agent_id, "task": task})
        except Exception as e:
            return f"Error sending task to remote agent '{agent_id}': {e}"


class A2ASendTaskAsyncTool(Tool):
    """Send a task to a subagent or remote agent without waiting."""

    name = "a2a_send_task_async"
    category = "A2A"
    description = (
        "Send a task without waiting — returns a task_id (a correlation id for "
        "remote peers). Poll with a2a_get_task_result. Auto-routes by agent_id. "
        + _TRANSPORT_BOTH
    )
    parameters: ClassVar[dict] = make_params(
        agent_id={"type": "string", "description": "agent_id of a local subagent or remote mesh peer."},
        task={"type": "string", "description": "Self-contained task for the recipient."},
    )

    async def execute(self, agent_id: str = "", task: str = "", **kwargs) -> str:
        if err := require_params(agent_id=agent_id, task=task):
            return err
        kind, backend = _resolve_backend(agent_id, getattr(self, "_ctx", None))
        if kind == "error":
            return backend
        if kind == "subagent":
            try:
                rpc_id = await backend.send_task_async(agent_id, task)
                return f"Task sent to '{agent_id}' (task_id: {rpc_id})."
            except Exception as e:
                return f"Error sending task to subagent '{agent_id}': {e}"
        try:
            return await backend.call_tool("__send_task_async", {"agent_id": agent_id, "task": task})
        except Exception as e:
            return f"Error sending task to remote agent '{agent_id}': {e}"


class A2AGetTaskResultTool(Tool):
    """Return the result of an async task, or 'pending'."""

    name = "a2a_get_task_result"
    category = "A2A"
    description = (
        "Return an async task's result, or 'pending' if not ready (A2A tasks/get). "
        "task_id comes from a2a_send_task_async (a correlation id for remote peers). "
        "Auto-routes by agent_id. "
        + _TRANSPORT_BOTH
    )
    parameters: ClassVar[dict] = make_params(
        agent_id={"type": "string", "description": "agent_id of a local subagent or remote mesh peer."},
        task_id={"type": "string", "description": "task_id / correlation id from a2a_send_task_async."},
    )

    async def execute(self, agent_id: str = "", task_id: str = "", **kwargs) -> str:
        if err := require_params(agent_id=agent_id, task_id=task_id):
            return err
        kind, backend = _resolve_backend(agent_id, getattr(self, "_ctx", None))
        if kind == "error":
            return backend
        if kind == "subagent":
            result = backend.get_task_result(agent_id, task_id)
            return result if result is not None else "pending"
        try:
            return await backend.call_tool("__get_task_result", {"corr_id": task_id})
        except Exception as e:
            return f"Error getting result for task '{task_id}': {e}"


class A2ACancelTaskTool(Tool):
    """Cancel a pending task (best-effort)."""

    name = "a2a_cancel_task"
    category = "A2A"
    description = (
        "Cancel a pending task (best-effort — clears any stored result; A2A "
        "tasks/cancel). Auto-routes by agent_id. "
        + _TRANSPORT_BOTH
    )
    parameters: ClassVar[dict] = make_params(
        agent_id={"type": "string", "description": "agent_id of a local subagent or remote mesh peer."},
        task_id={"type": "string", "description": "task_id / correlation id to cancel."},
    )

    async def execute(self, agent_id: str = "", task_id: str = "", **kwargs) -> str:
        if err := require_params(agent_id=agent_id, task_id=task_id):
            return err
        kind, backend = _resolve_backend(agent_id, getattr(self, "_ctx", None))
        if kind == "error":
            return backend
        if kind == "subagent":
            if backend.get_task_result(agent_id, task_id) is not None:
                return f"Task '{task_id}' on agent '{agent_id}' cancelled."
            return (
                f"Task '{task_id}' not found on agent '{agent_id}'. "
                "It may have already completed or the task_id is incorrect."
            )
        try:
            return await backend.call_tool("__cancel_task", {"agent_id": agent_id, "corr_id": task_id})
        except Exception as e:
            return f"Error cancelling task '{task_id}' on '{agent_id}': {e}"


class A2ASubscribeTaskTool(Tool):
    """Wait for an async task to complete and return its result."""

    name = "a2a_subscribe_task"
    category = "A2A"
    description = (
        "Wait for an async task to complete and return its result (A2A "
        "tasks/subscribe). timeout in seconds. Auto-routes by agent_id. "
        + _TRANSPORT_BOTH
    )
    parameters: ClassVar[dict] = make_params(
        agent_id={"type": "string", "description": "agent_id of a local subagent or remote mesh peer."},
        task_id={"type": "string", "description": "task_id / correlation id from a2a_send_task_async."},
        timeout={"type": "number", "description": "Seconds to wait.", "default": 120.0},
    )

    async def execute(self, agent_id: str = "", task_id: str = "", timeout: float = 120.0, **kwargs) -> str:
        if err := require_params(agent_id=agent_id, task_id=task_id):
            return err
        kind, backend = _resolve_backend(agent_id, getattr(self, "_ctx", None))
        if kind == "error":
            return backend
        if kind == "subagent":
            try:
                result = await backend.subscribe_task(agent_id, task_id, timeout=timeout)
                return result if result is not None else "pending"
            except TimeoutError:
                return f"Error: task to '{agent_id}' timed out after {timeout}s."
            except Exception as e:
                return f"Error waiting for task on '{agent_id}': {e}"
        try:
            return await backend.call_tool("__subscribe_task", {"corr_id": task_id, "timeout": timeout})
        except Exception as e:
            return f"Error waiting for task '{task_id}': {e}"


# ═══════════════════════════════════════════════════════════════════════════
# Mesh discovery — native A2A tools forwarding to the mqtt plugin (agent/MQTT
# transport).  One uniform ``a2a_`` prefix across the whole A2A tool surface.
# ═══════════════════════════════════════════════════════════════════════════


def _mesh_client_or_hint(ctx) -> tuple[Any, str]:
    """Return (mqtt-plugin MCP client, "") or (None, error_hint)."""
    client = getattr(ctx, "a2a_mcp_client", None) if ctx is not None else None
    if client is None:
        return None, (
            "A2A mesh not connected — requires the MQTT broker running. "
            "Local subagent tools (list_subagents, a2a_send_task) are always available."
        )
    return client, ""


class ListAgentsTool(Tool):
    """List known A2A mesh peers (agent discovery)."""

    name = "a2a_list_agents"
    category = "A2A"
    description = (
        "List known online A2A mesh peers as JSON agent cards (A2A agent-card "
        "discovery). "
        + _TRANSPORT_AGENT_ONLY
    )
    parameters: ClassVar[dict] = NO_PARAMS

    async def execute(self, **kwargs) -> str:
        client, hint = _mesh_client_or_hint(getattr(self, "_ctx", None))
        if client is None:
            return hint
        try:
            return await client.call_tool("__list_agents", {})
        except Exception as e:
            return f"Error listing mesh agents: {e}"


class ListTasksTool(Tool):
    """List A2A task-store entries."""

    name = "a2a_list_tasks"
    category = "A2A"
    description = (
        "List A2A task-store entries (filterable by agent/status; A2A tasks/list). "
        + _TRANSPORT_AGENT_ONLY
    )
    parameters: ClassVar[dict] = make_params(
        agent_id={"type": "string", "description": "Optional agent_id to filter on.", "default": ""},
        status={"type": "string", "description": "Optional task status to filter on.", "default": ""},
    )

    async def execute(self, agent_id: str = "", status: str = "", **kwargs) -> str:
        client, hint = _mesh_client_or_hint(getattr(self, "_ctx", None))
        if client is None:
            return hint
        try:
            args: dict[str, str] = {}
            if agent_id:
                args["agent_id"] = agent_id
            if status:
                args["status"] = status
            return await client.call_tool("__list_tasks", args)
        except Exception as e:
            return f"Error listing tasks: {e}"


class AgentCardTool(Tool):
    """Return a mesh peer's agent card."""

    name = "a2a_agent_card"
    category = "A2A"
    description = (
        "Return a mesh peer's card (agent_id, display_name, status), or 'unknown' "
        "(A2A agent-card discovery). "
        + _TRANSPORT_AGENT_ONLY
    )
    parameters: ClassVar[dict] = make_params(
        agent_id={"type": "string", "description": "Mesh peer agent_id."},
    )

    async def execute(self, agent_id: str = "", **kwargs) -> str:
        if err := require_params(agent_id=agent_id):
            return err
        client, hint = _mesh_client_or_hint(getattr(self, "_ctx", None))
        if client is None:
            return hint
        try:
            return await client.call_tool("__agent_card", {"agent_id": agent_id})
        except Exception as e:
            return f"Error fetching agent card: {e}"


class BroadcastTool(Tool):
    """Send a task to every known mesh peer."""

    name = "a2a_broadcast"
    category = "A2A"
    description = (
        "Send a task to every known A2A mesh peer (fire-and-forget). "
        "Extension — not in the A2A protocol standard. "
        + _TRANSPORT_AGENT_ONLY
    )
    parameters: ClassVar[dict] = make_params(
        task={"type": "string", "description": "Task text to broadcast to all peers."},
    )

    async def execute(self, task: str = "", **kwargs) -> str:
        if err := require_params(task=task):
            return err
        client, hint = _mesh_client_or_hint(getattr(self, "_ctx", None))
        if client is None:
            return hint
        try:
            return await client.call_tool("__broadcast", {"task": task})
        except Exception as e:
            return f"Error broadcasting: {e}"
