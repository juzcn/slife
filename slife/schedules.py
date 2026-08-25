"""Scheduled-task schedule expressions.

Thin wrapper over ``croniter`` for the scheduling surface.  ``next_run``
computes the next trigger time strictly after a reference time; the module
owns the timezone policy (explicit IANA tz via ``tzdata``, else the system
local timezone) and the error contract (:class:`ScheduleError`) so callers
never import croniter directly.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter

__all__ = ["ScheduleError", "next_run", "is_valid"]


class ScheduleError(ValueError):
    """Invalid schedule expression or unreachable trigger time."""


def _as_aware(dt: datetime) -> datetime:
    """Normalise a reference time to tz-aware local.

    Naive ``dt`` is interpreted as the system local timezone (matching the
    rest of slife, which stamps ``datetime.now().astimezone()``).
    """
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt


def next_run(
    expr: str,
    after: datetime,
    tz: str | None = None,
    *,
    strict: bool = True,
) -> datetime:
    """Return the next trigger time strictly after ``after`` (tz-aware).

    Args:
        expr: 5-field cron expression (``minute hour dom month dow``).
        after: reference time; naive is treated as system local.
        tz: Optional IANA timezone name (e.g. ``"Asia/Shanghai"``).  When
            set, triggers are computed in that zone; otherwise in the
            system local zone.
        strict: When True (default), cross-field validation is applied
            (e.g. rejects ``0 9 31 2 *`` — Feb 31 never fires).
    """
    if not is_valid(expr, strict=strict):
        raise ScheduleError(f"invalid cron expression {expr!r}")
    ref = _as_aware(after)
    if tz:
        ref = ref.astimezone(ZoneInfo(tz))
    try:
        return croniter(expr, ref, day_or=True).get_next(datetime)
    except Exception as e:  # croniter raises CroniterBadDateError etc.
        raise ScheduleError(f"no next run for {expr!r}: {e}") from e


def is_valid(expr: str, *, strict: bool = False) -> bool:
    """Return whether ``expr`` is a valid 5-field cron expression."""
    return croniter.is_valid(expr, strict=strict)
