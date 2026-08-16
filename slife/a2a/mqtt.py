"""MQTT asyncio adapter — wraps paho-mqtt with asyncio.Queue bridging.

Follows the same pattern as ``slife/mcp/client.py`` (_ReadAdapter / _WriteAdapter):
paho's threaded ``loop_start()`` delivers callbacks on a background thread;
each callback ``put_nowait()`` into an ``asyncio.Queue`` so the async side
can ``await queue.get()``.

Implements :class:`~slife.a2a.transport.TransportAdapter` so A2A protocol
code works with any transport (MQTT, HTTP Streamable, …).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time as _time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.reasoncodes import ReasonCode

from slife.a2a.transport import TransportAdapter, TransportMessage

logger = logging.getLogger(__name__)

# MQTTv5 reason-code constants
_MQTT_RC_SUCCESS = 0
# Bound each subscription's queue so a slow/hung consumer gets backpressure
# (drop the newest message with a warning) instead of unbounded growth.
_MAX_QUEUE_SIZE = 1000

def _get_mqtt():
    """Return the paho-mqtt module (paho-mqtt is a hard dependency)."""
    return mqtt


@dataclass
class MQTTMessage(TransportMessage):
    """MQTT-specific message with QoS and retain flag.

    Extends :class:`TransportMessage` with transport-level metadata.
    Can be used wherever ``TransportMessage`` is expected.
    """

    qos: int = 0
    retain: bool = False



class MQTTAdapter(TransportAdapter):
    """asyncio-friendly paho-mqtt wrapper — implements :class:`TransportAdapter`.

    Usage::

        adapter = MQTTAdapter("desk-01")
        await adapter.connect("localhost", 1883)
        await adapter.subscribe("Slife/+/presence")

        async for msg in adapter.messages("Slife/+/presence"):
            print(msg.topic, msg.payload)

        await adapter.publish("Slife/desk-01/presence", json.dumps(card))
    """

    def __init__(self, client_id: str):
        import os as _os
        # Append PID so two instances with the same agent-name don't
        # fight over the MQTT client-id at the protocol level.
        # Duplicate detection is handled at the application layer.
        self._agent_name = client_id
        self._client_id = f"{client_id}-{_os.getpid()}"
        self._client: mqtt.Client | None = None
        self._queues: dict[str, asyncio.Queue[TransportMessage]] = {}
        self._connected = False
        # Guards _queues/_subscriptions: paho's callback thread iterates them
        # while the event loop's subscribe()/messages() mutate them.  Iterating
        # a dict that changes size concurrently raises RuntimeError, and paho
        # re-raises callback exceptions, killing the network thread (silent
        # permanent message loss — no reconnect).  All access takes this lock.
        self._state_lock = threading.Lock()

        # ── Reconnect support (REVIEW H6) ─────────────────────────
        # Subscriptions tracked so they can be re-issued on reconnect
        # (clean_start=True drops them on the broker side).
        self._subscriptions: dict[str, int] = {}
        # Deliberate shutdown flag — unlike _connected it does NOT flip
        # during a transient disconnect, so message() generators survive.
        self._closed = False
        self._ever_connected = False
        # Created in __init__ (not after loop_start) to close the
        # _on_connect-before-wait race for a fast local broker.
        self._connect_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        #: App callback invoked on a REconnect (not the first connect),
        #: so the caller can re-announce presence.  Must be a coroutine
        #: function (it is scheduled on the event loop via
        #: ``run_coroutine_threadsafe``).
        self.on_reconnect: "Callable[[], Coroutine[Any, Any, None]] | None" = None

        # Keep-alive ping tracking
        self._last_publish_time = 0.0

    # ── Connection lifecycle ──────────────────────────────────────────

    async def connect(self, host: str = "localhost", port: int = 1883) -> None:
        """Connect to the MQTT broker and start the background network loop.

        Sets up LWT (Last Will Testament) so other agents see this agent
        go offline immediately if the connection drops.
        """
        if self._connected:
            return

        # Re-arm after a prior disconnect(): a reused adapter must deliver
        # messages again (messages() loops on _closed) and wait for a FRESH
        # connect signal — the previous _on_connect already set the old event,
        # so a stale one would return immediately without a real connection.
        self._closed = False
        self._connect_event = asyncio.Event()

        mq = _get_mqtt()

        lwt_topic = f"Slife/{self._agent_name}/presence"
        # agent_name is required by the peer watchdog to identify who left —
        # a bare {"status":"offline"} is dropped as "unknown peer".
        lwt_payload = json.dumps(
            {"status": "offline", "agent_name": self._agent_name},
        )

        c = mq.Client(
            CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
            protocol=mq.MQTTv5,
        )
        c.will_set(lwt_topic, lwt_payload, qos=1, retain=False)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(min_delay=5, max_delay=30)
        c.connect_async(host, port, keepalive=30)
        self._loop = asyncio.get_running_loop()
        c.loop_start()
        self._client = c

        # Wait for the connection to complete
        try:
            await self._wait_for_connection(timeout=10.0)
        except Exception:
            # A failed connect must not leak the paho thread / client — a
            # retry would otherwise stack a second client + network loop.
            self._client = None
            self._connected = False
            try:
                c.loop_stop()
            except Exception:
                pass
            raise

        self._connected = True
        self._last_publish_time = _time.monotonic()
        logger.info(
            "a2a_mqtt_connected id=%s host=%s port=%d",
            self._client_id, host, port,
        )

    async def disconnect(self) -> None:
        """Gracefully disconnect — publish offline, then stop the loop."""
        # Always mark closed so message() generators terminate, even if the
        # connection was already lost (a bare `if _connected` early-return
        # would leave them spinning forever).
        self._closed = True
        if not self._connected or self._client is None:
            return

        logger.info("a2a_mqtt_disconnecting id=%s", self._client_id)

        try:
            self._client.publish(
                f"Slife/{self._agent_name}/presence",
                json.dumps(
                    {"status": "offline", "agent_name": self._agent_name},
                ),
                qos=1,
                retain=False,
            )
        except Exception:
            pass

        self._client.loop_stop()
        self._client.disconnect()
        self._connected = False
        self._client = None
        logger.info("a2a_mqtt_disconnected id=%s", self._client_id)

    # ── Pub / Sub ─────────────────────────────────────────────────────

    async def publish(
        self,
        topic: str,
        payload: str,
        qos: int = 1,
        retain: bool = False,
    ) -> None:
        """Publish a message to a topic."""
        if self._client is None:
            raise RuntimeError("MQTT not connected")
        info = self._client.publish(topic, payload, qos=qos, retain=retain)
        if info.rc != _MQTT_RC_SUCCESS:
            logger.info(
                "a2a_mqtt_publish_fail topic=%s rc=%d", topic, info.rc,
            )
        self._last_publish_time = _time.monotonic()

    async def subscribe(self, topic: str, qos: int = 1) -> None:
        """Subscribe to a topic (supports MQTT wildcards like ``Slife/+/presence``)."""
        if self._client is None:
            raise RuntimeError("MQTT not connected")
        self._client.subscribe(topic, qos=qos)
        # Track the subscription so it can be re-issued on reconnect
        # (clean_start=True drops subscriptions on the broker side).
        with self._state_lock:
            self._subscriptions[topic] = qos
            # Create a queue for this subscription if it doesn't exist
            if topic not in self._queues:
                self._queues[topic] = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
        logger.debug("a2a_mqtt_subscribed topic=%s", topic)

    async def messages(self, topic_filter: str) -> AsyncIterator[TransportMessage]:
        """Async iterator yielding messages matching the given topic filter.

        Must call ``subscribe()`` for the same filter first.
        """
        with self._state_lock:
            queue = self._queues.get(topic_filter)
            if queue is None:
                queue = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
                self._queues[topic_filter] = queue

        # Loop on _closed (deliberate shutdown), NOT _connected: a transient
        # disconnect must not end the generator — after paho reconnects,
        # _on_connect restores _connected and re-subscribes, and the same
        # iterator keeps delivering (REVIEW H6).
        while not self._closed:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=1.0)
                yield msg
            except asyncio.TimeoutError:
                continue

    # ── Paho callbacks (run on paho's background thread) ──────────────

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.ConnectFlags,
        reason_code: ReasonCode,
        properties: Any,
    ) -> None:
        was_reconnect = self._ever_connected
        self._ever_connected = True
        self._connected = True
        # _on_connect runs on paho's background thread — asyncio.Event.set()
        # from a foreign thread doesn't reliably wake the event-loop waiter
        # (call_soon is not thread-safe), so schedule the set on the loop.
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._connect_event.set)
        else:
            self._connect_event.set()

        try:
            if was_reconnect:
                # paho auto-reconnected.  clean_start=True means the broker
                # dropped every subscription — re-issue them all, then let the
                # app re-announce presence (peers saw our LWT "offline").
                # Snapshot under the lock — subscribe() (event loop) may add
                # to _subscriptions concurrently, and mutating mid-iteration
                # would crash paho's loop thread (see _state_lock).
                with self._state_lock:
                    subs = list(self._subscriptions.items())
                for topic, qos in subs:
                    try:
                        client.subscribe(topic, qos=qos)
                        logger.debug("a2a_mqtt_resubscribed topic=%s", topic)
                    except Exception:
                        logger.warning(
                            "a2a_mqtt_resubscribe_fail topic=%s", topic, exc_info=True,
                        )
                cb = self.on_reconnect
                if cb is not None and self._loop is not None:
                    try:
                        asyncio.run_coroutine_threadsafe(cb(), self._loop)
                    except Exception:
                        logger.warning("a2a_mqtt_reconnect_cb_error", exc_info=True)
        except Exception as e:
            # Never let a callback exception escape to paho — it re-raises
            # and kills the network thread (silent, no reconnect).
            logger.warning("a2a_mqtt_on_connect_error err=%s", e)

        logger.debug(
            "a2a_mqtt_on_connect id=%s rc=%s reconnect=%s",
            self._client_id, reason_code, was_reconnect,
        )

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.DisconnectFlags,
        reason_code: ReasonCode,
        properties: Any,
    ) -> None:
        try:
            logger.debug(
                "a2a_mqtt_on_disconnect id=%s rc=%s",
                self._client_id, reason_code,
            )
            self._connected = False
        except Exception as e:
            # See _on_connect — never let a callback exception escape to paho.
            logger.warning("a2a_mqtt_on_disconnect_error err=%s", e)

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        msg: mqtt.MQTTMessage,
    ) -> None:
        """Route incoming messages to the matching asyncio.Queue(s)."""
        try:
            transport_msg = TransportMessage(
                topic=msg.topic,
                payload=msg.payload.decode("utf-8", errors="replace"),
            )

            logger.debug(
                "a2a_mqtt_on_message topic=%s len=%d",
                msg.topic, len(msg.payload),
            )

            # Route to all matching subscribed queues.  Snapshot under the
            # lock — subscribe()/messages() (event loop) may add to _queues
            # concurrently, and mutating mid-iteration would crash paho's
            # loop thread (see _state_lock).
            mq = _get_mqtt()
            with self._state_lock:
                queues = list(self._queues.items())
            matched = False
            for topic_filter, queue in queues:
                if mq.topic_matches_sub(topic_filter, msg.topic):
                    matched = True
                    try:
                        queue.put_nowait(transport_msg)
                        logger.debug(
                            "a2a_mqtt_routed topic=%s -> filter=%s",
                            msg.topic, topic_filter,
                        )
                    except asyncio.QueueFull:
                        logger.warning(
                            "a2a_mqtt_queue_full filter=%s topic=%s",
                            topic_filter, msg.topic,
                        )
            if not matched:
                logger.debug(
                    "a2a_mqtt_no_match topic=%s queues=%s",
                    msg.topic, list(self._queues.keys()),
                )
        except Exception as e:
            # Never let a callback exception escape to paho — it re-raises
            # and kills the network thread (silent, no reconnect).
            logger.warning("a2a_mqtt_on_message_error topic=%s err=%s", msg.topic, e)

    # ── Helpers ───────────────────────────────────────────────────────

    async def _wait_for_connection(self, timeout: float) -> None:
        """Spin until _on_connect signals, or timeout.

        The event is created in ``__init__`` — before ``loop_start()`` — so
        a fast local broker that connects before this coroutine runs still
        signals the same event (avoids a spurious 10s timeout).
        """
        try:
            await asyncio.wait_for(self._connect_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"MQTT connection timed out after {timeout}s"
            )

    @property
    def is_connected(self) -> bool:
        return self._connected
