"""A2A identity types — minimal, transport-agnostic."""

from __future__ import annotations

import json
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NewType

if TYPE_CHECKING:
    from slife.agent.loop import AgentEventHandler

AgentName = NewType("AgentName", str)
"""Identifies an agent by its name.  Examples: ``"human"``, ``"Jack"``, ``"desk-01"``."""

HUMAN = AgentName("human")
"""The operator at the keyboard."""

WECHAT = AgentName("wechat")
"""WeChat user — peer terminal, same processing pipeline as human."""

HEARTBEAT = AgentName("heartbeat")
"""Internal autonomous heartbeat — not a peer terminal."""

SYSTEM = AgentName("system")
"""slife itself — internal system turns (schedule triggers, etc.).
Not a peer terminal."""


@dataclass(frozen=True)
class Channel:
    """Durable source identity of a message entering the unified inbox.

    The channel is the *sender*: human operator, WeChat peer, subagent
    worker, heartbeat, A2A mesh peer, or slife itself.  It is orthogonal
    to markers (context annotations) and — by default — never enters the
    LLM context.  ``kind`` names the family; ``data`` carries kind-specific
    payload (A2A peer name, subagent name/task).  The payload is
    JSON-friendly and persisted per turn via :meth:`to_db` / :meth:`from_db`.
    """

    kind: str
    data: dict = field(default_factory=dict)

    # ── Factories ────────────────────────────────────────────────────

    @classmethod
    def human(cls) -> "Channel":
        """Keyboard operator in the TUI."""
        return cls("human")

    @classmethod
    def wechat(cls) -> "Channel":
        """WeChat peer terminal."""
        return cls("wechat")

    @classmethod
    def subagent(
        cls, name: str, task_id: str | None = None, scheduled: bool = False,
    ) -> "Channel":
        """Local worker completion.  ``scheduled`` marks a schedule worker."""
        return cls("subagent", {
            "name": name, "task_id": task_id, "scheduled": scheduled,
        })

    @classmethod
    def heartbeat(cls) -> "Channel":
        """Internal autonomous heartbeat."""
        return cls("heartbeat")

    @classmethod
    def a2a(cls, peer: str) -> "Channel":
        """A2A mesh peer; ``peer`` is the remote agent's name."""
        return cls("a2a", {"agent_name": peer})

    @classmethod
    def system(cls) -> "Channel":
        """slife itself (schedule trigger, etc.) — filtered from the TUI."""
        return cls("system")

    # ── Presentation / persistence ───────────────────────────────────

    def display_prefix(self) -> str | None:
        """TUI user-message prefix for this channel.

        ``None`` for the system channel — it is filtered from the live and
        restored chat view (the schedule/heartbeat trigger turns are also
        text-filtered as synthetic before this is ever consulted).
        """
        if self.kind == "system":
            return None
        if self.kind == "human":
            return "You> "
        if self.kind == "wechat":
            return "Wechat> "
        if self.kind == "heartbeat":
            return "Heartbeat> "
        if self.kind == "subagent":
            name = self.data.get("name") or "subagent"
            return f"Subagent({name})> "
        peer = self.data.get("agent_name") or self.data.get("name") or "?"
        return f"A2A({peer})"

    def to_db(self) -> tuple[str, dict]:
        """Persisted form: (``diary.channel`` identity string, payload).

        An A2A channel keeps its peer name as the identity so full-text
        search on the peer still matches the row; the name also rides the
        payload so restore renders ``A2A(<name>)`` without guessing.
        """
        if self.kind == "a2a":
            peer = self.data.get("agent_name") or ""
            return (peer or "a2a"), {"agent_name": peer}
        return self.kind, dict(self.data)

    @classmethod
    def from_db(cls, identity: str, data: "str | dict | None" = None) -> "Channel":
        """Rebuild a channel from a persisted row.

        ``identity`` is the ``diary.channel`` value: a known kind string,
        or — legacy — the raw peer name (or empty for human turns).  Any
        unknown identity, including legacy ``"schedule"`` and old peer-name
        rows, classifies as an A2A peer.  Bad payload JSON degrades to ``{}``.
        """
        parsed: dict = {}
        if isinstance(data, str):
            if data.strip():
                try:
                    parsed = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    parsed = {}
        elif isinstance(data, dict):
            parsed = data
        if not identity:
            return cls("human")
        if identity in ("human", "wechat", "subagent", "heartbeat", "a2a", "system"):
            return cls(identity, parsed)
        return cls("a2a", {"agent_name": identity, **parsed})


@dataclass
class AgentMessage:
    """A message from any agent, through any transport.

    Supports multi-terminal architecture: TUI, WeChat, MQTT, etc.
    are all peer-level input channels.  Each message carries optional
    *metadata* (channel info) and an *on_reply* callback that routes
    the agent's response back to the originating channel.
    """

    source: AgentName
    content: str
    images: list[str] = field(default_factory=list)
    reply_to: str | None = None
    correlation_id: str | None = None
    metadata: dict = field(default_factory=dict)
    # Text reply callback, optionally taking ``cancelled: bool = False``
    # so channels can signal cancellation to the sender.
    on_reply: "Callable[..., Awaitable[None]] | None" = None
    """Async callback invoked with the agent's response text.
    Set by input channels (WeChat, Telegram, etc.) to route replies
    back to the user.  Called after the agent loop completes."""

    handler: "AgentEventHandler | None" = None
    """TUI handler for streaming agent output to the chat view.
    Set by the TUI input path so each message carries its own handler
    rather than relying on a global mutable registry.  Remote A2A
    messages leave this as None and fall back to the default factory."""

    channel: "Channel | None" = None
    """Typed source identity; ``None`` falls back to the ``source`` string
    (legacy classification) for display and persistence."""
