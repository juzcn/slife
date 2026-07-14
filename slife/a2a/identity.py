"""A2A identity types — minimal, transport-agnostic."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NewType

AgentId = NewType("AgentId", str)
"""Identifies an agent.  Examples: ``"human"``, ``"sub-1"``, ``"desk-01"``."""

HUMAN = AgentId("human")
"""The operator at the keyboard."""


@dataclass
class AgentMessage:
    """A message from any agent, through any transport."""

    source: AgentId
    content: str
    images: list[str] = field(default_factory=list)
    reply_to: str | None = None
    correlation_id: str | None = None
