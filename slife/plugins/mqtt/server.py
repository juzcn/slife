"""slife-mqtt server — the A2A protocol over MQTT, as a replaceable plugin.

Slife (the main process) acts as a thin client: it connects to this
plugin over Streamable HTTP, registers the ``a2a__*`` tools, and drains
inbound tasks/presence via the harness-only ``__a2a_drain_incoming`` tool.
The plugin owns the :class:`A2AClient` (MQTT) with the main agent's
identity — senders and the mesh cannot tell which slife process sent a
message, so subagents connect to the same plugin and reuse the channel.

Config arrives via ``SLIFE_MQTT_CONFIG`` (a JSON serialization of
``A2AConfig``).  The parent spawns this process only when A2A is enabled
(the Mosquitto probe already succeeded), so ``enabled`` is True here.

Usage:
    uv run python -m slife.plugins.mqtt.server
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

from slife.a2a.card import AgentCard
from slife.a2a.client import A2AClient
from slife.a2a.config import A2AConfig
from slife.a2a.identity import AgentId, AgentMessage
from slife.server_utils import create_plugin_server, run_plugin_server


@asynccontextmanager
async def _mqtt_lifespan(_app):
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
        logger.warning("mqtt_plugin_eager_connect_failed err=%s", e)
    try:
        yield
    finally:
        client = _client
        if client is not None and client.is_connected:
            try:
                await client.disconnect()
            except Exception as e:
                logger.debug("mqtt_plugin_disconnect_error err=%s", e)


mcp, _log_path, logger = create_plugin_server(
    "slife-mqtt",
    instructions=(
        "slife-mqtt — A2A/MQTT mesh channel. Send tasks to peers "
        "(send_task / send_task_async), discover agents (list_agents), "
        "broadcast, cancel tasks, and query agent cards."
    ),
    lifespan=_mqtt_lifespan,
)

# ── Lazy client init (FastMCP lazy-init rule) ──────────────────────
_client: A2AClient | None = None
_connect_lock = asyncio.Lock()
_inbound_tasks: list[dict] = []
_presence_events: list[dict] = []
_MAX_QUEUED = 500


def _load_config() -> A2AConfig:
    raw = os.environ.get("SLIFE_MQTT_CONFIG", "{}")
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
                "A2A is not enabled (mqtt not configured or broker unreachable)"
            )
        client = A2AClient(cfg)
        client.on_incoming_task(_on_incoming_task)
        client.on_agent_change(_on_agent_change)
        await client.connect()
        _client = client
        logger.info("mqtt_plugin_client_connected id=%s", client.agent_id)
    return _client


# ── Inbound queueing (drained by the harness) ──────────────────────


async def _on_incoming_task(msg: AgentMessage) -> None:
    if len(_inbound_tasks) >= _MAX_QUEUED:
        _inbound_tasks.pop(0)
    _inbound_tasks.append({
        "type": "task",
        "source": str(msg.source),
        "content": msg.content,
        "reply_to": msg.reply_to or "",
        "correlation_id": msg.correlation_id or "",
    })


async def _on_agent_change(card: AgentCard, event: str) -> None:
    if len(_presence_events) >= _MAX_QUEUED:
        _presence_events.pop(0)
    _presence_events.append({
        "type": "presence",
        "event": event,
        "card": {
            "agent_id": str(card.agent_id),
            "display_name": card.display_name,
            "status": card.status,
        },
    })


# ═══════════════════════════════════════════════════════════════════════
# Mesh tools (registered as a2a__<name>)
# ═══════════════════════════════════════════════════════════════════════


# Remote-peer plumbing — reached through the unified native a2a_* tools
# (auto-routed to the mesh).  `_` prefix keeps them out of the LLM's tool
# list; they are still callable via the plugin's MCP client.

@mcp.tool(
    name="__send_task",
    description="Send a task to a remote mesh peer and wait. Harness-only (call via a2a_send_task).",
)
async def __send_task(agent_id: str, task: str) -> str:
    """Send a task to *agent_id* and wait for the result."""
    client = await _ensure_connected()
    return await client.send_task(AgentId(agent_id), task)


@mcp.tool(
    name="__send_task_async",
    description="Send a task to a remote mesh peer without waiting. Harness-only (call via a2a_send_task_async).",
)
async def __send_task_async(agent_id: str, task: str) -> str:
    """Send a task without waiting — returns the correlation id."""
    client = await _ensure_connected()
    return await client.send_task_async(AgentId(agent_id), task)


@mcp.tool(
    name="__list_agents",
    description="List known online A2A mesh peers as JSON agent cards. Harness-only (call via a2a_list_agents).",
)
async def __list_agents() -> str:
    """List known online A2A mesh peers as JSON agent cards."""
    client = await _ensure_connected()
    cards = await client.list_agents()
    return json.dumps(
        [{"agent_id": str(c.agent_id), "display_name": c.display_name, "status": c.status}
         for c in cards],
        ensure_ascii=False,
    )


@mcp.tool(
    name="__get_task_result",
    description="Return a remote async task's result, or 'pending'. Harness-only (call via a2a_get_task_result).",
)
async def __get_task_result(corr_id: str) -> str:
    """Return the result of an async task, or 'pending' if not ready."""
    client = await _ensure_connected()
    result = client.get_task_result(corr_id)
    return result if result is not None else "pending"


@mcp.tool(
    name="__cancel_task",
    description="Cancel a pending or async task on a remote mesh peer. Harness-only (call via a2a_cancel_task).",
)
async def __cancel_task(agent_id: str, corr_id: str) -> str:
    """Cancel a pending or async task on *agent_id*."""
    client = await _ensure_connected()
    cancelled = await client.cancel_task(AgentId(agent_id), corr_id)
    return "cancelled" if cancelled else "not_found"


@mcp.tool(
    name="__list_tasks",
    description="List A2A task-store entries (filterable by agent/status). Harness-only (call via a2a_list_tasks).",
)
async def __list_tasks(agent_id: str = "", status: str = "") -> str:
    """List A2A task-store entries (filterable by agent/status)."""
    client = await _ensure_connected()
    return json.dumps(
        client.list_tasks(agent_id=agent_id or None, status=status or None),
        ensure_ascii=False, default=str,
    )


@mcp.tool(
    name="__subscribe_task",
    description="Wait for a remote async task to complete. Harness-only (call via a2a_subscribe_task).",
)
async def __subscribe_task(corr_id: str, timeout: float = 120.0) -> str:
    """Wait for an async task to complete and return its result."""
    client = await _ensure_connected()
    result = await client.subscribe_task(corr_id, timeout=timeout)
    return result if result is not None else "pending"


@mcp.tool(
    name="__agent_card",
    description="Return a mesh peer's card (agent_id, display_name, status), or 'unknown'. Harness-only (call via a2a_agent_card).",
)
async def __agent_card(agent_id: str) -> str:
    """Return a mesh peer's card (agent_id, display_name, status), or 'unknown'."""
    client = await _ensure_connected()
    card = client.get_agent_card(AgentId(agent_id))
    if card is None:
        return "unknown"
    return json.dumps(
        {"agent_id": str(card.agent_id), "display_name": card.display_name, "status": card.status},
        ensure_ascii=False,
    )


@mcp.tool(
    name="__broadcast",
    description="Send a task to every known A2A mesh peer (fire-and-forget). Harness-only (call via a2a_broadcast).",
)
async def __broadcast(task: str) -> str:
    """Send *task* to every known A2A mesh peer (fire-and-forget)."""
    client = await _ensure_connected()
    corr_ids = await client.broadcast(task)
    return "\n".join(corr_ids) if corr_ids else "no_peers"


# ═══════════════════════════════════════════════════════════════════════
# Harness-only drain (the thin client polls this)
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="__a2a_drain_incoming",
    description="Drain queued inbound A2A tasks + presence events. Harness-only.",
)
async def __a2a_drain_incoming() -> str:
    """Drain queued inbound tasks + presence events (harness only)."""
    tasks = list(_inbound_tasks)
    _inbound_tasks.clear()
    presence = list(_presence_events)
    _presence_events.clear()
    return json.dumps(
        {"tasks": tasks, "presence": presence}, ensure_ascii=False,
    )


@mcp.tool(
    name="__a2a_dispatch_result",
    description="Publish a task result to a requester's result topic. Harness-only.",
)
async def __a2a_dispatch_result(
    reply_to: str, corr_id: str = "", text: str = "",
) -> str:
    """Publish a task result to the requester's result topic (harness only)."""
    client = await _ensure_connected()
    payload = json.dumps(
        {"correlation_id": corr_id, "result": text}, ensure_ascii=False,
    )
    await client.publish_message(reply_to, payload, qos=1)
    return "ok"


def main() -> None:
    try:
        run_plugin_server(mcp)
    finally:
        from slife.server_utils import shutdown_server_logging
        shutdown_server_logging()


if __name__ == "__main__":
    main()
