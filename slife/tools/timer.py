"""Timer tool — wait N minutes, then resume automatically.

``wait_minutes`` closes the "I need to wait a few minutes" gap without blocking
the turn: it schedules a one-shot wake (``schedule_wakeup``) and returns
immediately, so the agent ends its turn and is woken later by a ``[Timer]``
inbox message carrying full prior context.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from slife.tools.base import Tool, make_params

logger = logging.getLogger(__name__)

#: Upper bound on ``wait_minutes`` — 1 day.  Anything larger is almost
#: certainly a misplaced argument (``minutes`` vs ``hours``), and an unbounded
#: value schedules a bogus multi-year timer the agent can't easily revoke.
MAX_WAIT_MINUTES = 24 * 60


class WaitMinutesTool(Tool):
    """Pause the current work and resume automatically after N minutes."""

    name: ClassVar[str] = "wait_minutes"
    category: ClassVar[str] = "System"
    description: ClassVar[str] = (
        "Pause the current work and resume automatically after N minutes."
    )
    parameters: ClassVar[dict] = make_params(
        minutes={
            "type": "integer",
            "description": "Minutes to wait (a whole number, 1 or more).",
        },
        note={
            "type": "string",
            "default": "",
            "description": (
                "What to resume — your continuation plan, e.g. \"check the "
                "deploy, then report\". The wake message relays this back to "
                "you alongside your prior context."
            ),
        },
    )

    async def execute(self, minutes: int = 0, note: str = "", **kwargs) -> str:
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            return "Error: minutes must be a whole number of minutes."
        if minutes < 1:
            return "Error: minutes must be at least 1."
        if minutes > MAX_WAIT_MINUTES:
            return (
                f"Error: minutes cannot exceed {MAX_WAIT_MINUTES} (24h). "
                "Did you mean hours?"
            )
        ctx = getattr(self, "_ctx", None)
        schedule_wakeup = (
            getattr(ctx, "schedule_wakeup", None) if ctx is not None else None
        )
        if schedule_wakeup is None:
            return (
                "Error: timers are unavailable here — wait_minutes is a "
                "main-agent tool (a subagent worker has no turn to resume)."
            )
        await schedule_wakeup(minutes * 60, note)
        return (
            f"Timer set for {minutes} minute(s). End this turn now — a "
            f"[Timer] message will wake you when it elapses."
        )
