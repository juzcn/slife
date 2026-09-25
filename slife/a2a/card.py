"""AgentCard — slife's display/presence view of an A2A mesh agent.

The official A2A Agent Card lives in the ``a2a-over-mqtt`` SDK
(``build_card`` / ``parse_card``) — the wire is the SDK's, not ours.  This
module is the slife display layer on top of it: a minimal ``AgentCard``
(identity + online/offline status) plus the shared presence-line rendering
used by the TUI (:mod:`slife.ui.app`) and the per-turn prompt
(:mod:`slife.agent.system_prompt`) so neither drifts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from slife.a2a.identity import AgentName

# Control characters a remote peer could use to break out of a single display
# line in the turn prompt, TUI, or logs (newlines, tabs, ESC, …).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _safe_name(value: object) -> str:
    """Display-safe form of a remote, untrusted peer value.

    Presence fields (``agent_name``, ``status``) come from the MQTT wire with
    no validation.  Strip control characters — which would otherwise let a
    peer inject instructions into the per-turn prompt — and cap the
    length so a name cannot bloat the context.
    """
    s = _CONTROL_RE.sub(" ", str(value))
    return " ".join(s.split())[:128]


@dataclass
class AgentCard:
    """Who is this agent and is it alive right now?

    ``status`` is one of ``"online"`` / ``"offline"`` on the wire (the
    standard ``a2a-status`` presence, mapped by the mesh).  The dataclass
    default keeps the historic ``"idle"`` for constructed-but-unset cards.
    """

    agent_name: AgentName
    status: str = "idle"  # "online" or "offline"


def format_presence_line(card: "AgentCard", event: str) -> str | None:
    """Render a presence event exactly as the TUI shows it.

    Returns ``None`` for events that are not user-visible transitions
    (``"status_change"`` — a heartbeat from an already-known peer) so callers
    can filter them out.

    Used by both the TUI (:mod:`slife.ui.app`) and the per-turn prompt
    (:mod:`slife.agent.system_prompt`) so the two never drift.
    """
    name = _safe_name(card.agent_name)
    status = _safe_name(card.status)
    if event == "online":
        return f"⚡ {name} online [{status}]"
    if event == "offline":
        return f"✗ {name} offline"
    return None