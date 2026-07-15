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

    broker_command: str | None = None
    """Path to the mosquitto binary (optional — for auto-spawn)."""

    heartbeat_interval: int = 15
    """Seconds between presence heartbeat publishes."""

    heartbeat_timeout: int = 45
    """Seconds of silence before marking a peer as offline (3 × heartbeat)."""

    task_timeout: int = 120
    """Seconds to wait for a remote task result."""

    @classmethod
    def from_dict(
        cls, data: dict | None, agent_name: str | None = None,
    ) -> "A2AConfig":
        """Parse the ``mqtt`` section from slife.json5.

        A2A over MQTT is enabled **only** when ``--agent`` is passed on the
        CLI (``agent_name`` is not ``None``).  The json5 ``mqtt`` section
        provides broker connection details — it never enables A2A on its own.

        Args:
            data: The ``mqtt`` dict from the JSON5 config, or ``None``.
            agent_name: If provided (``--agent`` on the CLI), enables A2A
                        and uses this value as ``agent_id``.
        """
        # --agent is the only way to enable A2A over MQTT
        if agent_name is None:
            return cls()

        broker = {}
        if isinstance(data, dict):
            broker = data.get("broker", {}) if isinstance(data.get("broker"), dict) else {}

        return cls(
            enabled=True,
            agent_id=agent_name,
            agent_name=agent_name,
            broker_host=broker.get("host", "localhost"),
            broker_port=broker.get("port", 1883),
            broker_command=broker.get("command"),
            heartbeat_interval=(data or {}).get("heartbeat_interval", 15),
            heartbeat_timeout=(data or {}).get("heartbeat_timeout", 45),
            task_timeout=(data or {}).get("task_timeout", 120),
        )
