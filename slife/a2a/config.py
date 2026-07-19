"""A2A configuration — parsed from the ``a2a`` section of slife.json5.

Follows the same pattern as ``MCPConfig`` (slife/config.py:92-152).
"""

from __future__ import annotations

import platform
import os
from dataclasses import dataclass, field


def _default_agent_id() -> str:
    """Auto-generate an agent id from hostname + pid."""
    host = platform.node().split(".")[0] or "unknown"
    return f"{host}-{os.getpid()}"


@dataclass
class A2AConfig:
    """Configuration for the A2A P2P mesh."""

    enabled: bool = False
    """Master switch — A2A is off by default."""

    agent_id: str = field(default_factory=_default_agent_id)
    """Unique id in the mesh.  Auto-generated when not set in json5."""

    agent_name: str = ""
    """Optional human-readable display name."""

    broker_host: str = "localhost"
    broker_port: int = 1883

    heartbeat_interval: int = 15
    """Seconds between presence heartbeat publishes."""

    heartbeat_timeout: int = 45
    """Seconds of silence before marking a peer as offline (3 × heartbeat)."""

    task_timeout: int = 120
    """Seconds to wait for a remote task result."""

    @classmethod
    def from_dict(
        cls, data: dict | None, agent_id: str = "slife",
    ) -> "A2AConfig":
        """Parse the ``mqtt`` section from slife.json5.

        A2A over MQTT is enabled **at runtime** when Mosquitto is detected
        on ``broker_host:broker_port``.  The json5 ``mqtt`` section always
        provides connection details — ``enabled`` is set to ``True`` only
        after a successful TCP probe.

        Args:
            data: The ``mqtt`` dict from the JSON5 config, or ``None``.
            agent_id: The ``--agent`` value (defaults to ``"slife"``).
                      Used as the MQTT client id / agent identity.
        """
        broker = {}
        agent_name = ""
        if isinstance(data, dict):
            broker = data.get("broker", {}) if isinstance(data.get("broker"), dict) else {}
            agent_name = data.get("agent_name", "")

        return cls(
            enabled=False,  # set to True at runtime after mosquitto probe
            agent_id=agent_id,
            agent_name=agent_name,
            broker_host=broker.get("host", "localhost"),
            broker_port=broker.get("port", 1883),
            heartbeat_interval=(data or {}).get("heartbeat_interval", 15),
            heartbeat_timeout=(data or {}).get("heartbeat_timeout", 45),
            task_timeout=(data or {}).get("task_timeout", 120),
        )
