"""A2A transport abstraction — protocol-agnostic message bus.

Defines the :class:`TransportAdapter` ABC that all A2A transports
(MQTT, HTTP Streamable, etc.) must implement.  The interface is
topic-based pub/sub — MQTT's native model, which HTTP Streamable
emulates by mapping topics to HTTP endpoint paths.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass
class TransportMessage:
    """A decoded message delivered through an A2A transport.

    Minimal surface: *topic* (routing key) and *payload* (UTF-8 body).
    Transport-specific metadata (QoS, retain, etc.) is private to the
    adapter implementation — consumers only see these two fields.
    """

    topic: str
    payload: str


class TransportAdapter(ABC):
    """Protocol-agnostic A2A transport — message bus abstraction.

    Implementations:
        * :class:`~slife.a2a.mqtt.MQTTAdapter` — MQTT (paho-mqtt)
        * :class:`~slife.a2a.http.HttpStreamableTransport` —
          HTTP Streamable (``mcp`` library ``streamablehttp_client``)

    Lifecycle::

        adapter = SomeTransport("agent-1")
        await adapter.connect("host", port)
        await adapter.subscribe("Slife/+/presence")
        await adapter.publish("Slife/agent-1/presence", payload)
        async for msg in adapter.messages("Slife/+/presence"):
            print(msg.topic, msg.payload)
        await adapter.disconnect()
    """

    @abstractmethod
    async def connect(self, host: str, port: int) -> None:
        """Open the transport connection to *host*:*port*.

        Must be idempotent — calling on an already-connected adapter
        is a no-op.
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close the transport connection.

        Must be idempotent — calling on an already-disconnected adapter
        is a no-op.
        """
        ...

    @abstractmethod
    async def publish(
        self, topic: str, payload: str, qos: int = 1,
    ) -> None:
        """Publish *payload* to *topic*.

        Args:
            topic: Destination topic (e.g. ``"Slife/desk-01/tasks/inbox"``).
            payload: UTF-8 string body.
            qos: Quality-of-service hint.  MQTT honours this directly;
                HTTP transports ignore it (always best-effort).
        """
        ...

    @abstractmethod
    async def subscribe(self, topic: str, qos: int = 1) -> None:
        """Subscribe to *topic* (supports MQTT-style wildcards).

        After subscribing, matching messages are delivered via
        :meth:`messages`.
        """
        ...

    @abstractmethod
    def messages(self, topic_filter: str) -> AsyncIterator[TransportMessage]:
        """Yield messages matching *topic_filter* as they arrive.

        Must be an async generator — consumers iterate with
        ``async for msg in adapter.messages(filter)``.

        Returns immediately (does not block waiting for the first
        message).  Exits when the transport disconnects.
        """
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """``True`` when the transport session is currently active."""
        ...
