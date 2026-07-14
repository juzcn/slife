"""A2A (Agent-to-Agent) — two transports, one protocol.

MQTT for remote instances, stdin/stdout for local subagents.
The LLM sees one agent pool via the unified A2A toolset in
:mod:`slife.tools.a2a`.

All tools are proper :class:`Tool` subclasses, auto-discovered at startup.
They use module-level transport references set by :class:`AgentService`.
"""

from slife.a2a.card import AgentCard
from slife.a2a.client import A2AClient
from slife.a2a.config import A2AConfig
from slife.a2a.identity import AgentId, AgentMessage, HUMAN
from slife.a2a.mqtt import MQTTAdapter
from slife.a2a.tools import (  # noqa: F401
    A2ABroadcastTool,
    A2ACancelTaskTool,
    A2AGetAgentCardTool,
    A2AGetTaskResultTool,
    A2AListAgentsTool,
    A2AListSubagentsTool,
    A2AListTasksTool,
    A2ANotifyUserTool,
    A2ASendTaskAsyncTool,
    A2ASendTaskTool,
    A2ASubscribeTaskTool,
    SubagentSpawnTool,
    SubagentStopTool,
)

__all__ = [
    "A2ABroadcastTool",
    "A2ACancelTaskTool",
    "A2AClient",
    "A2AConfig",
    "A2AGetAgentCardTool",
    "A2AGetTaskResultTool",
    "A2AListAgentsTool",
    "A2AListSubagentsTool",
    "A2AListTasksTool",
    "A2ANotifyUserTool",
    "A2ASendTaskAsyncTool",
    "A2ASendTaskTool",
    "A2ASubscribeTaskTool",
    "AgentCard",
    "AgentId",
    "AgentMessage",
    "HUMAN",
    "MQTTAdapter",
    "SubagentSpawnTool",
    "SubagentStopTool",
]
