"""Shared time-window bound normalization for search tools.

Every LLM-visible ``since`` / ``until`` search bound speaks one grammar.
This module is the single implementation of that grammar plus the
column-granularity rounding:

- relative words LLMs pass verbatim are resolved against the local calendar:
  the day words (``today`` / ``yesterday`` / ``tomorrow`` / ``now``), the
  calendar periods ``last|this week|month|quarter|year``, and offsets of the
  form ``<N> day(s)|week(s)|month(s)|year(s) ago``;
- **a period word anchors to the period's EDGE, not to today's day-of-month.**
  ``since=last month`` means the FIRST of last month and ``until=last month``
  its LAST day — the same word is two different instants, and neither is
  ``today - 1 month`` (which for a mid-month today lands 20-odd days off);
- offset-aware ISO datetimes are converted to the local offset so a
  lexicographic comparison against locally-stored timestamps does not
  misorder across the offset boundary;
- a date-only ``until`` advances by one day against a **timestamp**
  column (``created_at >= '2026-07-20'`` would exclude records that
  sort after the bare date), but is left alone against a **date** column
  (``date <= '2026-07-20'`` already includes the whole day).

**Unrecognized input raises :class:`InvalidTimeBound`.**  It used to pass
through unchanged, on the stated theory that "the SQL layer can reject it" —
but SQLite does not reject it.  ``created_at >= '上个月'`` is a string
comparison that matches nothing, so a bound nobody understood produced the
same empty result as a real no-match: the one outcome a search bound must
never produce silently.  Passing garbage through also meant a Chinese
relative word (``昨天``) was a guaranteed empty result.

Month arithmetic uses ``dateutil.relativedelta`` — ``timedelta`` has no month
or year unit, because a month is not a fixed number of days.  That is a
declared dependency (see ``pyproject.toml``); it used to arrive only
transitively, via ``croniter``.

Used by memdb (granularity ``"datetime"``, ``created_at``) and memfiles
(granularity ``"date"``, the diary ``date`` column).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from dateutil.relativedelta import relativedelta


class InvalidTimeBound(ValueError):
    """A ``since``/``until`` bound written in no grammar this module speaks."""


#: The grammar this module accepts, for tool ``description=`` (an expression
#: context — a tool's *docstring* must stay a literal, or CPython files no
#: docstring at all).  Single-sourced so what the LLM is told and what is
#: implemented cannot drift.
BOUND_GRAMMAR = (
    "ISO date/datetime, or one of: today, yesterday, tomorrow, now, "
    "last|this week|month|quarter|year, "
    "'<N> day(s)|week(s)|month(s)|year(s) ago'"
)


#: Day words, as name -> offset in days from today.
_DAY_WORDS: dict[str, int] = {"yesterday": -1, "today": 0, "tomorrow": 1}

#: Calendar periods ``last``/``this`` may name, as name -> months per period
#: (``week`` is 0: weeks are day-based, see :func:`_shift_period`).
_PERIOD_MONTHS: dict[str, int] = {
    "week": 0, "month": 1, "quarter": 3, "year": 12,
}

_PERIOD_RE = re.compile(r"^(last|this)\s+(week|month|quarter|year)$")
_AGO_RE = re.compile(r"^(\d+)\s+(day|week|month|year)s?\s+ago$")


def _bound_error(role: str, value: str) -> str:
    return f"invalid {role} bound {value!r} — expected {BOUND_GRAMMAR}"


def _shift_period(start: date, period: str, count: int) -> date:
    """Shift a period's first day by *count* whole periods.

    *start* must already BE a period start, so the month arithmetic cannot
    clamp (``day=1`` never overflows a shorter month).
    """
    if period == "week":
        return start + timedelta(weeks=count)
    return start + relativedelta(months=_PERIOD_MONTHS[period] * count)


def _period_start(day: date, period: str) -> date:
    """The first day of the period containing *day* (weeks start Monday)."""
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    if period == "quarter":
        return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)
    if period == "year":
        return date(day.year, 1, 1)
    raise InvalidTimeBound(_bound_error("since", period))


def _resolve_period(which: str, period: str, role: str, today: date) -> date:
    """Resolve ``last|this <period>`` to an edge of that period.

    ``since`` takes the period's first day, ``until`` its last — so the same
    word bounds the window from either end rather than naming one instant.
    """
    current = _period_start(today, period)
    if which == "this":
        first = current
    else:                                   # "last" — the period before
        first = _shift_period(current, period, -1)
    if role == "until":
        return _shift_period(first, period, 1) - timedelta(days=1)
    return first


def _resolve_ago(count: int, unit: str, today: date) -> date:
    """Resolve ``<N> <unit> ago`` — a POINT, deliberately not period-anchored.

    ``3 days ago`` names a day, not a period, so unlike ``last week`` it is
    measured back from today rather than snapped to a boundary.  Month/year
    shifts clamp at month ends (``2026-03-31`` minus a month is ``2026-02-28``),
    which is the arithmetic ``relativedelta`` exists to provide.
    """
    if unit == "day":
        return today - timedelta(days=count)
    if unit == "week":
        return today - timedelta(weeks=count)
    if unit == "month":
        return today - relativedelta(months=count)
    return today - relativedelta(years=count)


def _resolve_relative(key: str, role: str, today: date) -> date | None:
    """The date a relative phrase names, or None if it is not one."""
    if key in _DAY_WORDS:
        return today + timedelta(days=_DAY_WORDS[key])
    m = _PERIOD_RE.match(key)
    if m:
        return _resolve_period(m.group(1), m.group(2), role, today)
    m = _AGO_RE.match(key)
    if m:
        return _resolve_ago(int(m.group(1)), m.group(2), today)
    return None


def _is_iso(value: str) -> bool:
    """True if *value* is an ISO date or datetime we can compare against.

    ``datetime.fromisoformat`` covers bare dates too (midnight), and from
    3.11 accepts the ``Z`` suffix as well as explicit offsets.
    """
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def normalize_time_bound(
    value: str,
    *,
    role: str = "since",
    granularity: str = "datetime",
) -> str:
    """Normalize an LLM-supplied ``since``/``until`` bound for comparison.

    ``role`` is the bound's name ("since" or "until"); it anchors a period
    word to the period's first or last day, and drives the date-only-``until``
    rounding.  ``granularity`` matches the column being compared:
    ``"datetime"`` (a timestamp column) or ``"date"`` (a date-only column —
    values are reduced to ``YYYY-MM-DD`` and a date-only ``until`` is not
    advanced).

    Raises :class:`InvalidTimeBound` for input in no known grammar.
    """
    today = date.today()

    # Internal whitespace collapsed, so "last   month" reads as "last month".
    # No caching of the resolved words: they are derived from `today`, which
    # this reads per call, so a long-running server cannot serve a stale one.
    key = " ".join(value.strip().lower().split())

    if key == "now":
        # ``now`` is a time-of-day, not a calendar date — resolved to a full
        # timestamp so a ``since=now`` is not pinned to the start of the day.
        value = now_local_seconds()
    else:
        resolved = _resolve_relative(key, role, today)
        if resolved is not None:
            value = resolved.isoformat()
        elif not _is_iso(value.strip()):
            raise InvalidTimeBound(_bound_error(role, value))

    if granularity == "date":
        # Date-only column: reduce a datetime bound to its date part so it
        # compares against ``date`` values.  No +1-day advance — a date-only
        # ``until`` already includes the whole day.
        try:
            value = date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            pass  # Not an ISO date/datetime; pass through unchanged
    elif role == "until" and len(value) == 10 and "T" not in value:
        # Date-only until against a timestamp column: advance one day so
        # records on that day are included (created_at has a time component
        # that sorts after the bare date).
        try:
            value = (date.fromisoformat(value) + timedelta(days=1)).isoformat()
        except ValueError:
            pass  # Not a valid ISO date; pass through unchanged

    # Normalize offset-aware ISO datetimes to the local offset.  Timestamps
    # are stored in local time; the LLM may pass UTC ("Z") or a different
    # offset, which would misorder a lexicographic comparison across the
    # offset boundary.  A naive datetime is already local and is left
    # unchanged.
    if "T" in value:
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is not None:
                value = local_iso_seconds(dt)
        except ValueError:
            pass  # not a parseable ISO datetime; pass through unchanged

    return value


def now_local_seconds() -> str:
    """Current wall clock as a local ISO timestamp, seconds precision.

    Slife's stored-timestamp convention (see the module docstring):
    local time, ``YYYY-MM-DDTHH:MM:SS+HH:MM``.  Every store's ``_now``
    spelled this out inline before it moved here.
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


def local_iso_seconds(dt: datetime) -> str:
    """Normalize a datetime to the local-ISO-seconds convention."""
    return dt.astimezone().isoformat(timespec="seconds")
