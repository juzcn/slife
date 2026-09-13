"""slife-a2a server — the A2A protocol over MQTT, as a replaceable plugin.

Slife (the main process) acts as a thin client: it connects to this
plugin over Streamable HTTP, registers the ``a2a_*`` tools, and drains
inbound tasks/presence via the internal ``__a2a_drain_incoming`` tool.
The plugin owns the mesh — :class:`slife.a2a.mesh.A2AMesh`, the official
``a2a-over-mqtt`` SDK transport — with the main agent's identity; senders
and the mesh cannot tell which slife process sent a message, so subagents
connect to the same plugin and reuse the channel.

The LLM tool surface is the standard A2A operation set: ``message/send``
(``a2a_send_message``) with an explicit ``message_type`` — ``task_request``
(starts a task, async push result model) or ``task_response`` (completes an
inbound task with the standard task status/result update), ``CancelTask``
(``a2a_cancel_task``), discovery (``a2a_list_agents``) and fire-and-forget
events (``a2a_broadcast``).  Everything rides the standard wire — no
message/task split, no sync-wait variants.

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
import sys
from collections import deque
from contextlib import asynccontextmanager

from slife.a2a.card import AgentCard
from slife.a2a.config import A2AConfig
from slife.a2a.mesh import A2AMesh
from slife.server_utils import create_plugin_server, run_plugin_server


@asynccontextmanager
async def _a2a_lifespan(_app):
    """Connect the mesh eagerly on plugin startup.

    The mesh announces presence (retained Agent Card) and is reachable by
    peers without any a2a_* tool call.  A failed eager connect is tolerated:
    it logs a warning and the mesh stays disconnected (tools still attempt a
    lazy connect on demand).  On shutdown the mesh disconnects — the SDK
    publishes the offline card and cancels inflight inbound tasks.
    """
    try:
        await _ensure_connected()
    except Exception as e:
        logger.warning("a2a_plugin_eager_connect_failed err=%s", e)
    try:
        yield
    finally:
        client = _client
        if client is not None:
            try:
                await client.disconnect()
            except Exception as e:
                logger.debug("a2a_plugin_disconnect_error err=%s", e)


mcp, _log_path, logger = create_plugin_server(
    "slife-a2a",
    instructions=(
        "slife-a2a — A2A mesh channel (MQTT binding, standard A2A-over-MQTT "
        "profile). Send a task to a peer or complete an inbound task "
        "(a2a_send_message with message_type task_request/task_response), "
        "discover agents (a2a_list_agents), broadcast events (a2a_broadcast), "
        "and cancel tasks (a2a_cancel_task)."
    ),
    lifespan=_a2a_lifespan,
)

# ── Lazy client init (FastMCP lazy-init rule) ──────────────────────
_client: A2AMesh | None = None
_connect_lock = asyncio.Lock()
_MAX_QUEUED = 500


def _make_queue() -> "deque[dict]":
    """A bounded FIFO — ``append`` drops the oldest entry past ``_MAX_QUEUED``
    (deque overflow eviction instead of a manual ``pop(0)`` keep-below loop)."""
    return deque(maxlen=_MAX_QUEUED)


_inbound_tasks = _make_queue()
_inbound_events = _make_queue()
_presence_events = _make_queue()
_cancellations = _make_queue()
_task_completions = _make_queue()


def _load_config() -> A2AConfig:
    raw = os.environ.get("SLIFE_A2A_CONFIG", "{}")
    return A2AConfig(**json.loads(raw))


async def _ensure_connected() -> A2AMesh:
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
        client = A2AMesh(cfg)
        _wire_callbacks(client)
        try:
            await client.connect()
        except Exception:
            # connect() can fail mid-way (broker refused, timeout) — never
            # leak a half-started mesh on retry.
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        _client = client
        logger.info("a2a_plugin_client_connected id=%s", client.agent_name)
    return _client


def _wire_callbacks(client: A2AMesh) -> None:
    """Attach the plugin's inbound queues to the mesh."""
    client.on_inbound_task = _on_inbound_task
    client.on_task_completion = _on_task_completion
    client.on_agent_change = _on_agent_change
    client.on_peer_cancel = _on_peer_cancel
    client.on_broadcast_event = _on_broadcast_event


# ── Inbound queueing (drained by the harness) ──────────────────────


def _enqueue(collection: "deque[dict]", kind: str, entry: dict) -> None:
    """Append bounded FIFO — drop the oldest rather than block the mesh."""
    if len(collection) >= _MAX_QUEUED:
        logger.warning("a2a_inbound_overflow dropped=1 type=%s", kind)
    collection.append(entry)  # deque(maxlen=_MAX_QUEUED) evicts the oldest


def _on_inbound_task(
    source: str, content: str, task_id: str, kind: str = "task",
) -> None:
    # Only TASK_REQUEST messages are tasks (task_id present); a MESSAGE
    # conversation arrives task-less with kind="message" — no completion.
    _enqueue(_inbound_tasks, "task", {
        "type": "task", "source": source, "content": content,
        "task_id": task_id, "kind": kind,
    })


def _on_task_completion(
    corr_id: str, result: str, cancelled: bool, peer: str,
    kind: str = "task",
) -> None:
    _enqueue(_task_completions, "task_completion", {
        "corr_id": corr_id, "result": result, "cancelled": cancelled,
        "peer": peer, "kind": kind,
    })


def _on_agent_change(card: AgentCard, event: str) -> None:
    _enqueue(_presence_events, "presence", {
        "type": "presence", "event": event,
        "card": {"agent_name": str(card.agent_name), "status": card.status},
    })


def _on_peer_cancel(task_id: str) -> None:
    _enqueue(_cancellations, "cancel", {
        "type": "cancel", "corr_id": task_id,
    })


def _on_broadcast_event(sender: str, text: str) -> None:
    _enqueue(_inbound_events, "event", {
        "type": "event", "source": sender, "content": text,
    })


# ═══════════════════════════════════════════════════════════════════════
# A2A mesh tools (LLM-visible, standard A2A operations)
# ═══════════════════════════════════════════════════════════════════════
#
# The standard A2A-over-MQTT profile is async-native: a message/send is typed —
# 'task_request' returns a task_id immediately and the result auto-delivers to
# the inbox later (an [A2A:…] marker of type 'task_response'); 'task_response'
# completes an inbound task; 'message' is a bare conversation.  No waiting, no
# timeout parameter.


@mcp.tool(
    name="a2a_send_message",
    description="Send a message to an A2A mesh peer. message_type decides "
    "the semantics: 'task_request' (default) starts a task and returns its "
    "task_id; 'task_response' completes the inbound task given by task_id "
    "with message; 'message' sends a bare conversation (no task). Only "
    "task_request creates a task. Results auto-push to your inbox — never "
    "wait or poll.",
)
async def a2a_send_message(
    agent: str, message: str,
    message_type: str = "task_request", task_id: str = "",
) -> str:
    """Send a message to *agent*.

    Args:
        agent: Peer's agent_name (from a2a_list_agents).
        message: Task text, response result, or conversation text.
        message_type: 'task_request' (default) starts a task, returns task_id;
            'task_response' completes the inbound task named by task_id with
            message; 'message' sends a bare conversation, no task.
        task_id: Required when message_type='task_response': the [A2A:...]
            marker's task_id you are answering.
    """
    if message_type not in ("message", "task_request", "task_response"):
        return (
            f"Error: message_type must be 'message', 'task_request', or "
            f"'task_response', got {message_type!r}."
        )
    client = await _ensure_connected()
    if message_type == "task_response":
        if not task_id:
            return (
                "Error: task_id required for message_type='task_response' "
                "(the [A2A:...] marker's task_id)."
            )
        return client.complete_task(task_id, message, False)
    sent_id = await client.send_message(agent, message, message_type=message_type)
    if message_type == "message":
        return f"{sent_id}\nSent to {agent}. No task created — the reply arrives as a message."
    return f"{sent_id}\nSent to {agent}. The result will arrive in your inbox."


@mcp.tool(
    name="a2a_cancel_task",
    description="Cancel a task you sent to an A2A mesh peer. Returns the "
    "resulting status: 'cancelled' when the cancel was issued, "
    "'completed'/'failed'/'cancelled' for an already-terminal task (a "
    "finished result is never lost), or 'not_found' for an unknown task id.",
)
async def a2a_cancel_task(agent: str, task_id: str) -> str:
    """Cancel a task sent to *agent*.

    Args:
        agent: Remote peer's agent_name (from a2a_list_agents).
        task_id: The task id returned by a2a_send_message.
    """
    client = await _ensure_connected()
    return await client.cancel_task(agent, task_id)


@mcp.tool(
    name="a2a_list_agents",
    description="List A2A mesh agents as JSON cards — your own card first, "
    "then each peer found by discovery, sorted by name. Each card is "
    "{\"agent_name\": \"...\", \"status\": \"online\"|\"offline\"}.",
)
async def a2a_list_agents() -> str:
    """List mesh agents — this agent's own card first, then remote peers."""
    client = await _ensure_connected()
    return json.dumps(
        [{"agent_name": str(c.agent_name), "status": c.status}
         for c in client.list_agents()],
        ensure_ascii=False,
    )


@mcp.tool(
    name="a2a_broadcast",
    description="Publish a fire-and-forget event to every A2A mesh peer "
    "(QoS 0, loss-tolerant). No reply is expected — peers receive it as an "
    "informational [A2A:...] event with no task_id and no completion.",
)
async def a2a_broadcast(event: str) -> str:
    """Broadcast an event to every mesh peer — fire-and-forget.

    Args:
        event: The event text to publish.
    """
    client = await _ensure_connected()
    await client.broadcast(event)
    return "Broadcast sent — fire-and-forget, no reply expected."


# ═══════════════════════════════════════════════════════════════════════
# Internal drain (the thin client polls this)
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="__a2a_drain_incoming",
    description="Drain queued inbound A2A tasks + presence events + "
    "cancellations + async task completions. Internal — called by the agent "
    "service.",
)
async def __a2a_drain_incoming() -> str:
    """Drain queued inbound tasks + events + presence + cancellations +
    completions (harness only)."""
    tasks = list(_inbound_tasks)
    _inbound_tasks.clear()
    events = list(_inbound_events)
    _inbound_events.clear()
    presence = list(_presence_events)
    _presence_events.clear()
    cancellations = list(_cancellations)
    _cancellations.clear()
    completions = list(_task_completions)
    _task_completions.clear()
    return json.dumps(
        {
            "tasks": tasks,
            "events": events,
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

    Reads the current mesh state WITHOUT triggering a connect — a status
    probe must never side-effect a connection (that would defeat the check
    and mask a genuinely offline mesh).  Peers come from the mesh's presence
    cache, and queued inbound task/presence/cancel counts report how much
    work the harness drain loop still has buffered.
    """
    cfg = _load_config()
    client = _client
    connected = client is not None and client.is_connected
    agent_name = ""
    status = ""
    peers: list[dict] = []
    if client is not None and connected:
        agent_name = client.agent_name
        status = client.status
        for card in client.list_agents():
            if str(card.agent_name) != agent_name:
                peers.append({
                    "agent_name": str(card.agent_name),
                    "status": card.status,
                })
    return json.dumps({
        "enabled": cfg.enabled,
        "connected": connected,
        "agent_name": agent_name,
        "status": status,
        "broker": f"{cfg.broker_host}:{cfg.broker_port}",
        "peers": peers,
        "queued": {
            "tasks": len(_inbound_tasks),
            "events": len(_inbound_events),
            "presence": len(_presence_events),
            "cancellations": len(_cancellations),
            "task_completions": len(_task_completions),
        },
    }, ensure_ascii=False)


def _ensure_windows_selector_loop() -> None:
    """aiomqtt's socket callbacks (loop.add_reader/add_writer) are unsupported
    by the default Windows Proactor event loop — its connections fail with
    NotImplementedError.  This process is dedicated to the mesh (it spawns no
    subprocesses, which is what the Proactor loop is for), so switching to
    the selector loop is safe here.

    Deliberately NOT at module import: the module is imported in-process by
    tests (test_a2a_plugin), where the process-wide policy change would
    break the suite's asyncio-subprocess tests.  Scoped to the plugin entry.
    """
    if sys.platform == "win32":  # pragma: no cover — Windows-only
        try:
            asyncio.set_event_loop_policy(
                asyncio.WindowsSelectorEventLoopPolicy(),
            )
        except (ValueError, AttributeError):
            pass


def main() -> None:
    _ensure_windows_selector_loop()
    try:
        run_plugin_server(mcp)
    finally:
        from slife.server_utils import shutdown_server_logging
        shutdown_server_logging()


if __name__ == "__main__":
    main()