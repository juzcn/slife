"""A2A mesh — standard A2A-over-MQTT transport on the official EMQX SDK.

The wire is 100% the ``a2a-over-mqtt`` SDK: topic scheme
(``$a2a/v1/{discovery|request|reply|event}/{org}/{unit}/{agent_id}``),
JSON-RPC 2.0 over MQTT v5 ``ResponseTopic``/``CorrelationData`` properties,
Agent Cards with ``a2a-status`` presence, and the ack → stream → artifact →
terminal task lifecycle.  This module adds only slife's harness glue:

* **Inbound** — a :class:`MeshResponder` (SDK ``Responder`` subclass) whose
  ``on_request`` enqueues the task for the harness and blocks until the model
  completes it out-of-band (:meth:`A2AMesh.complete_task`); the SDK then
  finishes the standard lifecycle (artifact + terminal / cancel) itself.
* **Outbound** — a thin persistent driver (the SDK's ``Requester`` is
  connection-per-call, which would break our async-push / late-result model):
  sends, reply listening, discovery/presence, cancellation.  All wire
  building/parsing uses SDK primitives directly.
* **Presence** — discovery wildcard → peer cache + transition events; the
  SDK's ``Responder`` owns the retained card / LWT for our own identity.

Two connections, distinct client ids: the SDK ``Responder`` connects on its
own (presence publishing stays inside the SDK, untouched); the outbound
client (identifier ``{org}/{unit}/{agent}-out``) only subscribes discovery
+ our reply sessions and publishes requests — never presence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid

import aiomqtt
from a2a_over_mqtt import (
    A2ARequest,
    MqttConfig,
    Responder,
    TERMINAL_KINDS,
    TopicSpace,
    build_card,
    classify_reply,
    get_correlation_data,
    make_properties,
)
from a2a_over_mqtt.protocol import REPLY_ARTIFACT, REPLY_SUBMITTED, REPLY_TEXT

from slife.a2a.card import AgentCard
from slife.a2a.config import A2AConfig
from slife.a2a.identity import AgentName
from slife.a2a.task_store import get_store

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 15.0
_RETRY_DELAY = 1.0
_MAX_INFLIGHT = 16
#: Standard TASK_RESPONSE continuity for inbound tasks: the SDK emits the
#: "submitted" ack, then a long harness turn (LLM thinking, often > 30 s) would
#: otherwise leave the response stream silent past a standard requester's
#: stream_idle_timeout (30 s) — the peer would time out the stream even though
#: we are still working.  Emit a "working" status update every interval until
#: the completion bridge resolves, short enough that any idle-keeping requester
#: resets on each ping.
_WORKING_KEEPALIVE_S = 25.0

# A2A-over-MQTT requester retry profile (the profile's standard defaults,
# implemented by the SDK's Requester — mirrored here for the push model):
# first reply within 15 s, retry with exponential backoff 1000/2000/4000 ms
# ± 20 % jitter, at most 3 attempts.  Retries reuse the same Task.id +
# context_id (same payload) and generate a fresh Correlation Data.
_REPLY_FIRST_TIMEOUT_S = 15.0
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = [1.0, 2.0, 4.0]
_JITTER_FACTOR = 0.2
_MAX_TRACKED_SENDS = 500  # delivery-tracking cap (mirrors the task store)


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with ±20 % jitter for retry attempt (0-indexed)."""
    base = _BACKOFF_BASE_S[min(attempt, len(_BACKOFF_BASE_S) - 1)]
    jitter = base * _JITTER_FACTOR * (2 * random.random() - 1)
    return base + jitter


class _OutboundSend:
    """One outbound message being delivered with the requester retry profile.

    ``record`` distinguishes TASK_REQUEST (tracked in the task store, result
    auto-pushed as a task completion) from MESSAGE (a conversational send —
    no store record; the peer's reply pushes as a message)."""

    __slots__ = (
        "task_id", "agent", "session", "payload", "attempts",
        "delivered", "loops", "record",
    )

    def __init__(
        self, task_id: str, agent: str, session: str, payload: str,
        *, record: bool = True,
    ) -> None:
        self.task_id = task_id
        self.agent = agent
        self.session = session
        self.payload = payload
        self.attempts = 1
        self.delivered = False
        self.record = record
        self.loops: set[asyncio.Task] = set()


class _InboundWait:
    """Outcome holder for one inbound task awaiting harness completion."""

    __slots__ = ("outcome", "_event")

    def __init__(self) -> None:
        # (cancelled, result) — ``None`` until resolved (or externally cancelled).
        self.outcome: tuple[bool, str] | None = None
        self._event = asyncio.Event()

    def resolve(self, result: str, cancelled: bool) -> bool:
        """Record the outcome (exactly-once).  Returns False if already set."""
        if self.outcome is not None:
            return False
        self.outcome = (cancelled, result)
        self._event.set()
        return True

    async def wait(self) -> tuple[bool, str]:
        await self._event.wait()
        assert self.outcome is not None
        return self.outcome


class MeshResponder(Responder):
    """SDK :class:`Responder` bridged to the harness's out-of-band completion.

    The SDK runs the full protocol lifecycle inside ``run()`` (ack → working →
    artifact → terminal, per-task dedup, ``CancelTask``, LWT).  slife completes
    a task from OUTSIDE the request scope — the harness drains the task to the
    agent loop and the model answers, possibly many turns later, by sending a
    ``message_type="task_response"`` message that resolves ``on_request``'s
    blocked outcome — so ``on_request`` enqueues the task and blocks on a
    per-task outcome.
    """

    def __init__(self, *args, mesh: "A2AMesh", **kwargs) -> None:
        super().__init__(*args, max_concurrent=_MAX_INFLIGHT, **kwargs)
        self._mesh = mesh
        self._completion: dict[str, _InboundWait] = {}

    async def on_request(
        self, request: A2ARequest, stream,
    ) -> str | None:
        task_id = str(request.task_id)
        waiter = _InboundWait()
        self._completion[task_id] = waiter

        # TASK_RESPONSE continuity: keep the standard stream alive while the
        # harness works on the turn (see _WORKING_KEEPALIVE_S).  Stops the
        # moment the outcome resolves; a dead connection just ends the pings.
        async def _keepalive() -> None:
            try:
                while waiter.outcome is None:
                    await asyncio.sleep(_WORKING_KEEPALIVE_S)
                    if waiter.outcome is not None:
                        return
                    try:
                        await stream("task acknowledged — working on it")
                    except Exception:
                        return  # connection is gone; stop pinging
            except asyncio.CancelledError:
                pass

        pinger = asyncio.create_task(_keepalive())
        try:
            message_type = request.variables.get("message_type", "task_request")
            if message_type == "task_response":
                # A received TASK_RESPONSE is NOT a new task: a peer that
                # streams its result reaches us via the requester's response
                # topic (classified as a result push); a stray response-typed
                # message on our request topic is acknowledged silently and
                # never enqueued as an inbound task.  Return "" so the SDK
                # completes it (empty artifact, terminal) without a record.
                logger.info(
                    "a2a_inbound_response_ignored task=%s from=%s",
                    task_id, request.sender or "unknown",
                )
                return None
            if message_type == "message":
                # MESSAGE: a conversational message — not a task.  Enqueue it
                # task-less (no completion expected, no bridge) and finish:
                # the SDK empty-completes it, and the peer's real reply
                # arrives as a NEW inbound message of its own.
                self._mesh.on_inbound_task(
                    request.sender or "unknown", request.text, "",
                    kind="message",
                )
                return None
            self._mesh.on_inbound_task(
                request.sender or "unknown", request.text, task_id,
            )
            try:
                cancelled, result = await waiter.wait()
            except BaseException:
                # The SDK cancelled this task (peer CancelTask, or shutdown).
                # Surface a harness preempt (Esc-equivalent) only when it was
                # an external cancel — not our own completion and not shutdown.
                if waiter.outcome is None and not self._mesh._closing:
                    self._mesh.on_peer_cancel(task_id)
                raise
            if cancelled:
                raise asyncio.CancelledError("task cancelled by harness")
            return result
        finally:
            pinger.cancel()
            self._completion.pop(task_id, None)

    def resolve(self, task_id: str, result: str, cancelled: bool) -> str:
        """Resolve an inbound task's completion bridge.

        Returns ``"ok"``, or an error string.  Refuses unknown or
        already-completed task ids — each task completes exactly once (the SDK
        publishes its final status exactly once).
        """
        waiter = self._completion.get(task_id)
        if waiter is None:
            return (
                f"Error: unknown task_id {task_id!r} — no inbound task with "
                f"that id is awaiting a result."
            )
        if not waiter.resolve(result or "", cancelled):
            return f"Error: task_id {task_id!r} already completed."
        return "ok"


class _PresencePeer:
    __slots__ = ("name", "status")

    def __init__(self, name: str, status: str) -> None:
        self.name = name
        self.status = status  # "online" | "offline"


def _user_property(props, key: str) -> str:
    """Read an MQTT v5 user property (list of (k, v) tuples)."""
    if props is None:
        return ""
    user_props = getattr(props, "UserProperty", None)
    if not user_props:
        return ""
    for k, v in user_props:
        if k == key:
            return str(v)
    return ""


def _presence_status(props) -> str:
    """Map the standard ``a2a-status`` presence property to online/offline.

    Absent property (a card without presence stamping) defaults to online;
    ``lwt`` (LWT offline card) maps to offline.
    """
    status = _user_property(props, "a2a-status")
    if not status:
        return "online"
    return "offline" if status == "offline" or status == "lwt" else "online"


def _reply_state(data: dict) -> str:
    """Store status implied by a terminal reply's raw wire state."""
    result = data.get("result") or {}
    status = result.get("statusUpdate", {}).get("status", {})
    if not status:
        status = result.get("task", {}).get("status", {})
    state = (status.get("state") or "").upper()
    if state == "TASK_STATE_COMPLETED":
        return "completed"
    if state == "TASK_STATE_CANCELED":
        return "cancelled"
    return "failed"


class A2AMesh:
    """Standard A2A mesh driver — inbound responder + thin outbound client.

    The plugin sets the four harness callbacks, then :meth:`connect` /
    :meth:`disconnect`.  Tools call :meth:`send_message`, :meth:`cancel_task`,
    :meth:`list_agents`, :meth:`complete_task`.
    """

    def __init__(self, config: A2AConfig) -> None:
        self._config = config
        self._topics = TopicSpace(
            org=config.org or "default", unit=config.unit or "default",
        )
        self.agent_name = config.agent_name
        self._mqtt_cfg = MqttConfig(
            host=config.broker_host, port=config.broker_port,
        )
        self._card = build_card(
            name=self.agent_name,
            description="slife agent — A2A mesh peer",
            url=f"mqtt://{config.broker_host}:{config.broker_port}",
            input_modes=["text/plain"],
            output_modes=["text/plain"],
            # Cheaper insurance for a future same-name-collision detector than
            # today's system — flagged, not solved.
            extensions=[{"instance": uuid.uuid4().hex}],
        )
        self._responder = MeshResponder(
            agent_id=self.agent_name, mqtt=self._mqtt_cfg,
            topics=self._topics, card=self._card, mesh=self,
        )

        self._outbound: aiomqtt.Client | None = None
        self._connected = False
        self._closing = False
        self._ready = asyncio.Event()  # outbound connected + subscribed
        self._responder_ready = asyncio.Event()  # inbound responder subscribed
        self._tasks: list[asyncio.Task] = []
        self._peers: dict[str, _PresencePeer] = {}
        self._pending: dict[str, str] = {}  # corr → last artifact text
        self._sends: dict[str, _OutboundSend] = {}  # task_id → delivery state
        self._corr_to_task: dict[str, str] = {}  # correlation → task_id

        # Harness callbacks — replaced by the plugin before connect().
        self.on_inbound_task = (
            lambda source, content, task_id, kind="task": None
        )
        self.on_task_completion = (
            lambda corr_id, result, cancelled, peer, kind="task": None
        )
        self.on_agent_change = lambda card, event: None
        self.on_peer_cancel = lambda task_id: None
        self.on_broadcast_event = lambda sender, text: None

    # ── Connection ──────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """Whether the outbound connection is live (health probe trivium)."""
        return self._connected

    @property
    def status(self) -> str:
        return "online" if self._connected else "offline"

    @property
    def broker_address(self) -> str:
        return f"{self._config.broker_host}:{self._config.broker_port}"

    async def connect(self) -> None:
        """Start the inbound responder and the outbound driver.

        Returns once the outbound connection is established — the broker probe
        in the plugin gate already confirmed reachability, so a failure here is
        a genuine error surfaced to the caller.  Raises on connect timeout.
        """
        await self.disconnect()
        self._closing = False
        self._ready = asyncio.Event()
        self._responder_ready = asyncio.Event()
        self._tasks.append(asyncio.create_task(self._run_responder()))
        self._tasks.append(asyncio.create_task(self._run_outbound()))
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=_CONNECT_TIMEOUT)
            # The SDK responder subscribes its request topic BEFORE publishing
            # the retained online card — so our own online card sighted on the
            # discovery wildcard means inbound delivery is live.  Without this
            # gate, a send issued right after connect() could be dropped before
            # the responder subscribed (recovered only by the 15 s delivery
            # retry).
            await asyncio.wait_for(
                self._responder_ready.wait(), timeout=_CONNECT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await self.disconnect()
            raise RuntimeError(
                f"A2A mesh connect timeout — is the broker "
                f"{self.broker_address} up?"
            ) from None

    async def disconnect(self) -> None:
        """Stop both connections; inflight inbound tasks get cancelled by the
        SDK (their cancel noise is suppressed by the ``_closing`` gate)."""
        self._closing = True
        tasks, self._tasks = self._tasks, []
        loops = [loop for send in self._sends.values() for loop in send.loops]
        for task in tasks + loops:
            if not task.done():
                task.cancel()
        if tasks or loops:
            await asyncio.gather(*(tasks + loops), return_exceptions=True)
        self._sends.clear()
        self._corr_to_task.clear()
        self._connected = False
        self._outbound = None

    async def _run_responder(self) -> None:
        """The SDK responder: own connection, presence, inbound lifecycle.

        run() ends when the connection drops (the SDK has no reconnect) — loop
        until shutdown so a broker hiccup re-advertises presence + requests.
        """
        while not self._closing:
            try:
                await self._responder.run()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._closing:
                    break
                logger.warning(
                    "a2a_responder_disconnected err=%s retry=%.1fs", e,
                    _RETRY_DELAY,
                )
                try:
                    await asyncio.sleep(_RETRY_DELAY)
                except asyncio.CancelledError:
                    raise

    async def _run_outbound(self) -> None:
        """Persistent outbound client: sends, reply listening, discovery.

        Subscribes discovery wildcard + our reply sessions; reconnects on drop
        until shutdown.  Publishes requests only — the responder owns presence.
        """
        kwargs = self._mqtt_cfg.client_kwargs(
            identifier=(
                f"{self._topics.org}/{self._topics.unit}/{self.agent_name}-out"
            ),
        )
        while not self._closing:
            try:
                async with aiomqtt.Client(**kwargs) as client:
                    await client.subscribe(
                        self._topics.discovery_wildcard(), qos=1,
                    )
                    await client.subscribe(
                        self._topics.reply(self.agent_name, "+"), qos=1,
                    )
                    await client.subscribe(
                        self._topics.event("+"), qos=1,
                    )
                    self._outbound = client
                    self._connected = True
                    self._ready.set()
                    logger.info(
                        "a2a_outbound_connected id=%s broker=%s",
                        kwargs["identifier"], self.broker_address,
                    )
                    try:
                        async for msg in client.messages:
                            self._handle_message(msg)
                    except asyncio.CancelledError:
                        raise
                    except (aiomqtt.MqttError, AttributeError, ValueError):
                        pass  # connection dropped → reconnect below
                    except Exception:
                        # A handler/parse bug must not masquerade as a
                        # disconnect (that would spin reconnects); log and
                        # keep consuming.
                        logger.exception("a2a_outbound_loop_error")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._closing:
                    break
                logger.debug(
                    "a2a_outbound_disconnected err=%s retry=%.1fs", e,
                    _RETRY_DELAY,
                )
            finally:
                self._connected = False
                self._outbound = None
            if not self._closing:
                try:
                    await asyncio.sleep(_RETRY_DELAY)
                except asyncio.CancelledError:
                    raise

    # ── Inbound routing ─────────────────────────────────────────────────

    def _handle_message(self, msg) -> None:
        try:
            category = msg.topic.value.split("/")[2]
        except IndexError:
            return
        if category == "discovery":
            self._handle_discovery(msg)
        elif category == "reply":
            self._handle_reply(msg)
        elif category == "event":
            self._handle_event(msg)

    def _handle_event(self, msg) -> None:
        """Fire-and-forget broadcast event (no reply, no task_id).

        The payload carries ``{"sender": …, "text": …}`` so receivers can
        attribute a broadcast whose topic's final segment is the shared
        ``broadcast`` id (not the publisher's address).
        """
        raw = msg.payload.decode("utf-8", "replace") if msg.payload else ""
        if not raw:
            return
        try:
            data = json.loads(raw)
            sender = data.get("sender", "")
            text = data.get("text", "")
        except (ValueError, TypeError):
            return  # a broadcast we don't recognise is dropped
        if sender == self.agent_name:
            return  # our own broadcast echo
        if not text:
            return
        self.on_broadcast_event(sender or "unknown", text)

    def _handle_discovery(self, msg) -> None:
        topic = msg.topic.value
        try:
            agent_id = topic.rsplit("/", 1)[1]
        except IndexError:
            return
        if agent_id == self.agent_name:
            # Our own retained card echo.  The SDK responder publishes it
            # AFTER subscribing its request topic, so an ONLINE sighting is the
            # deterministic "inbound subscription live" signal connect() gates
            # on.  Never cached/announced as a peer.
            if _presence_status(msg.properties) == "online":
                self._responder_ready.set()
            return
        status = _presence_status(msg.properties)
        prev = self._peers.get(agent_id)
        if prev is not None and prev.status == status:
            return  # no transition → no event
        self._peers[agent_id] = _PresencePeer(agent_id, status)
        if prev is None and status == "offline":
            # A cold retained offline card — the peer was already gone BEFORE
            # we subscribed (mosquitto re-sends every retained card on
            # subscribe) — is not a transition: cache it so the roster knows,
            # but don't announce a fake "went offline" (dead cards from past
            # sessions would otherwise fire endless ✗ offline lines).  The
            # peer's first ONLINE card IS a transition and will announce.
            logger.debug("a2a_presence_cold_offline agent=%s", agent_id)
            return
        event = "online" if status == "online" else "offline"
        self.on_agent_change(
            AgentCard(agent_name=AgentName(agent_id), status=status), event,
        )

    def _handle_reply(self, msg) -> None:
        corr = get_correlation_data(msg)
        if not corr:
            return
        task_id = self._corr_to_task.get(corr)
        if task_id is None:
            # A reply for an unknown/expired send — nothing to route.
            logger.debug("a2a_reply_unknown_corr corr=%s", corr)
            return
        send = self._sends.get(task_id)
        if send is not None:
            send.delivered = True  # any classified reply confirms delivery
        raw = msg.payload.decode("utf-8", "replace") if msg.payload else ""
        if not raw:
            return
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return
        kind, content = classify_reply(data)
        if kind in (REPLY_SUBMITTED, REPLY_TEXT):
            return
        if kind == REPLY_ARTIFACT:
            # The final artifact precedes the terminal status event; hold its
            # text so the terminal (which carries no message) has a result.
            self._pending[task_id] = content
            return
        if kind not in TERMINAL_KINDS:
            return
        result = self._pending.pop(task_id, "") or content
        state = _reply_state(data)
        cancelled = state == "cancelled"
        if send is not None and not send.record:
            # MESSAGE conversation: no task record was created — push the
            # reply as a message, not a task completion.
            self.on_task_completion(
                task_id, result, cancelled, send.agent, "message",
            )
        else:
            # TASK_REQUEST: record the terminal state on the task store.
            record = get_store().get(task_id)
            if record is None:
                return
            if state == "cancelled":
                get_store().record_cancel(task_id)
            elif state == "completed":
                get_store().record_result(task_id, result)
            else:
                get_store().record_error(task_id, result or "task failed")
            self.on_task_completion(
                task_id, result, cancelled, record.agent_name, "task",
            )
        # Terminal reply → the send is finished; stop tracking its delivery.
        self._sends.pop(task_id, None)

    # ── Outbound operations (LLM-facing, via the plugin) ────────────────

    async def _publish_props(
        self, topic: str, payload: str, props, *, qos: int = 1,
    ) -> None:
        """Publish with MQTT v5 ResponseTopic + CorrelationData properties —
        required: the receiving ``validate_a2a_request`` rejects requests
        without them."""
        client = self._outbound
        if client is None or not self._connected:
            raise RuntimeError("A2A mesh is not connected.")
        await client.publish(topic, payload, qos=qos, properties=props)

    async def _publish_attempt(self, send: _OutboundSend, corr: str) -> None:
        """One requester-profile attempt — fresh Correlation Data, same
        payload + session response topic."""
        self._corr_to_task[corr] = send.task_id
        props = make_properties(
            response_topic=self._topics.reply(self.agent_name, send.session),
            correlation_data=corr,
        )
        await self._publish_props(
            self._topics.request(send.agent), send.payload, props,
        )

    async def _deliver(self, send: _OutboundSend) -> None:
        """Requester retry profile for one outbound task.

        Re-publish the same payload (same Task.id + context_id) with a fresh
        Correlation Data until a first reply arrives or the attempts are
        exhausted.  A peer that already received (and deduped) the request
        answers a retry with a ``working`` replay — which counts as the first
        reply, confirming delivery while the task keeps running.
        """
        try:
            try:
                await asyncio.sleep(_REPLY_FIRST_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            while not (self._closing or send.delivered):
                if send.attempts >= _MAX_ATTEMPTS:
                    logger.warning(
                        "a2a_delivery_exhausted task=%s attempts=%d",
                        send.task_id, send.attempts,
                    )
                    return
                await asyncio.sleep(_backoff_delay(send.attempts - 1))
                if self._closing or send.delivered:
                    return
                send.attempts += 1
                try:
                    await self._publish_attempt(send, uuid.uuid4().hex[:16])
                except Exception as e:
                    logger.debug(
                        "a2a_delivery_retry_failed task=%s err=%s",
                        send.task_id, e,
                    )
                    continue  # still under the attempts budget — try again
                logger.info(
                    "a2a_delivery_retry task=%s attempt=%d", send.task_id,
                    send.attempts,
                )
                try:
                    await asyncio.sleep(_REPLY_FIRST_TIMEOUT_S)
                except asyncio.CancelledError:
                    raise
        except asyncio.CancelledError:
            raise
        finally:
            # Keep the primary correlation (corr == task_id) mapped so a late
            # reply still routes — a push model never abandons a result.  Drop
            # only the per-attempt retry correlations.
            for corr, tid in list(self._corr_to_task.items()):
                if tid == send.task_id and corr != send.task_id:
                    self._corr_to_task.pop(corr, None)
            task = asyncio.current_task()
            if task is not None:
                send.loops.discard(task)

    async def send_message(
    self, agent: str, message: str, *, message_type: str = "task_request",
) -> str:
        """Standard ``SendMessage`` — returns the task_id immediately.

        ``message_type`` rides the request's ``metadata.variables`` so a peer
        can classify the message: only ``"task_request"`` creates a task
        (recorded + result pushed); ``"message"`` is a bare conversation (not
        recorded, replies pushed as messages); ``"task_response"`` is unused
        here (completed via :meth:`complete_task`).  Delivery follows the
        requester retry profile; the result is pushed back later
        (:meth:`_handle_reply` terminal → :attr:`on_task_completion`).
        """
        task_id = uuid.uuid4().hex
        session = uuid.uuid4().hex[:12]
        request = A2ARequest(
            text=message, request_id=task_id, task_id=task_id,
            sender=self.agent_name,
            variables={"message_type": message_type},
        )
        record = message_type == "task_request"
        if record:
            get_store().record_send(task_id, agent, message, "mqtt")
        send = _OutboundSend(
            task_id, agent, session, request.to_json(), record=record,
        )
        if len(self._sends) >= _MAX_TRACKED_SENDS:
            # Evict the oldest tracked send (delivery loop keeps running on the
            # send object it captured, but its replies can no longer route).
            oldest = next(iter(self._sends))
            self._sends.pop(oldest, None)
        self._sends[task_id] = send
        loop = asyncio.create_task(self._deliver(send))
        send.loops.add(loop)
        # First attempt — correlation == the public task_id.
        await self._publish_attempt(send, task_id)
        return task_id

    async def cancel_task(self, agent: str, task_id: str) -> str:
        """Standard ``CancelTask``; returns the resulting status string."""
        record = get_store().get(task_id)
        if record is None:
            return "not_found"
        if record.status in ("completed", "failed", "cancelled"):
            return record.status
        session = uuid.uuid4().hex[:12]
        props = make_properties(
            response_topic=self._topics.reply(self.agent_name, session),
            correlation_data=task_id,
        )
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": task_id,
            "method": "CancelTask",
            "params": {"id": task_id},
        })
        await self._publish_props(self._topics.request(agent), payload, props)
        return "cancelled"

    async def broadcast(self, event: str) -> None:
        """Publish a fire-and-forget event (QoS 0) on the unit's event topic.

        Subscribers receive it as an inbound ``[A2A:…]`` event without a
        task_id — no reply is expected, and the payload's ``sender`` lets a
        receiver filter its own echo.
        """
        payload = json.dumps(
            {"sender": self.agent_name, "text": event}, ensure_ascii=False,
        )
        await self._publish_plain(self._topics.event("broadcast"), payload, qos=0)

    async def _publish_plain(
        self, topic: str, payload: str, *, qos: int = 0,
    ) -> None:
        """Publish without MQTT v5 request/response properties."""
        client = self._outbound
        if client is None or not self._connected:
            raise RuntimeError("A2A mesh is not connected.")
        await client.publish(topic, payload, qos=qos)

    def list_agents(self) -> list[AgentCard]:
        """Own card first, then each discovered peer (by name).  Pure cache
        read — never side-effects a connection (health-probe requirement)."""
        cards = [AgentCard(
            agent_name=AgentName(self.agent_name), status=self.status,
        )]
        for peer in sorted(self._peers.values(), key=lambda p: p.name):
            cards.append(AgentCard(
                agent_name=AgentName(peer.name), status=peer.status,
            ))
        return cards

    def complete_task(
        self, task_id: str, result: str = "", cancelled: bool = False,
    ) -> str:
        """Resolve an inbound task's bridge (backs the ``task_response`` send)."""
        return self._responder.resolve(task_id, result, cancelled)