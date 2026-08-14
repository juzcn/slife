"""A2AClient — P2P communication core.

Each slife instance runs one A2AClient.  All clients are **peers** —
there is no central directory, no master election, no hierarchy.
Mosquitto is just the shared medium.

Responsibilities
----------------
* Connect to the MQTT broker, publish LWT for instant offline detection
* Periodic heartbeat — every agent announces its presence; peers that go
  silent for ``heartbeat_timeout`` seconds are pruned
* Agent discovery — subscribe to ``slife/+/presence``, maintain an
  in-memory table of known peers with ``on_agent_change`` callbacks
* Task routing — ``send_task(target, task)`` publishes to the target's
  inbox and waits for a result on the caller's result topic
* Inbound tasks — subscribe to own inbox, deliver via callback
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
import uuid
from collections.abc import Awaitable, Callable

from slife.a2a.card import AgentCard
from slife.a2a.config import A2AConfig
from slife.a2a.identity import AgentName, AgentMessage
from slife.a2a.mqtt import MQTTAdapter
from slife.a2a.transport import TransportAdapter, TransportMessage
from slife.a2a import wire


class DuplicateAgentError(RuntimeError):
    """Raised when another instance with the same agent-id is on the MQTT mesh."""
    pass

logger = logging.getLogger(__name__)

# ── Module-level current-client reference ────────────────────────────
# Set by AgentService.start_a2a() / stop_a2a() so that native tools
# (Slife.tools.a2a) can look up the live transport without closures.
_current_client: "A2AClient | None" = None


def get_client() -> "A2AClient | None":
    """Return the live A2AClient, or None if A2A is not active."""
    return _current_client


def set_client(client: "A2AClient") -> None:
    """Set the current A2AClient (called by AgentService.start_a2a)."""
    global _current_client
    _current_client = client


def clear_client() -> None:
    """Clear the current A2AClient (called by AgentService.stop_a2a)."""
    global _current_client
    _current_client = None


AgentChangeCallback = Callable[[AgentCard, str], Awaitable[None]]
"""Callback signature: ``async def on_change(card: AgentCard, event: str)``
where *event* is ``"online"``, ``"offline"``, or ``"timeout"``."""

IncomingTaskCallback = Callable[[AgentMessage], Awaitable[None]]
"""Callback for inbound A2A tasks."""

IncomingCancelCallback = Callable[[str], Awaitable[None]]
"""Callback for inbound A2A cancel requests — receives the task corr_id."""


class A2AClient:
    """P2P A2A client — each Slife instance has one."""

    def __init__(
        self, config: A2AConfig, transport: TransportAdapter | None = None,
    ):
        self._config = config
        self._agent_name = AgentName(config.agent_name)
        self._adapter: TransportAdapter = (
            transport if transport is not None
            else MQTTAdapter(config.agent_name)
        )

        # Peer tracking: agent_name → (AgentCard, last_heard_at)
        self._peers: dict[AgentName, tuple[AgentCard, float]] = {}

        # Callbacks
        self._agent_change_callbacks: list[AgentChangeCallback] = []
        self._incoming_task_callback: IncomingTaskCallback | None = None
        self._incoming_cancel_callback: IncomingCancelCallback | None = None

        # Heartbeat / watchdog tasks
        self._heartbeat_task: asyncio.Task | None = None
        self._peer_watchdog_task: asyncio.Task | None = None
        self._inbox_listener_task: asyncio.Task | None = None

        # Correlation tracking for send_task → await result
        self._pending_tasks: dict[str, asyncio.Future[str]] = {}

        # Completed async task results (corr_id → result_text)
        self._completed_tasks: dict[str, str] = {}

        # Status exposed via AgentCard
        self._status: str = "idle"

    # ── Properties ────────────────────────────────────────────────────

    @property
    def agent_name(self) -> AgentName:
        return self._agent_name

    @property
    def status(self) -> str:
        return self._status

    @property
    def is_connected(self) -> bool:
        return self._adapter.is_connected

    # ── Connection lifecycle ──────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to the broker and start all background tasks."""
        host = self._config.broker_host
        port = self._config.broker_port

        logger.info("a2a_connect host=%s port=%d id=%s", host, port, self._agent_name)
        await self._adapter.connect(host, port)

        # On a paho auto-reconnect, re-announce presence so peers (which
        # saw our LWT "offline") mark us online again (REVIEW H6).
        self._adapter.on_reconnect = self._publish_presence

        # Subscribe to peer presence before publishing our own,
        # so we can detect duplicate agent-ids already on the mesh.
        await self._adapter.subscribe("Slife/+/presence")

        # Check for duplicate agent-id — collect existing presences before
        # announcing ourselves.  Peers re-publish presence every heartbeat
        # interval, so a short window can miss one that announced earlier;
        # 5s is a best-effort compromise against blocking connect too long.
        try:
            async with asyncio.timeout(5.0):
                async for msg in self._adapter.messages("Slife/+/presence"):
                    try:
                        card = json.loads(msg.payload)
                        if card.get("agent_name") == self._agent_name:
                            raise DuplicateAgentError(
                                f"Agent '{self._agent_name}' is already running "
                                f"on the MQTT mesh.\n"
                                f"  • Stop mosquitto first, then restart slife\n"
                                f"  • Or use a different agent id:\n"
                                f"      slife --agent <new-id>"
                            )
                    except (json.JSONDecodeError, KeyError):
                        pass
        except TimeoutError:
            pass  # No duplicates found — good

        # Announce our presence
        await self._publish_presence("online")

        # Subscribe to own inbox + results
        await self._adapter.subscribe(f"Slife/{self._agent_name}/tasks/inbox")
        await self._adapter.subscribe(f"Slife/{self._agent_name}/tasks/result")

        # Start background loops
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._peer_watchdog_task = asyncio.create_task(self._peer_watchdog_loop())
        self._inbox_listener_task = asyncio.create_task(self._inbox_listener())

        logger.info("a2a_connected id=%s", self._agent_name)

    async def disconnect(self) -> None:
        """Gracefully leave the mesh."""
        logger.info("a2a_disconnecting id=%s", self._agent_name)

        # Cancel background tasks
        for task in (
            self._heartbeat_task, self._peer_watchdog_task, self._inbox_listener_task,
        ):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        await self._publish_presence("offline")

        # Resolve pending tasks as failed
        for future in self._pending_tasks.values():
            if not future.done():
                future.set_exception(RuntimeError("A2A client disconnected"))
        self._pending_tasks.clear()
        self._completed_tasks.clear()

        await self._adapter.disconnect()
        logger.info("a2a_disconnected id=%s", self._agent_name)

    # ── Public transport proxies ────────────────────────────────────
    # These exist so external code (Inbox, SubagentManager) never
    # reaches through self._adapter directly.

    async def publish_message(
        self, topic: str, payload: str, qos: int = 1,
    ) -> None:
        """Publish *payload* to *topic* on the underlying transport."""
        await self._adapter.publish(topic, payload, qos=qos)

    async def subscribe_topic(self, topic: str, qos: int = 1) -> None:
        """Subscribe to *topic* on the underlying transport."""
        await self._adapter.subscribe(topic, qos=qos)

    # ── Status ────────────────────────────────────────────────────────

    async def update_status(self, status: str) -> None:
        """Set idle/busy and announce."""
        self._status = status
        await self._publish_presence("online")

    # ── Discovery ─────────────────────────────────────────────────────

    def own_card(self) -> AgentCard:
        """Return this agent's own mesh card (self-view).

        ``list_agents`` deliberately excludes self — it is the peer cache.
        Agent-facing tooling like ``a2a_list_agents`` prepends this card so
        the agent can tell itself apart from peers instead of mistaking a
        same-named peer (e.g. a second ``slife`` process) for itself.
        """
        return AgentCard(
            agent_name=self._agent_name,
            status=self._status,
        )

    async def list_agents(self) -> list[AgentCard]:
        """Return all known online peer agents."""
        return [card for card, _ in self._peers.values()]

    def on_agent_change(self, callback: AgentChangeCallback) -> None:
        """Register a callback fired when agents come online or go offline."""
        self._agent_change_callbacks.append(callback)

    def on_incoming_task(self, callback: IncomingTaskCallback) -> None:
        """Register a callback for inbound A2A tasks."""
        self._incoming_task_callback = callback

    def on_incoming_cancel(self, callback: IncomingCancelCallback) -> None:
        """Register a callback fired when a peer cancels one of our tasks."""
        self._incoming_cancel_callback = callback

    # ── Task routing ──────────────────────────────────────────────────

    async def send_task(
        self, target: AgentName, task: str, timeout: float | None = None,
    ) -> str:
        """Send a task to *target* and wait for the result.

        Publishes an official ``SendMessage`` JSON-RPC request to
        ``slife/<target>/tasks/inbox`` (task text wrapped in a
        ``Message``), then waits for the result on our own result topic.
        """
        if timeout is None:
            timeout = self._config.task_timeout

        corr_id = uuid.uuid4().hex[:12]

        from slife.a2a.task_store import get_store
        get_store().record_send(corr_id, str(target), task, "mqtt")

        payload = json.dumps(
            wire.send_message_envelope(
                corr_id=corr_id,
                source=str(self._agent_name),
                task=task,
                reply_to=f"Slife/{self._agent_name}/tasks/result",
            ),
            ensure_ascii=False,
        )

        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending_tasks[corr_id] = future

        logger.debug(
            "a2a_send_task target=%s corr_id=%s task=%.80s",
            target, corr_id, task,
        )

        await self._adapter.publish(
            f"Slife/{target}/tasks/inbox", payload, qos=1,
        )

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            logger.debug("a2a_task_result corr_id=%s len=%d", corr_id, len(result))
            get_store().record_result(corr_id, result)
            return result
        except asyncio.TimeoutError:
            self._pending_tasks.pop(corr_id, None)
            get_store().record_error(corr_id, "timeout")
            raise TimeoutError(
                f"Task to '{target}' timed out after {timeout}s"
            )
        except asyncio.CancelledError:
            # A cancelled wait (loop tool_timeout, or a2a_cancel_task cancelling
            # the waiter) must not leak the pending future — drop it so a late
            # peer result can't resolve a future nobody awaits (REVIEW §1-8).
            self._pending_tasks.pop(corr_id, None)
            # Mark the store record terminal so a cancelled waiter doesn't
            # leave it "pending" forever.  If a2a_cancel_task already marked
            # it cancelled, leave that status alone.
            rec = get_store().get(corr_id)
            if rec is not None and rec.status == "pending":
                get_store().record_error(corr_id, "cancelled")
            raise

    # ── Async task routing ────────────────────────────────────────────

    async def send_task_async(self, target: AgentName, task: str) -> str:
        """Send a task without waiting — returns *correlation_id* immediately.

        The result can be retrieved later via :meth:`get_task_result`.
        """
        corr_id = uuid.uuid4().hex[:12]

        from slife.a2a.task_store import get_store
        get_store().record_send(corr_id, str(target), task, "mqtt")

        payload = json.dumps(
            wire.send_message_envelope(
                corr_id=corr_id,
                source=str(self._agent_name),
                task=task,
                reply_to=f"Slife/{self._agent_name}/tasks/result",
            ),
            ensure_ascii=False,
        )
        logger.debug(
            "a2a_send_task_async target=%s corr_id=%s task=%.80s",
            target, corr_id, task,
        )
        await self._adapter.publish(
            f"Slife/{target}/tasks/inbox", payload, qos=1,
        )
        return corr_id

    def get_task_result(self, corr_id: str) -> str | None:
        """Return the result of an async task, or ``None`` if not yet complete.

        Results are consumed — once retrieved they are removed from the store.
        """
        return self._completed_tasks.pop(corr_id, None)

    async def cancel_task(self, target: AgentName, corr_id: str) -> str:
        """Cancel a pending or async task, returning its resulting status.

        Returns ``"cancelled"``, ``"completed"``, ``"failed"``, or
        ``"not_found"``.  A task that already finished (completed/failed) is
        **never** reported as cancelled and its result is never discarded —
        it stays retrievable via :meth:`get_task_result` (REVIEW C5).  A
        still-pending task's local waiter is cancelled, the store record is
        marked cancelled, and a ``CancelTask`` request is published to
        *target* (the peer decides whether to honour it).
        """
        from slife.a2a.task_store import get_store

        store = get_store()
        rec = store.get(corr_id)

        # Cancel the synchronous waiter, if one is still waiting.
        future = self._pending_tasks.pop(corr_id, None)
        if future is not None and not future.done():
            future.cancel()

        if rec is not None:
            if rec.status in ("completed", "failed"):
                # Terminal — not cancelable; leave the result retrievable.
                return rec.status
            if rec.status == "cancelled":
                return "cancelled"
            # pending → cancelled
            store.record_cancel(corr_id)

        # Notify the target agent with the official CancelTask request.
        cancel_payload = json.dumps(
            wire.cancel_task_envelope(corr_id, str(self._agent_name)),
            ensure_ascii=False,
        )
        try:
            await self._adapter.publish(
                f"Slife/{target}/tasks/inbox", cancel_payload, qos=1,
            )
        except Exception:
            pass

        return "cancelled" if rec is not None else "not_found"

    async def broadcast(self, task: str) -> list[str]:
        """Send *task* to every known peer (fire-and-forget).

        Returns the list of correlation ids, one per peer.
        """
        corr_ids: list[str] = []
        for peer_id in list(self._peers.keys()):
            try:
                cid = await self.send_task_async(peer_id, task)
                corr_ids.append(f"{peer_id}:{cid}")
            except Exception as e:
                logger.warning("a2a_broadcast_skip peer=%s err=%s", peer_id, e)
        logger.info("a2a_broadcast peers=%d task=%.80s", len(corr_ids), task)
        return corr_ids

    def get_agent_card(self, agent_name: AgentName) -> AgentCard | None:
        """Return the :class:`AgentCard` for a known peer, or ``None``."""
        entry = self._peers.get(AgentName(agent_name))
        return entry[0] if entry else None

    # ── Task introspection ────────────────────────────────────────────

    def list_tasks(
        self, agent_name: str | None = None, status: str | None = None,
    ) -> list[dict]:
        """List mesh tasks from the shared :class:`TaskStore`.

        Each record is serialized as an official A2A ``Task`` dict.
        """
        from slife.a2a.task_store import get_store
        return [
            rec.to_task()
            for rec in get_store().list_tasks(
                agent_name=agent_name, status=status, transport="mqtt",
            )
        ]

    async def subscribe_task(
        self, task_id: str, timeout: float = 120.0,
    ) -> str | None:
        """Wait for an existing task to complete, returning its result.

        If the task is still pending (has a live Future), awaits it.
        If the task already completed, returns the stored result immediately.
        If the task is unknown, returns ``None``.
        """
        # Check completed store first
        if task_id in self._completed_tasks:
            return self._completed_tasks.pop(task_id)

        # Check pending — the future might still be alive
        future = self._pending_tasks.get(task_id)
        if future is not None:
            try:
                result = await asyncio.wait_for(future, timeout=timeout)
                from slife.a2a.task_store import get_store
                get_store().record_result(task_id, result)
                return result
            except asyncio.TimeoutError:
                from slife.a2a.task_store import get_store
                get_store().record_error(task_id, "timeout")
                raise TimeoutError(f"Subscribe to task '{task_id}' timed out after {timeout}s")

        # Subscribe via MQTT progress topic — wait for result on result topic
        progress_topic = f"Slife/{self._agent_name}/tasks/result"
        try:
            await self._adapter.subscribe(progress_topic)
        except Exception:
            pass  # Already subscribed

        # Poll with backoff
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if task_id in self._completed_tasks:
                result = self._completed_tasks.pop(task_id)
                from slife.a2a.task_store import get_store
                get_store().record_result(task_id, result)
                return result
            await asyncio.sleep(0.5)

        from slife.a2a.task_store import get_store
        get_store().record_error(task_id, "timeout")
        raise TimeoutError(f"Subscribe to task '{task_id}' timed out after {timeout}s")

    async def _publish_presence(self, status_override: str | None = None) -> None:
        """Publish our presence (called on connect, heartbeat, status change).

        The payload carries the official ``AgentCard`` fields plus the slife
        extensions (``agent_name``/``status``) read by the peer watchdog.
        """
        card = AgentCard(
            agent_name=self._agent_name,
            status=status_override if status_override in ("offline",) else self._status,
        )
        payload = json.dumps(card.to_dict(), ensure_ascii=False)
        await self._adapter.publish(
            f"Slife/{self._agent_name}/presence", payload, qos=1, retain=False,
        )

    async def _heartbeat_loop(self) -> None:
        """Periodically publish presence."""
        interval = self._config.heartbeat_interval
        while True:
            await asyncio.sleep(interval)
            try:
                await self._publish_presence()
            except Exception as e:
                logger.warning("a2a_heartbeat_fail err=%s", e)

    async def _peer_watchdog_loop(self) -> None:
        """Listen for peer presence and prune stale entries."""
        timeout = self._config.heartbeat_timeout
        async for msg in self._adapter.messages("Slife/+/presence"):
            try:
                data = json.loads(msg.payload)
            except json.JSONDecodeError:
                continue

            # Prune on every presence sighting — including our own echo and
            # malformed/offline payloads that fail to identify a peer.  Without
            # this, a peer that vanishes silently (lost offline message) is
            # never marked offline, because prune only ran on the peer path.
            await self._prune_stale_peers(timeout)

            card = AgentCard.from_dict(data)
            peer_id = card.agent_name
            if not peer_id or peer_id == self._agent_name:
                continue

            status = card.status

            was_known = peer_id in self._peers

            if status == "offline":
                if was_known:
                    old_card, _ = self._peers.pop(peer_id)
                    logger.info("a2a_agent_offline id=%s", peer_id)
                    await self._notify_agent_change(old_card, "offline")
                continue

            # Online / heartbeat
            self._peers[peer_id] = (card, _time.monotonic())

            if not was_known:
                logger.info("a2a_agent_online id=%s", peer_id)
                await self._notify_agent_change(card, "online")
            else:
                await self._notify_agent_change(card, "status_change")

    async def _prune_stale_peers(self, timeout: float) -> None:
        """Remove peers we haven't heard from within *timeout* seconds."""
        now = _time.monotonic()
        stale = [
            (pid, card)
            for pid, (card, last_heard) in self._peers.items()
            if now - last_heard > timeout
        ]
        for pid, card in stale:
            self._peers.pop(pid, None)
            logger.info("a2a_agent_timeout id=%s", pid)
            await self._notify_agent_change(card, "timeout")

    # ── Inbox listener ────────────────────────────────────────────────

    async def _inbox_listener(self) -> None:
        """Listen for incoming tasks on our inbox topic.

        Uses separate ``messages()`` async iterators for inbox and result
        queues — the same pattern as :meth:`_peer_watchdog_loop`.  This
        avoids creating/cancelling ``asyncio.Task`` objects on every poll
        cycle, which leaks orphaned ``queue.get()`` tasks that silently
        consume inbound messages.
        """
        inbox_filter = f"Slife/{self._agent_name}/tasks/inbox"
        result_filter = f"Slife/{self._agent_name}/tasks/result"

        logger.debug(
            "a2a_inbox_listener_start inbox=%s result=%s",
            inbox_filter, result_filter,
        )

        # Merge both streams into a single queue we can select on
        merged: "asyncio.Queue[TransportMessage]" = asyncio.Queue()

        async def forward(adapter, topic_filter):
            """Forward every message from *topic_filter* into *merged*."""
            try:
                async for msg in adapter.messages(topic_filter):
                    await merged.put(msg)
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning(
                    "a2a_forward_error filter=%s", topic_filter, exc_info=True,
                )

        f_inbox = asyncio.create_task(
            forward(self._adapter, inbox_filter),
        )
        f_result = asyncio.create_task(
            forward(self._adapter, result_filter),
        )

        try:
            # Loop until cancelled (at shutdown) — NOT `while is_connected`:
            # a transient broker disconnect must not kill the inbox listener.
            # The forward() tasks' messages() iterators now survive reconnects
            # (see MQTTAdapter.messages), so this keeps delivering afterwards.
            while True:
                try:
                    msg = await asyncio.wait_for(merged.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                try:
                    if msg.topic == result_filter or "/tasks/result" in msg.topic:
                        await self._handle_result(msg)
                    else:
                        await self._handle_incoming_task(msg)
                except Exception:
                    logger.warning(
                        "a2a_inbox_handler_error topic=%s", msg.topic, exc_info=True,
                    )
        finally:
            f_inbox.cancel()
            f_result.cancel()

    async def _handle_incoming_task(self, msg: TransportMessage) -> None:
        """Process an incoming task request (official ``SendMessage``).

        ``CancelTask`` requests are routed to the ``on_incoming_cancel``
        callback (the receiver drops/preempts the task) rather than being
        delivered as tasks.  Slife routing fields ride in the ``_slife``
        extension.
        """
        try:
            data = json.loads(msg.payload)
        except json.JSONDecodeError:
            logger.warning("a2a_invalid_task_payload topic=%s", msg.topic)
            return

        method = data.get("method", "")
        slife = data.get("_slife", {}) if isinstance(data.get("_slife"), dict) else {}
        source = AgentName(slife.get("source", "unknown"))

        if method == "CancelTask":
            logger.info("a2a_incoming_cancel source=%s task_id=%s", source, data.get("id"))
            if self._incoming_cancel_callback is not None:
                await self._incoming_cancel_callback(str(data.get("id", "")))
            return

        # SendMessage — extract the task text from the Message's first part.
        params = data.get("params", {}) if isinstance(data.get("params"), dict) else {}
        message = wire.Message.from_dict(params.get("message"))
        task = ""
        if message is not None and message.content:
            task = message.content[0].text
        reply_to = slife.get("reply_to", "")
        corr_id = str(data.get("id", ""))

        logger.info(
            "a2a_incoming_task source=%s corr_id=%s task=%.80s",
            source, corr_id, task,
        )

        if self._incoming_task_callback:
            agent_msg = AgentMessage(
                source=source,
                content=task,
                reply_to=reply_to,
                correlation_id=corr_id,
            )
            await self._incoming_task_callback(agent_msg)

    async def _handle_result(self, msg: TransportMessage) -> None:
        """Process a task result (official ``result.task`` envelope).

        Resolves synchronous waiters; stores async results for later
        retrieval via :meth:`get_task_result`.
        """
        try:
            data = json.loads(msg.payload)
        except json.JSONDecodeError:
            return

        # The JSON-RPC id is the correlation id; the result carries a Task.
        corr_id = str(data.get("id", ""))
        slife = data.get("_slife", {}) if isinstance(data.get("_slife"), dict) else {}
        if not corr_id:
            corr_id = str(slife.get("correlation_id", ""))
        result_block = data.get("result", {}) if isinstance(data.get("result"), dict) else {}
        result_text = wire.task_result_text(result_block.get("task", {}))
        # A peer that honoured a CancelTask returns a CANCELLED task — record
        # the store status accordingly (REVIEW C5).
        task_state = (
            (result_block.get("task") or {}).get("status") or {}
        ).get("state", "")
        cancelled = task_state == wire.TaskState.CANCELLED.value
        future = self._pending_tasks.pop(corr_id, None)

        from slife.a2a.task_store import get_store

        if future and not future.done():
            future.set_result(result_text)
            if cancelled:
                get_store().record_cancel(corr_id)
            else:
                get_store().record_result(corr_id, result_text)
            logger.debug("a2a_result_resolved corr_id=%s", corr_id)
        else:
            # Store for async retrieval — no synchronous waiter.  Cap the
            # cache so a session that never polls results doesn't grow it
            # without bound (oldest evicted first).
            self._completed_tasks[corr_id] = result_text
            if len(self._completed_tasks) > 100:
                oldest = next(iter(self._completed_tasks))
                self._completed_tasks.pop(oldest, None)
            if cancelled:
                get_store().record_cancel(corr_id)
            else:
                get_store().record_result(corr_id, result_text)
            logger.debug("a2a_result_stored_async corr_id=%s", corr_id)

    # ── Notify ─────────────────────────────────────────────────────────

    async def _notify_agent_change(self, card: AgentCard, event: str) -> None:
        """Fire all registered agent-change callbacks."""
        for cb in self._agent_change_callbacks:
            try:
                await cb(card, event)
            except Exception as e:
                logger.warning("a2a_agent_change_cb_error err=%s", e)
