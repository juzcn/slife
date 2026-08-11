"""AgentCard — identity + liveness announcement, wire-conformant.

The card carries the slife identity/liveness fields (``agent_id``,
``display_name``, ``status``) plus the official A2A ``AgentCard`` fields
(name, description, url, version, capabilities, skills) so the presence
payload mirrors the canonical shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from slife.a2a.identity import AgentId


@dataclass
class AgentCard:
    """Who is this agent and is it alive right now?"""

    agent_id: AgentId
    display_name: str = ""
    status: str = "idle"  # "idle" or "busy"

    # Official A2A AgentCard fields (mirror a2a_pb2) — wire conformant.
    protocol_version: str = "0.3.0"
    name: str = ""
    description: str = ""
    url: str = ""
    version: str = ""
    capabilities: dict = field(
        default_factory=lambda: {
            "streaming": False,
            "push_notifications": False,
        },
    )
    skills: list = field(default_factory=list)

    @classmethod
    def create(cls, agent_id: AgentId, display_name: str = "", status: str = "idle") -> "AgentCard":
        return cls(agent_id=agent_id, display_name=display_name, status=status)

    def to_dict(self) -> dict:
        """Serialize as the presence wire payload (official + slife fields)."""
        d: dict = {
            "protocolVersion": self.protocol_version,
            "name": self.name or self.display_name,
            "description": self.description,
            "url": self.url,
            "version": self.version,
            "capabilities": self.capabilities,
            "skills": self.skills,
        }
        # Slife extensions — read by the peer watchdog, format_presence_line
        # and duplicate-id detection.
        d["agent_id"] = str(self.agent_id)
        d["display_name"] = self.display_name
        d["status"] = self.status
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "AgentCard":
        """Parse a presence wire payload back into an :class:`AgentCard`."""
        return cls(
            agent_id=AgentId(data.get("agent_id", "?")),
            display_name=data.get("display_name", ""),
            status=data.get("status", "idle"),
            protocol_version=data.get("protocolVersion", "0.3.0"),
            name=data.get("name", ""),
            description=data.get("description", ""),
            url=data.get("url", ""),
            version=data.get("version", ""),
            capabilities=data.get("capabilities", {}),
            skills=data.get("skills", []),
        )


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
