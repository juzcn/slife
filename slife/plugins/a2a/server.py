"""slife-a2a server — the A2A protocol over MQTT, as a replaceable plugin.

Slife (the main process) acts as a thin client: it connects to this
plugin over Streamable HTTP, registers the ``a2a__*`` tools, and drains
inbound tasks/presence via the internal ``__a2a_drain_incoming`` tool.
The plugin owns the :class:`A2AClient` (MQTT) with the main agent's
identity — senders and the mesh cannot tell which slife process sent a
message, so subagents connect to the same plugin and reuse the channel.

Config arrives via ``SLIFE_A2A_CONFIG`` (a JSON serialization of
``A2AConfig``).  The parent spawns this process only when A2A is enabled
(the Mosquitto probe already succeeded), so ``enabled`` is True here.

Usage:
    uv run python -m slife.plugins.a2a.server
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

from slife.a2a import wire
from slife.a2a.card import AgentCard
from slife.a2a.client import A2AClient
from slife.a2a.config import A2AConfig
from slife.a2a.identity import AgentName, AgentMessage
from slife.fifoset import FifoSet
from slife.server_utils import create_plugin_server, run_plugin_server


@asynccontextmanager
async def _a2a_lifespan(_app):
    """Connect the mesh eagerly on plugin startup.

    Restores the pre-pluginization behavior (``AgentService.start_a2a``)
    where the A2AClient connected at launch — this agent announces
    presence and is reachable by peers without any a2a_* tool call.

    A failed eager connect is tolerated: it logs a warning and the
    client stays disconnected (mesh tools still attempt a lazy connect
    on demand).  On shutdown the client disconnects, announcing offline.
    """
    try:
        await _ensure_connected()
    except Exception as e:
        logger.warning("a2a_plugin_eager_connect_failed err=%s", e)
    try:
        yield
    finally:
        client = _client
        if client is not None and client.is_connected:
            try:
                await client.disconnect()
            except Exception as e:
                logger.debug("a2a_plugin_disconnect_error err=%s", e)


mcp, _log_path, logger = create_plugin_server(
    "slife-a2a",
    instructions=(
        "slife-a2a — A2A mesh channel (MQTT binding). Send tasks to "
        "peers (a2a_send_task / a2a_send_task_async), discover agents "
        "(a2a_list_agents), broadcast, cancel tasks, and query agent cards."
    ),
    lifespan=_a2a_lifespan,
)

# ── Lazy client init (FastMCP lazy-init rule) ──────────────────────
_client: A2AClient | None = None
_connect_lock = asyncio.Lock()
_inbound_tasks: list[dict] = []
_presence_events: list[dict] = []
_cancellations: list[dict] = []
_task_completions: list[dict] = []  # outbound async results → auto-push to harness
_poll_tasks: "FifoSet" = FifoSet()  # outbound async task_ids sent in "poll" mode (no auto-push)
# Outbound stateless message corr_ids → peer name (no task-store record).  A
# completion pops its entry; a silent peer can only evict past `_MAX_QUEUED`
# (oldest first, insertion order).
_message_sends: dict[str, str] = {}
# Because we're the responder, and we don't have a global registry that maps task IDs to their
# initiating agent's inbox, we snapshot the inbound task's reply_to at receive time.
_reply_tos: dict[str, str] = {}
_MAX_QUEUED = 500


def _load_config() -> A2AConfig:
    raw = os.environ.get("SLIFE_A2A_CONFIG", "{}")
    return A2AConfig(**json.loads(raw))


async def _ensure_connected() -> A2AClient:
    global _client
    if _client is not None and _client.is_connected:
        return _client
    async with _connect_lock:
        if _client is not None and _client.is_connected:
            return _client
        cfg = _load_config()
        if not cfg.enabled:
            raise RuntimeError(
                "A2A is not enabled (a2a not configured or broker unreachable)"
            )
        client = A2AClient(cfg)
        client.on_incoming_task(_on_incoming_task)
        client.on_incoming_cancel(_on_incoming_cancel)
        client.on_agent_change(_on_agent_change)
        client.on_task_result(_on_task_result)
        try:
            await client.connect()
        except Exception:
            # connect() can fail AFTER the adapter is already up (duplicate
            # agent name, subscribe error, refused CONNACK) — never leak the
            # connected paho thread on retry.
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        _client = client
        logger.info("a2a_plugin_client_connected id=%s", client.agent_name)
    return _client


# ── Inbound queueing (drained by the harness) ──────────────────────


async def _on_incoming_task(msg: AgentMessage) -> None:
    if len(_inbound_tasks) >= _MAX_QUEUED:
        # Queue full — drop the oldest rather than block the mesh; at least
        # surface it (the remote sender only learns via its task_timeout).
        logger.warning(
            "a2a_inbound_overflow dropped=1 type=task",
        )
        _inbound_tasks.pop(0)
    _inbound_tasks.append({
        "type": "task",
        "source": str(msg.source),
        "content": msg.content,
        "reply_to": msg.reply_to or "",
        "correlation_id": msg.correlation_id or "",
        # Wire kind (stateless message vs task) — drives the receiver's
        # [A2A:…] marker.  Defaults to task for peers that don't stamp it.
        "kind": msg.metadata.get("a2a_kind", "task"),
    })
    # Snapshot the requester's result topic so the LLM can complete this task
    # later via ``a2a_set_task_done(task_id, …)`` — the model never needs to
    # name a topic.  Only messages that name both an id and a reply topic are
    # completable; the map is bounded by evicting the oldest entry.
    corr_id = msg.correlation_id or ""
    reply_to = msg.reply_to or ""
    if corr_id and reply_to:
        _reply_tos[corr_id] = reply_to
        if len(_reply_tos) > _MAX_QUEUED:
            _reply_tos.pop(next(iter(_reply_tos)))


async def _on_task_result(corr_id: str, result: str, cancelled: bool) -> None:
    """Queue an outbound completion (async task or stateless message) for
    auto-push to the harness.

    The result arrived over MQTT (the peer published to our result topic);
    the harness drains this and pushes it into the history so the agent
    never needs to poll or block on subscribe.  A stateless message
    completion carries ``kind="message"`` and its peer from
    ``_message_sends`` (messages have no task-store record); a task's peer
    comes from the store.
    """
    if corr_id in _message_sends:
        peer = _message_sends.pop(corr_id, "")
        kind = "message"
    else:
        peer = ""
        try:
            from slife.a2a.task_store import get_store
            rec = get_store().get(corr_id)
            if rec is not None:
                peer = rec.agent_name
        except Exception:
            pass
        kind = "task"
    if corr_id in _poll_tasks:
        # Sent in "poll" mode — the caller retrieves via a2a_get_task_result;
        # no auto-push (avoids the redundant double-delivery of poll + push).
        _poll_tasks.discard(corr_id)
        return
    if len(_task_completions) >= _MAX_QUEUED:
        logger.warning("a2a_inbound_overflow dropped=1 type=task_completion")
        _task_completions.pop(0)
    _task_completions.append({
        "corr_id": corr_id, "result": result,
        "cancelled": cancelled, "peer": peer, "kind": kind,
    })


async def _on_incoming_cancel(corr_id: str) -> None:
    """A peer cancelled a task.  Drop it if still queued here (replying with
    a CANCELLED result so a waiting sender resolves), and always queue the
    cancel for the harness — the task may already have been drained to the
    agent loop, which needs to preempt it (Esc-equivalent).
    """
    if corr_id:
        # The peer gave up on this task — it must never be completable as
        # success via a2a_set_task_done afterwards.
        _reply_tos.pop(str(corr_id), None)
        for i, t in enumerate(_inbound_tasks):
            if t.get("correlation_id") == corr_id:
                entry = _inbound_tasks.pop(i)
                logger.info("a2a_queued_task_cancelled corr=%s", corr_id)
                reply_to = entry.get("reply_to", "")
                if reply_to and _client is not None:
                    try:
                        task = wire.Task.cancelled(corr_id, "cancelled by peer")
                        payload = json.dumps(
                            wire.task_result_envelope(corr_id, task),
                            ensure_ascii=False,
                        )
                        await _client.publish_message(reply_to, payload, qos=1)
                    except Exception:
                        pass
                break
    if len(_cancellations) >= _MAX_QUEUED:
        logger.warning("a2a_inbound_overflow dropped=1 type=cancel")
        _cancellations.pop(0)
    _cancellations.append({"type": "cancel", "corr_id": corr_id})


async def _on_agent_change(card: AgentCard, event: str) -> None:
    if len(_presence_events) >= _MAX_QUEUED:
        logger.warning("a2a_inbound_overflow dropped=1 type=presence")
        _presence_events.pop(0)
    _presence_events.append({
        "type": "presence",
        "event": event,
        "card": {
            "agent_name": str(card.agent_name),
            "status": card.status,
        },
    })


# ═══════════════════════════════════════════════════════════════════════
# A2A mesh tools (LLM-visible, uniform a2a_ prefix)
# ═══════════════════════════════════════════════════════════════════════
#
# These are the LLM-facing A2A tools.  The harness registers them with the
# server name ``a2a``; because every tool already starts with ``a2a_``, the
# proxy keeps the exact name (no ``a2a__a2a_`` duplication).  The MQTT
# transport is an internal binding — the tool surface is transport-agnostic
# (an HTTP/gRPC binding would plug in behind the same tools).


@mcp.tool(
    name="a2a_send_task",
    description="Send a task to a remote A2A mesh peer and wait for the result. "
    "timeout: seconds to wait before timing out (default 120; ≤0 = default). "
    "On a wait-timeout the call auto-degrades to async — the result is "
    "auto-delivered to your inbox when it arrives.",
)
async def a2a_send_task(
    agent_name: str, task: str, timeout: float | None = None,
) -> str:
    """Send a task to *agent_name* and wait for the result.

    The wait is enforced here in the plugin — the peer's task-store record
    is marked ``timeout`` — rather than by the sender's harness killing the
    MCP call mid-flight (the sender's ``_timeout`` meta-param maps onto this
    parameter, per the native-timeout contract).

    Args:
        agent_name: Remote peer's agent_name (from a2a_list_agents).
        task: The task text/instruction for the peer.
        timeout: Seconds to wait for the peer's result before timing out.
            Defaults to the plugin's ``task_timeout`` (120 s); a value ≤ 0
            also selects the default.
    """
    if timeout is not None and timeout <= 0:
        timeout = None  # ≤0 → plugin default, matching `_timeout: 0`
    client = await _ensure_connected()
    if timeout is not None:
        return await client.send_task(AgentName(agent_name), task, timeout=timeout)
    return await client.send_task(AgentName(agent_name), task)


@mcp.tool(
    name="a2a_send_task_async",
    description="Send a task to a remote A2A mesh peer without waiting — returns a "
    "task_id; mode 'poll' disables auto-delivery (retrieve with "
    "a2a_get_task_result).",
)
async def a2a_send_task_async(agent_name: str, task: str, mode: str = "auto") -> str:
    """Send a task without waiting — returns the correlation/task id.

    Args:
        agent_name: Remote peer's agent_name (from a2a_list_agents).
        task: The task text/instruction for the peer.
        mode: 'auto' (default) auto-push the result (also pollable);
            'poll' — no push, retrieve with a2a_get_task_result.
    """
    if mode not in ("auto", "poll"):
        return f"Error: mode must be 'auto' or 'poll', got {mode!r}."
    client = await _ensure_connected()
    corr_id = await client.send_task_async(AgentName(agent_name), task)
    if mode == "poll":
        # Bound the poll-tracking set — a silent peer would otherwise
        # accumulate ids forever (each id is only removed when its result
        # arrives).  Eviction drops the OLDEST id, not an arbitrary one (F10).
        _poll_tasks.add(corr_id)
        _poll_tasks.evict_to(_MAX_QUEUED)
        return (
            f"{corr_id}\n[auto-delivery disabled (mode=poll) — retrieve with "
            f"a2a_get_task_result]"
        )
    return corr_id


@mcp.tool(
    name="a2a_send_message",
    description="Send a stateless message to a remote A2A mesh peer and wait "
    "for its reply. timeout: seconds to wait before timing out (default 120; "
    "≤0 = default). On a wait-timeout the call auto-degrades to async — the "
    "reply is auto-delivered to your inbox when it arrives.",
)
async def a2a_send_message(
    agent_name: str, text: str, timeout: float | None = None,
) -> str:
    """Send a stateless message to *agent_name* and wait for its reply.

    A conversational ping-pong — unlike a2a_send_task it is not recorded as
    a task (a2a_list_tasks / a2a_cancel_task don't see it).

    The wait is enforced here in the plugin rather than by the sender's
    harness killing the MCP call mid-flight (the sender's ``_timeout``
    meta-param maps onto this parameter).

    Args:
        agent_name: Remote peer's agent_name (from a2a_list_agents).
        text: The message text for the peer.
        timeout: Seconds to wait for the peer's reply before timing out.
            Defaults to the plugin's ``task_timeout`` (120 s); a value ≤ 0
            also selects the default.
    """
    if timeout is not None and timeout <= 0:
        timeout = None  # ≤0 → plugin default, matching `_timeout: 0`
    client = await _ensure_connected()
    if timeout is not None:
        return await client.send_task(
            AgentName(agent_name), text, record=False, timeout=timeout,
        )
    return await client.send_task(AgentName(agent_name), text, record=False)


@mcp.tool(
    name="a2a_send_message_async",
    description="Send a stateless message to a remote A2A mesh peer without "
    "waiting — returns a message id; mode 'poll' disables auto-delivery "
    "(retrieve with a2a_get_task_result).",
)
async def a2a_send_message_async(
    agent_name: str, text: str, mode: str = "auto",
) -> str:
    """Send a stateless message without waiting — returns the message id.

    Like a2a_send_message the exchange is not recorded as a task.

    Args:
        agent_name: Remote peer's agent_name (from a2a_list_agents).
        text: The message text for the peer.
        mode: 'auto' (default) auto-push the reply (also pollable);
            'poll' — no push, retrieve with a2a_get_task_result.
    """
    if mode not in ("auto", "poll"):
        return f"Error: mode must be 'auto' or 'poll', got {mode!r}."
    client = await _ensure_connected()
    corr_id = await client.send_task_async(
        AgentName(agent_name), text, record=False,
    )
    # Bound the message-peer map — a silent peer would otherwise accumulate
    # ids forever (each id is only removed when its reply arrives).  Eviction
    # drops the OLDEST id, not an arbitrary one (F10).
    _message_sends[corr_id] = str(agent_name)
    if len(_message_sends) > _MAX_QUEUED:
        _message_sends.pop(next(iter(_message_sends)))
    if mode == "poll":
        _poll_tasks.add(corr_id)
        _poll_tasks.evict_to(_MAX_QUEUED)
        return (
            f"{corr_id}\n[auto-delivery disabled (mode=poll) — retrieve with "
            f"a2a_get_task_result]"
        )
    return corr_id


@mcp.tool(
    name="a2a_list_agents",
    description="List online A2A mesh agents as JSON cards ({agent_name, status}).",
)
async def a2a_list_agents() -> str:
    """List mesh agents — this agent's own card first, then remote peers."""
    client = await _ensure_connected()
    cards = [client.own_card()] + await client.list_agents()
    return json.dumps(
        [{"agent_name": str(c.agent_name), "status": c.status} for c in cards],
        ensure_ascii=False,
    )


@mcp.tool(
    name="a2a_get_task_result",
    description="Return a remote async task's result, or 'pending' if not ready.",
)
async def a2a_get_task_result(agent_name: str, task_id: str) -> str:
    """Return the result of an async task, or 'pending' if not ready.

    Args:
        agent_name: Remote peer's agent_name (from a2a_list_agents).
        task_id: The task id returned by a2a_send_task_async.
    """
    client = await _ensure_connected()
    result = client.get_task_result(task_id)
    return result if result is not None else "pending"


@mcp.tool(
    name="a2a_cancel_task",
    description="Cancel a pending or async task on a remote peer; reports the "
    "resulting status.",
)
async def a2a_cancel_task(agent_name: str, task_id: str) -> str:
    """Cancel a pending or async task on *agent_name*.

    Returns the task's resulting status — a task that already finished is
    reported as ``completed``/``failed`` (never ``cancelled``) and its result
    stays retrievable.

    Args:
        agent_name: Remote peer's agent_name (from a2a_list_agents).
        task_id: The task id returned by a2a_send_task_async.
    """
    client = await _ensure_connected()
    return await client.cancel_task(AgentName(agent_name), task_id)


@mcp.tool(
    name="a2a_list_tasks",
    description="List A2A mesh task-store entries (agent/status filters).",
)
async def a2a_list_tasks(agent_name: str = "", status: str = "") -> str:
    """List A2A task-store entries (filterable by agent/status).

    Args:
        agent_name: Optional filter — only tasks involving this peer.
        status: Optional filter — pending/completed/failed/cancelled.
    """
    client = await _ensure_connected()
    return json.dumps(
        client.list_tasks(agent_name=agent_name or None, status=status or None),
        ensure_ascii=False, default=str,
    )


@mcp.tool(
    name="a2a_agent_card",
    description="Return a mesh peer's card (agent_name, status), or 'unknown'.",
)
async def a2a_agent_card(agent_name: str) -> str:
    """Return a mesh peer's card (agent_name, status), or 'unknown'.

    Args:
        agent_name: Remote peer's agent_name (from a2a_list_agents).
    """
    client = await _ensure_connected()
    card = client.get_agent_card(AgentName(agent_name))
    if card is None:
        return "unknown"
    return json.dumps(
        {"agent_name": str(card.agent_name), "status": card.status},
        ensure_ascii=False,
    )


@mcp.tool(
    name="a2a_broadcast",
    description="Send a task to every known A2A mesh peer (fire-and-forget).",
)
async def a2a_broadcast(task: str) -> str:
    """Send *task* to every known A2A mesh peer (fire-and-forget).

    Args:
        task: The task text/instruction to send to every peer.
    """
    client = await _ensure_connected()
    corr_ids = await client.broadcast(task)
    return "\n".join(corr_ids) if corr_ids else "no_peers"


# ═══════════════════════════════════════════════════════════════════════
# Internal drain (the thin client polls this)
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="__a2a_drain_incoming",
    description="Drain queued inbound A2A tasks + presence events + cancellations "
    "+ async task completions. Internal — called by the agent service.",
)
async def __a2a_drain_incoming() -> str:
    """Drain queued inbound tasks + presence events + cancellations + async
    task completions (harness only)."""
    tasks = list(_inbound_tasks)
    _inbound_tasks.clear()
    presence = list(_presence_events)
    _presence_events.clear()
    cancellations = list(_cancellations)
    _cancellations.clear()
    completions = list(_task_completions)
    _task_completions.clear()
    return json.dumps(
        {
            "tasks": tasks,
            "presence": presence,
            "cancellations": cancellations,
            "task_completions": completions,
        },
        ensure_ascii=False,
    )


@mcp.tool(
    name="__check",
    description="A2A mesh status as JSON. Internal — probed by the harness's system_health.",
)
async def __check() -> str:
    """Return mesh connection + peer status for the harness health check.

    Reads the current client state WITHOUT triggering a connect — a status
    probe must never side-effect a connection (that would defeat the check
    and mask a genuinely offline mesh).  Peers come from the client's
    presence cache, and queued inbound task/presence/cancel counts report
    how much work the harness drain loop still has buffered.
    """
    cfg = _load_config()
    client = _client
    connected = client is not None and client.is_connected
    agent_name = ""
    status = ""
    peers: list[dict] = []
    if client is not None and client.is_connected:
        agent_name = str(client.agent_name)
        status = str(client.status)
        try:
            for card in await client.list_agents():
                peers.append({
                    "agent_name": str(card.agent_name),
                    "status": card.status,
                })
        except Exception as e:
            logger.warning("a2a_status_list_agents_failed err=%s", e)
    return json.dumps({
        "enabled": cfg.enabled,
        "connected": connected,
        "agent_name": agent_name,
        "status": status,
        "broker": f"{cfg.broker_host}:{cfg.broker_port}",
        "peers": peers,
        "queued": {
            "tasks": len(_inbound_tasks),
            "presence": len(_presence_events),
            "cancellations": len(_cancellations),
            "task_completions": len(_task_completions),
        },
    }, ensure_ascii=False)


async def _publish_task_result(
    reply_to: str, corr_id: str, text: str, cancelled: bool,
) -> str:
    """Publish a completed/cancelled :class:`Task` result envelope to *reply_to*.

    Shared by the internal ``__a2a_dispatch_result`` and the LLM-visible
    ``a2a_set_task_done`` — one publish path, one envelope shape.  This is the
    A2A request→response closure: a ``result.task`` envelope keyed by the
    original corr id on the requester's result topic.
    """
    client = await _ensure_connected()
    task = (
        wire.Task.cancelled(corr_id, text)
        if cancelled else wire.Task.completed(corr_id, text)
    )
    payload = json.dumps(
        wire.task_result_envelope(corr_id, task), ensure_ascii=False,
    )
    await client.publish_message(reply_to, payload, qos=1)
    return "ok"


@mcp.tool(
    name="a2a_set_task_done",
    description="Complete an inbound A2A task you received and are answering this "
    "turn. Its task_id is the [A2A:...] marker's task_id. Publishes the task's "
    "final result to the sender; unknown task_ids are refused. Completing a task "
    "is not sending a new message — it closes the task.",
)
async def a2a_set_task_done(
    task_id: str, result: str, cancelled: bool = False,
) -> str:
    """Mark a received A2A task as done, delivering *result* to its requester.

    The semantic is a terminal state transition (completed, or cancelled with
    *cancelled* true) on the ONE inbound task named by *task_id* — not a new
    message, and never a new task.  No task-store record is created here and
    nothing is awaited: the task's requester resolves from the published
    result envelope.

    Args:
        task_id: The inbound task's id (the [A2A:...] marker's ``task_id``).
        result: The task's result text (the artifact the requester receives).
        cancelled: If true, publish a CANCELLED result instead of completed.
    """
    if not task_id:
        return "Error: task_id required."
    reply_to = _reply_tos.get(task_id)
    if reply_to is None:
        return (
            f"Error: unknown task_id {task_id!r} — no inbound task with that "
            f"id is awaiting a result."
        )
    try:
        ok = await _publish_task_result(reply_to, task_id, result, cancelled)
    except Exception as e:
        logger.warning(
            "a2a_set_task_done_publish_failed task=%s err=%s", task_id, e,
        )
        return f"Error: failed to publish the result — {e}"
    # One-shot: a second completion of the same task becomes an explicit
    # unknown-task error, never a silent duplicate result envelope.
    _reply_tos.pop(task_id, None)
    return ok


@mcp.tool(
    name="__a2a_dispatch_result",
    description="Publish a task result to a requester's result topic. Internal — called by the agent service.",
)
async def __a2a_dispatch_result(
    reply_to: str, corr_id: str = "", text: str = "", cancelled: bool = False,
) -> str:
    """Publish a task result to the requester's result topic (harness only).

    The payload is the official JSON-RPC response envelope carrying a
    completed :class:`Task`, or a CANCELLED one when *cancelled* is true.
    """
    return await _publish_task_result(reply_to, corr_id, text, cancelled)


def main() -> None:
    try:
        run_plugin_server(mcp)
    finally:
        from slife.server_utils import shutdown_server_logging
        shutdown_server_logging()


if __name__ == "__main__":
    main()
