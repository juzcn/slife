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


def format_presence_line(card: "AgentCard", event: str) -> str | None:
    """Render a presence event exactly as the TUI shows it.

    Returns ``None`` for events that are not user-visible transitions
    (``"status_change"`` — a heartbeat from an already-known peer, fired
    every ``heartbeat_interval``) so callers can filter them out.

    Used by both the TUI (:mod:`slife.ui.app`) and the per-turn context
    footer (:mod:`slife.agent.system_prompt`) so the two never drift.
    """
    if event == "online":
        name = card.display_name or card.agent_id
        extra = (
            f" ({card.agent_id})"
            if card.display_name and card.display_name != card.agent_id
            else ""
        )
        return f"⚡ {name}{extra} online [{card.status}]"
    if event == "offline":
        return f"✗ {card.agent_id} offline"
    if event == "timeout":
        return f"⏱ {card.agent_id} timed out"
    return None
