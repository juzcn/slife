"""A2A (Agent-to-Agent) — one protocol, pluggable transports.

The A2A protocol operations and data model (mirroring the official
a2a-python reference interface) with a custom transport binding (MQTT).
The LLM-facing ``a2a_*`` tools live in the a2a plugin
(:mod:`slife.plugins.a2a`), not here.

Subagents are **not** part of A2A — they are local workers (see
:mod:`slife.tools.subagent`).
"""

from slife.a2a.card import AgentCard
from slife.a2a.client import A2AClient
from slife.a2a.config import A2AConfig
from slife.a2a.identity import AgentName, AgentMessage, HUMAN
from slife.a2a.mqtt import MQTTAdapter
from slife.a2a.transport import TransportAdapter, TransportMessage

__all__ = [
    "A2AClient",
    "A2AConfig",
    "AgentCard",
    "AgentName",
    "AgentMessage",
    "HUMAN",
    "MQTTAdapter",
    "TransportAdapter",
    "TransportMessage",
]
