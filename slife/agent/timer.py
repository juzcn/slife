"""One-shot timer — an in-memory "wake me in N minutes" primitive.

Unlike scheduled tasks (cron → subagent worker), a timer resumes the *main
agent itself*: the ``wait_minutes`` tool schedules a background
``asyncio.sleep`` and, when it elapses, posts a ``[Timer]`` message into the
unified inbox.  Because every inbox message runs against the main agent's one
shared history, the wake is a fresh turn with full prior context.

The timer is deliberately in-memory — it dies with the process.  Anything that
must survive a restart is a scheduled task, not a timer.
"""

from __future__ import annotations

#: TUI filter mark — a turn whose user message starts with this is a timer
#: wake (synthetic, filtered from the chat view like heartbeat / schedule).
TIMER_MARK = "[Timer]"


def is_timer_trigger(text: str) -> bool:
    """True when *text* is a timer wake — a synthetic resume turn, not a real
    user query."""
    return text.startswith(TIMER_MARK)


def timer_text(minutes: float, note: str) -> str:
    """Build the wake message posted into the inbox when a timer elapses.

    The first line keeps the ``[Timer]`` prefix — ``TIMER_MARK`` /
    ``is_autonomous_trigger`` rely on it.  *note* is the continuation plan the
    agent wrote when it set the timer; it is a belt-and-suspenders reminder on
    top of the shared context the wake turn already carries.
    """
    note = (note or "").strip()
    tail = f" Resume: {note}" if note else " Resume what you were doing."
    return f"{TIMER_MARK} Your {minutes:g}-minute timer elapsed.{tail}"
