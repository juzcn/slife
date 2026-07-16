"""A2A identity types — minimal, transport-agnostic."""

from __future__ import annotations

from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field
from typing import NewType

AgentId = NewType("AgentId", str)
"""Identifies an agent.  Examples: ``"human"``, ``"sub-1"``, ``"desk-01"``."""

HUMAN = AgentId("human")
"""The operator at the keyboard."""


@dataclass
class AgentMessage:
    """A message from any agent, through any transport.

    Supports multi-terminal architecture: TUI, WeChat, MQTT, etc.
    are all peer-level input channels.  Each message carries optional
    *metadata* (channel info) and an *on_reply* callback that routes
    the agent's response back to the originating channel.
    """

    source: AgentId
    content: str
    images: list[str] = field(default_factory=list)
    reply_to: str | None = None
    correlation_id: str | None = None
    metadata: dict = field(default_factory=dict)
    on_reply: Callable[[str], Awaitable[None]] | None = None
    """Async callback invoked with the agent's response text.
    Set by input channels (WeChat, Telegram, etc.) to route replies
    back to the user.  Called after the agent loop completes."""
