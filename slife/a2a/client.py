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
from typing import Any

from slife.a2a.card import AgentCard
from slife.a2a.config import A2AConfig
from slife.a2a.identity import AgentId, AgentMessage
from slife.a2a.mqtt import MQTTAdapter, MQTTMessage

logger = logging.getLogger(__name__)

# ── Module-level current-client reference ────────────────────────────
# Set by AgentService.start_a2a() / stop_a2a() so that native tools
# (slife.tools.a2a) can look up the live transport without closures.
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


class A2AClient:
    """P2P A2A client — each slife instance has one."""

    def __init__(self, config: A2AConfig):
        self._config = config
        self._agent_id = AgentId(config.agent_id)
        self._adapter = MQTTAdapter(config.agent_id)

        # Peer tracking: agent_id → (AgentCard, last_heard_at)
        self._peers: dict[AgentId, tuple[AgentCard, float]] = {}

        # Callbacks
        self._agent_change_callbacks: list[AgentChangeCallback] = []
        self._incoming_task_callback: IncomingTaskCallback | None = None

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
    def agent_id(self) -> AgentId:
        return self._agent_id

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

        logger.info("a2a_connect host=%s port=%d id=%s", host, port, self._agent_id)
        await self._adapter.connect(host, port)

        # Announce our presence immediately
        await self._publish_presence("online")

        # Subscribe to peer presence
        await self._adapter.subscribe("slife/+/presence")

        # Subscribe to own inbox + results
        await self._adapter.subscribe(f"slife/{self._agent_id}/tasks/inbox")
        await self._adapter.subscribe(f"slife/{self._agent_id}/tasks/result")

        # Start background loops
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._peer_watchdog_task = asyncio.create_task(self._peer_watchdog_loop())
        self._inbox_listener_task = asyncio.create_task(self._inbox_listener())

        logger.info("a2a_connected id=%s", self._agent_id)

    async def disconnect(self) -> None:
        """Gracefully leave the mesh."""
        logger.info("a2a_disconnecting id=%s", self._agent_id)

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
        for corr_id, future in self._pending_tasks.items():
            if not future.done():
                future.set_exception(RuntimeError("A2A client disconnected"))
        self._pending_tasks.clear()
        self._completed_tasks.clear()

        await self._adapter.disconnect()
        logger.info("a2a_disconnected id=%s", self._agent_id)

    # ── Status ────────────────────────────────────────────────────────

    async def update_status(self, status: str) -> None:
        """Set idle/busy and announce."""
        self._status = status
        await self._publish_presence("online")

    # ── Discovery ─────────────────────────────────────────────────────

    async def list_agents(self) -> list[AgentCard]:
        """Return all known online peer agents."""
        return [card for card, _ in self._peers.values()]

    def on_agent_change(self, callback: AgentChangeCallback) -> None:
        """Register a callback fired when agents come online or go offline."""
        self._agent_change_callbacks.append(callback)

    def on_incoming_task(self, callback: IncomingTaskCallback) -> None:
        """Register a callback for inbound A2A tasks."""
        self._incoming_task_callback = callback

    # ── Task routing ──────────────────────────────────────────────────

    async def send_task(
        self, target: AgentId, task: str, timeout: float | None = None,
    ) -> str:
        """Send a task to *target* and wait for the result.

        Publishes to ``slife/<target>/tasks/inbox`` with a unique
        correlation id, then waits for a response on our own result topic.
        """
        if timeout is None:
            timeout = self._config.task_timeout

        corr_id = uuid.uuid4().hex[:12]

        from slife.a2a.task_store import get_store
        get_store().record_send(corr_id, str(target), task, "mqtt")

        payload = json.dumps({
            "correlation_id": corr_id,
            "source": self._agent_id,
            "task": task,
            "reply_to": f"slife/{self._agent_id}/tasks/result",
        })

        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._pending_tasks[corr_id] = future

        logger.debug(
            "a2a_send_task target=%s corr_id=%s task=%.80s",
            target, corr_id, task,
        )

        await self._adapter.publish(
            f"slife/{target}/tasks/inbox", payload, qos=1,
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

    # ── Async task routing ────────────────────────────────────────────

    async def send_task_async(self, target: AgentId, task: str) -> str:
        """Send a task without waiting — returns *correlation_id* immediately.

        The result can be retrieved later via :meth:`get_task_result`.
        """
        corr_id = uuid.uuid4().hex[:12]

        from slife.a2a.task_store import get_store
        get_store().record_send(corr_id, str(target), task, "mqtt")

        payload = json.dumps({
            "correlation_id": corr_id,
            "source": self._agent_id,
            "task": task,
            "reply_to": f"slife/{self._agent_id}/tasks/result",
        })
        logger.debug(
            "a2a_send_task_async target=%s corr_id=%s task=%.80s",
            target, corr_id, task,
        )
        await self._adapter.publish(
            f"slife/{target}/tasks/inbox", payload, qos=1,
        )
        return corr_id

    def get_task_result(self, corr_id: str) -> str | None:
        """Return the result of an async task, or ``None`` if not yet complete.

        Results are consumed — once retrieved they are removed from the store.
        """
        return self._completed_tasks.pop(corr_id, None)

    async def cancel_task(self, target: AgentId, corr_id: str) -> bool:
        """Cancel a pending or async task.

        Removes the local future (synchronous) or completed result (async),
        and sends a cancellation notice to *target*.
        """
        cancelled = False

        from slife.a2a.task_store import get_store

        # Cancel synchronous waiter
        future = self._pending_tasks.pop(corr_id, None)
        if future is not None and not future.done():
            future.cancel()
            cancelled = True

        # Remove async result if present
        if self._completed_tasks.pop(corr_id, None) is not None:
            cancelled = True

        if cancelled:
            get_store().record_cancel(corr_id)

        # Notify the target agent
        cancel_payload = json.dumps({
            "correlation_id": corr_id,
            "source": self._agent_id,
            "action": "cancel",
        })
        try:
            await self._adapter.publish(
                f"slife/{target}/tasks/inbox", cancel_payload, qos=1,
            )
        except Exception:
            pass

        return cancelled

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

    def get_agent_card(self, agent_id: AgentId) -> AgentCard | None:
        """Return the :class:`AgentCard` for a known peer, or ``None``."""
        entry = self._peers.get(AgentId(agent_id))
        return entry[0] if entry else None

    # ── Task introspection ────────────────────────────────────────────

    def list_tasks(
        self, agent_id: str | None = None, status: str | None = None,
    ) -> list:
        """List MQTT tasks from the shared :class:`TaskStore`."""
        from slife.a2a.task_store import get_store
        return get_store().list_tasks(
            agent_id=agent_id, status=status, transport="mqtt",
        )

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
        progress_topic = f"slife/{self._agent_id}/tasks/result"
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

    async def set_push_notification(
        self, task_id: str, notify_topic: str,
    ) -> bool:
        """Configure push notifications for *task_id*.

        Publishes a configuration message asking the target agent to push
        status updates to *notify_topic*.  The local client subscribes to
        that topic so updates arrive as MQTT messages.

        Returns ``True`` if the configuration was published.
        """
        from slife.a2a.task_store import get_store

        rec = get_store().get(task_id)
        if rec is None:
            return False

        # Subscribe locally
        try:
            await self._adapter.subscribe(notify_topic)
        except Exception:
            pass

        # Tell the target agent where to push updates
        config_payload = json.dumps({
            "correlation_id": task_id,
            "source": self._agent_id,
            "action": "set_push_notification",
            "notify_topic": notify_topic,
        })
        try:
            await self._adapter.publish(
                f"slife/{rec.agent_id}/tasks/inbox",
                config_payload, qos=1,
            )
        except Exception:
            return False

        logger.info(
            "a2a_push_notification_set task_id=%s topic=%s",
            task_id, notify_topic,
        )
        return True

    # ── Presence / heartbeat ──────────────────────────────────────────

    async def _publish_presence(self, status_override: str | None = None) -> None:
        """Publish our presence (called on connect, heartbeat, status change)."""
        card = AgentCard(
            agent_id=self._agent_id,
            display_name=self._config.agent_name,
            status=status_override if status_override in ("offline",) else self._status,
        )
        payload = json.dumps({
            "agent_id": card.agent_id,
            "display_name": card.display_name,
            "status": card.status,
        })
        await self._adapter.publish(
            f"slife/{self._agent_id}/presence", payload, qos=1, retain=False,
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
        async for msg in self._adapter.messages("slife/+/presence"):
            try:
                data = json.loads(msg.payload)
            except json.JSONDecodeError:
                continue

            peer_id = AgentId(data.get("agent_id", ""))
            if not peer_id or peer_id == self._agent_id:
                continue

            status = data.get("status", "online")
            display_name = data.get("display_name", peer_id)

            was_known = peer_id in self._peers

            if status == "offline":
                if was_known:
                    card, _ = self._peers.pop(peer_id)
                    logger.info("a2a_agent_offline id=%s", peer_id)
                    await self._notify_agent_change(card, "offline")
                continue

            # Online / heartbeat
            card = AgentCard(
                agent_id=peer_id, display_name=display_name, status=status,
            )
            self._peers[peer_id] = (card, _time.monotonic())

            if not was_known:
                logger.info("a2a_agent_online id=%s name=%s", peer_id, display_name)
                await self._notify_agent_change(card, "online")
            else:
                await self._notify_agent_change(card, "status_change")

            # Also prune stale peers on each message receipt
            await self._prune_stale_peers(timeout)

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
        inbox_filter = f"slife/{self._agent_id}/tasks/inbox"
        result_filter = f"slife/{self._agent_id}/tasks/result"

        logger.debug(
            "a2a_inbox_listener_start inbox=%s result=%s",
            inbox_filter, result_filter,
        )

        # Merge both streams into a single queue we can select on
        merged: asyncio.Queue[MQTTMessage] = asyncio.Queue()

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
            while self._adapter.is_connected:
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

    async def _wait_for_message(
        self,
        inbox_q: asyncio.Queue | None,
        result_q: asyncio.Queue | None,
    ) -> MQTTMessage | None:
        """Wait for the next message from either queue.

        Uses individual ``get()`` coroutines wrapped in tasks so we can
        wait on both queues simultaneously.  The ``try/finally`` ensures
        every task is cancelled on *any* exit path — including the
        ``asyncio.wait_for`` timeout in :meth:`_inbox_listener`.  Without
        this, orphaned ``get()`` tasks accumulate and silently consume
        inbound messages, making A2A task delivery look broken.
        """
        tasks: list[asyncio.Task] = []
        try:
            if inbox_q is not None:
                tasks.append(asyncio.create_task(inbox_q.get()))
            if result_q is not None:
                tasks.append(asyncio.create_task(result_q.get()))

            if not tasks:
                await asyncio.sleep(0.5)
                return None

            done, _ = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED,
            )
            return await done.pop()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def _handle_incoming_task(self, msg: MQTTMessage) -> None:
        """Process an incoming task request."""
        try:
            data = json.loads(msg.payload)
        except json.JSONDecodeError:
            logger.warning("a2a_invalid_task_payload topic=%s", msg.topic)
            return

        source = AgentId(data.get("source", "unknown"))
        task = data.get("task", "")
        reply_to = data.get("reply_to", "")
        corr_id = data.get("correlation_id", "")

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

    async def _handle_result(self, msg: MQTTMessage) -> None:
        """Process a task result (correlation_id match).

        Resolves synchronous waiters; stores async results for later
        retrieval via :meth:`get_task_result`.
        """
        try:
            data = json.loads(msg.payload)
        except json.JSONDecodeError:
            return

        corr_id = data.get("correlation_id", "")
        result_text = data.get("result", "")
        future = self._pending_tasks.pop(corr_id, None)

        from slife.a2a.task_store import get_store

        if future and not future.done():
            future.set_result(result_text)
            get_store().record_result(corr_id, result_text)
            logger.debug("a2a_result_resolved corr_id=%s", corr_id)
        else:
            # Store for async retrieval — no synchronous waiter
            self._completed_tasks[corr_id] = result_text
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
