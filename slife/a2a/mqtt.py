"""MQTT asyncio adapter — wraps paho-mqtt with asyncio.Queue bridging.

Follows the same pattern as ``slife/mcp/client.py`` (_ReadAdapter / _WriteAdapter):
paho's threaded ``loop_start()`` delivers callbacks on a background thread;
each callback ``put_nowait()`` into an ``asyncio.Queue`` so the async side
can ``await queue.get()``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import paho.mqtt.client as mqtt

from slife.a2a.identity import AgentId

logger = logging.getLogger(__name__)

# MQTTv5 reason-code constants
_MQTT_RC_SUCCESS = 0


@dataclass
class MQTTMessage:
    """A decoded MQTT message delivered through the async interface."""

    topic: str
    payload: str
    qos: int
    retain: bool


class MQTTAdapter:
    """asyncio-friendly paho-mqtt wrapper.

    Usage::

        adapter = MQTTAdapter("desk-01")
        await adapter.connect("localhost", 1883)
        await adapter.subscribe("Slife/+/presence")

        async for msg in adapter.messages("Slife/+/presence"):
            print(msg.topic, msg.payload)

        await adapter.publish("Slife/desk-01/presence", json.dumps(card))
    """

    def __init__(self, client_id: str):
        self._client_id = client_id
        self._client: mqtt.Client | None = None
        self._queues: dict[str, asyncio.Queue[MQTTMessage]] = {}
        self._connected = False

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

        lwt_topic = f"Slife/{self._client_id}/presence"
        lwt_payload = json.dumps({"status": "offline"})

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
            protocol=mqtt.MQTTv5,
        )
        self._client.will_set(lwt_topic, lwt_payload, qos=1, retain=False)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        self._client.connect_async(host, port, keepalive=30)
        self._client.loop_start()

        # Wait for the connection to complete
        await self._wait_for_connection(timeout=10.0)

        self._connected = True
        self._last_publish_time = _time.monotonic()
        logger.info(
            "a2a_mqtt_connected id=%s host=%s port=%d",
            self._client_id, host, port,
        )

    async def disconnect(self) -> None:
        """Gracefully disconnect — publish offline, then stop the loop."""
        if not self._connected or self._client is None:
            return

        logger.info("a2a_mqtt_disconnecting id=%s", self._client_id)

        try:
            self._client.publish(
                f"Slife/{self._client_id}/presence",
                json.dumps({"status": "offline"}),
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
            logger.warning(
                "a2a_mqtt_publish_fail topic=%s rc=%d", topic, info.rc,
            )
        self._last_publish_time = _time.monotonic()

    async def subscribe(self, topic: str, qos: int = 1) -> None:
        """Subscribe to a topic (supports MQTT wildcards like ``Slife/+/presence``)."""
        if self._client is None:
            raise RuntimeError("MQTT not connected")
        self._client.subscribe(topic, qos=qos)
        # Create a queue for this subscription if it doesn't exist
        if topic not in self._queues:
            self._queues[topic] = asyncio.Queue()
        logger.debug("a2a_mqtt_subscribed topic=%s", topic)

    async def messages(self, topic_filter: str) -> AsyncIterator[MQTTMessage]:
        """Async iterator yielding messages matching the given topic filter.

        Must call ``subscribe()`` for the same filter first.
        """
        queue = self._queues.get(topic_filter)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[topic_filter] = queue

        while self._connected:
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
        reason_code: mqtt.ReasonCode,
        properties: Any,
    ) -> None:
        self._connect_event.set()
        logger.debug(
            "a2a_mqtt_on_connect id=%s rc=%s",
            self._client_id, reason_code,
        )

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: Any,
    ) -> None:
        logger.debug(
            "a2a_mqtt_on_disconnect id=%s rc=%s",
            self._client_id, reason_code,
        )
        self._connected = False

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        msg: mqtt.MQTTMessage,
    ) -> None:
        """Route incoming messages to the matching asyncio.Queue(s)."""
        mqtt_msg = MQTTMessage(
            topic=msg.topic,
            payload=msg.payload.decode("utf-8", errors="replace"),
            qos=msg.qos,
            retain=msg.retain,
        )

        logger.debug(
            "a2a_mqtt_on_message topic=%s len=%d",
            msg.topic, len(msg.payload),
        )

        # Route to all matching subscribed queues
        matched = False
        for topic_filter, queue in self._queues.items():
            if mqtt.topic_matches_sub(topic_filter, msg.topic):
                matched = True
                try:
                    queue.put_nowait(mqtt_msg)
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

    # ── Helpers ───────────────────────────────────────────────────────

    async def _wait_for_connection(self, timeout: float) -> None:
        """Spin until _on_connect signals, or timeout."""
        self._connect_event = asyncio.Event()
        try:
            await asyncio.wait_for(self._connect_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"MQTT connection timed out after {timeout}s"
            )

    @property
    def is_connected(self) -> bool:
        return self._connected
