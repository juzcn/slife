"""A2A (Agent-to-Agent) — two transports, one protocol.

MQTT for remote instances, HTTP Streamable for local/remote HTTP,
stdin/stdout for local subagents.
The LLM sees one agent pool via the unified A2A toolset in
:mod:`slife.tools.a2a`.

All tools are proper :class:`Tool` subclasses, auto-discovered at startup.
They use module-level transport references set by :class:`AgentService`.
"""

from slife.a2a.card import AgentCard
from slife.a2a.client import A2AClient
from slife.a2a.config import A2AConfig
from slife.a2a.identity import AgentId, AgentMessage, HUMAN, SUBAGENT
from slife.a2a.mqtt import MQTTAdapter
from slife.a2a.transport import TransportAdapter, TransportMessage
from slife.tools.a2a import (  # noqa: F401 — re-export for slife.a2a namespace
    ListSubagentsTool,
    NotifyUserTool,
    SpawnSubagentTool,
    StopSubagentTool,
)

__all__ = [
    "A2AClient",
    "A2AConfig",
    "AgentCard",
    "AgentId",
    "AgentMessage",
    "HUMAN",
    "ListSubagentsTool",
    "MQTTAdapter",
    "SpawnSubagentTool",
    "StopSubagentTool",
    "SUBAGENT",
    "TransportAdapter",
    "TransportMessage",
]
