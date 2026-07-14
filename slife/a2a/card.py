"""AgentCard — minimal identity + liveness announcement."""

from __future__ import annotations

from dataclasses import dataclass

from slife.a2a.identity import AgentId


@dataclass
class AgentCard:
    """Who is this agent and is it alive right now?"""

    agent_id: AgentId
    display_name: str = ""
    status: str = "idle"  # "idle" or "busy"

    @classmethod
    def create(cls, agent_id: AgentId, display_name: str = "", status: str = "idle") -> "AgentCard":
        return cls(agent_id=agent_id, display_name=display_name, status=status)
