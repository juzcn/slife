"""Unified identity markers — a single structured text marker per turn.

A restored turn's user message carries one optional ``[Kind:{json}]`` marker
prepended to the real text.  The marker is the single carrier of "who/what
this turn was" for the LLM and for restore-time code (display, filtering,
footnotes) — replacing ad-hoc text sniffing of ``[Heartbeat]`` /
``[Schedule …]`` prefixes and the bare ``channel`` string guess that caused
the ``subagent(a2a)`` mislabel.

Design:
  - Grammar: ``[Kind:{json}]`` — the kind is a tag, the payload is JSON.
    ``[Heartbeat:{}]`` (empty object) when there is no payload — never a
    bare ``[Heartbeat]`` or ``[Heartbeat:]`` (avoids parser ambiguity).
  - **human is the default**: a message with no marker is human.  Only
    non-human kinds carry a marker.
  - The marker is generated at **restore time** from the persisted
    ``channel`` column; the live message stream is untouched (it keeps its
    own ``[Turn: N]`` / ``[TrimContext: N]`` markers).
  - ``render_marker`` and ``parse_marker`` are pure inverses.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Kind of a message whose identity is unknown / not one of the known kinds.
UNKNOWN = "Unknown"
#: The operator at the keyboard (TUI).  Never marked — absence == human.
HUMAN = "Human"
#: WeChat peer terminal.
WECHAT = "Wechat"
#: Internal autonomous heartbeat trigger.
HEARTBEAT = "Heartbeat"
#: Internal scheduled-task trigger.
SCHEDULE = "Schedule"
#: Local subagent (worker) completion.
SUBAGENT = "Subagent"
#: Remote A2A peer.
REMOTE = "Remote"

#: ``[Kind:{json}]  remainder`` — kind, JSON payload, then the real text.
#: ``re.DOTALL`` so a payload containing newlines still matches.
_KIND_TAG = re.compile(r"^\[(\w+):(\{.*\})\]\s?(.*)$", re.DOTALL)


def render_marker(kind: str, **payload: Any) -> str:
    """Build the ``[Kind:{json}]`` marker for *kind* with *payload*.

    An empty payload renders as ``[Kind:{}]`` so the marker is always
    parseable (never a bare ``[Kind]``).  Keys are JSON-sorted for a
    stable, deterministic marker.
    """
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return f"[{kind}:{body}]"


def parse_marker(text: str) -> tuple[dict[str, Any] | None, str]:
    """Split *text* into ``(identity, remainder)``.

    Returns ``(None, text)`` when *text* has no marker — i.e. it is a
    human turn (absence == human).  When a marker is present, returns
    ``({"kind": ..., **payload}, remainder_text)``.

    Also tolerates the legacy live prefixes ``[Heartbeat]`` and
    ``[Schedule …]`` (no JSON payload) so restore of old rows is uniform.
    """
    m = _KIND_TAG.match(text)
    if not m:
        kind, rest = _match_legacy(text)
        if kind is None:
            return None, text
        return {"kind": kind}, rest
    kind, payload, rest = m.group(1), m.group(2), m.group(3)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"kind": kind, **data}, rest


def _match_legacy(text: str) -> tuple[str | None, str]:
    """Match the legacy no-JSON live prefixes: ``[Heartbeat]`` / ``[Schedule …]``."""
    if text.startswith("[Heartbeat]"):
        return HEARTBEAT, text[len("[Heartbeat]"):].lstrip()
    if text.startswith("[Schedule"):
        # ``[Schedule name]`` / ``[Schedule missed]`` — kind + optional name.
        m = re.match(r"^\[Schedule\s+([^\]]+)\]\s?(.*)$", text, re.DOTALL)
        if m:
            return SCHEDULE, m.group(2)
        return SCHEDULE, text[len("[Schedule"):].lstrip()
    return None, text
