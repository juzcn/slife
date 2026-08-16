"""A2A configuration — parsed from the ``a2a`` section of slife.json5.

Follows the same pattern as ``MCPConfig`` (slife/config.py:92-152).
"""

from __future__ import annotations

import logging
import platform
import os
from dataclasses import dataclass, field


logger = logging.getLogger(__name__)


def _default_agent_name() -> str:
    """Auto-generate an agent id from hostname + pid."""
    host = platform.node().split(".")[0] or "unknown"
    return f"{host}-{os.getpid()}"


@dataclass
class A2AConfig:
    """Configuration for the A2A P2P mesh."""

    enabled: bool = False
    """Master switch — A2A is off by default."""

    agent_name: str = field(default_factory=_default_agent_name)
    """Unique id in the mesh.  Auto-generated when not set in json5.

    This is also the agent's only identity — there is no separate display
    name (a duplicate was pure context pollution).
    """

    transport: str = "mqtt"
    """Transport type.  Only ``"mqtt"`` (default) is implemented — any other
    value (e.g. the removed ``"http"`` skeleton) disables A2A with a warning
    at config load instead of crashing startup."""

    broker_host: str = "localhost"
    broker_port: int = 1883

    http_host: str = "127.0.0.1"
    """HTTP Streamable server host (only used when ``transport == "http"``)."""

    http_port: int = 0
    """HTTP Streamable server port (0 = auto-assign, only when ``transport == "http"``)."""

    heartbeat_interval: int = 15
    """Seconds between presence heartbeat publishes."""

    heartbeat_timeout: int = 45
    """Seconds of silence before marking a peer as offline (3 × heartbeat)."""

    task_timeout: int = 120
    """Seconds to wait for a remote task result."""

    @classmethod
    def from_dict(
        cls, data: dict | None, agent_name: str = "slife",
    ) -> "A2AConfig":
        """Parse the ``a2a`` section from slife.json5.

        A2A over MQTT is enabled **at runtime** when Mosquitto is detected
        on ``broker_host:broker_port``.  The json5 ``a2a`` section always
        provides connection details — ``enabled`` is set to ``True`` only
        after a successful TCP probe.

        Args:
            data: The ``a2a`` dict from the JSON5 config, or ``None``.
            agent_name: The ``--agent`` value (defaults to ``"slife"``).
                      Used as the MQTT client id / agent identity.

        Note:
            A ``transport`` other than ``"mqtt"`` (e.g. a future gRPC/HTTP
            binding) disables A2A (``enabled=False``) and logs a warning —
            it never crashes startup.
        """
        broker = {}
        if isinstance(data, dict):
            broker = data.get("broker", {}) if isinstance(data.get("broker"), dict) else {}

        # The a2a section provides connection details only.
        # A2A enablement is decided at runtime by the Mosquitto TCP probe —
        # the json5 a2a section never carries an "enabled" field.
        # When data is None (no a2a section), enabled stays False —
        # start_a2a() won't even attempt a probe.
        default_enabled = isinstance(data, dict)

        transport = (data or {}).get("transport", "mqtt")
        enabled = default_enabled
        if transport != "mqtt":
            # Only MQTT is implemented.  A config requesting any other
            # transport must not crash startup — parse the section,
            # disable A2A, and surface a warning.
            logger.warning(
                "a2a_transport_unsupported transport=%s action=a2a_disabled "
                "supported=('mqtt',)",
                transport,
            )
            enabled = False

        return cls(
            enabled=enabled,  # downgraded at runtime on probe failure
            agent_name=agent_name,
            transport=transport,
            broker_host=broker.get("host", "localhost"),
            broker_port=broker.get("port", 1883),
            http_host=(data or {}).get("http_host", "127.0.0.1"),
            http_port=(data or {}).get("http_port", 0),
            heartbeat_interval=(data or {}).get("heartbeat_interval", 15),
            heartbeat_timeout=(data or {}).get("heartbeat_timeout", 45),
            task_timeout=(data or {}).get("task_timeout", 120),
        )
