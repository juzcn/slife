"""Subagent (agent worker) identity — the unified-inbox source sentinel.

A subagent is a local worker, not an A2A peer.  Its completion result is
posted into the parent's unified inbox under the ``SUBAGENT`` source so it
shares the human history, while remaining distinguishable from actual
human turns in memory search.
"""

from __future__ import annotations

from slife.a2a.identity import AgentName

SUBAGENT = AgentName("subagent")
"""Subagent completion — result posted by the subagent manager when an
async subagent task finishes.  Routed to the human history so the
user sees the output, but persisted with channel ``"subagent"`` so it is
distinguishable from actual human turns in memory search."""
