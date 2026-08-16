"""AgentCard — identity + liveness announcement, wire-conformant.

The card carries the slife identity/liveness fields (``agent_name``,
``status``) plus the official A2A ``AgentCard`` fields
(name, description, url, version, capabilities, skills) so the presence
payload mirrors the canonical shape.  There is no separate display name —
``agent_name`` is the identity (the ``--agent`` value); a duplicate
``display_name`` field was pure context pollution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from slife.a2a.identity import AgentName

# Control characters a remote peer could use to break out of a single display
# line in the system-prompt footer, TUI, or logs (newlines, tabs, ESC, …).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _safe_name(value: object, limit: int = 128) -> str:
    """Display-safe form of a remote, untrusted peer value.

    Presence fields (``agent_name``, ``status``) come from the MQTT wire with
    no validation.  Strip control characters — which would otherwise let a
    peer inject instructions into the per-turn context footer — and cap the
    length so a name cannot bloat the context.
    """
    s = _CONTROL_RE.sub(" ", str(value))
    return " ".join(s.split())[:limit]


@dataclass
class AgentCard:
    """Who is this agent and is it alive right now?"""

    agent_name: AgentName
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
    def create(cls, agent_name: AgentName, status: str = "idle") -> "AgentCard":
        return cls(agent_name=agent_name, status=status)

    def to_dict(self) -> dict:
        """Serialize as the presence wire payload (official + slife fields)."""
        d: dict = {
            "protocolVersion": self.protocol_version,
            "name": self.name or str(self.agent_name),
            "description": self.description,
            "url": self.url,
            "version": self.version,
            "capabilities": self.capabilities,
            "skills": self.skills,
        }
        # Slife extensions — read by the peer watchdog, format_presence_line
        # and duplicate-id detection.
        d["agent_name"] = str(self.agent_name)
        d["status"] = self.status
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "AgentCard":
        """Parse a presence wire payload back into an :class:`AgentCard`."""
        return cls(
            agent_name=AgentName(data.get("agent_name", "?")),
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
    name = _safe_name(card.agent_name)
    status = _safe_name(card.status)
    if event == "online":
        return f"⚡ {name} online [{status}]"
    if event == "offline":
        return f"✗ {name} offline"
    if event == "timeout":
        return f"⏱ {name} timed out"
    return None
