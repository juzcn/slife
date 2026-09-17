"""Shared time-window bound normalization for search tools.

Every LLM-visible ``since`` / ``until`` search bound speaks one grammar.
This module is the single implementation of that grammar plus the
column-granularity rounding:

- relative words LLMs pass verbatim (``today`` / ``yesterday`` /
  ``tomorrow`` / ``now``) are resolved against the local calendar;
- offset-aware ISO datetimes are converted to the local offset so a
  lexicographic comparison against locally-stored timestamps does not
  misorder across the offset boundary;
- a date-only ``until`` advances by one day against a **timestamp**
  column (``created_at >= '2026-07-20'`` would exclude records that
  sort after the bare date), but is left alone against a **date** column
  (``date <= '2026-07-20'`` already includes the whole day).

Used by memdb (granularity ``"datetime"``, ``created_at``) and memfiles
(granularity ``"date"``, the diary ``date`` column).  Unparseable input
passes through unchanged so the SQL layer can reject it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

#: Relative-date words LLMs may pass verbatim despite being told to
#: compute ISO datetimes.  We convert them server-side so a time-window
#: search doesn't silently return zero (or wrong) results.  ``now`` is NOT
#: cached here: it is a time-of-day, not a calendar date — see
#: :func:`normalize_time_bound`.
_RELATIVE_DATES: dict[str, str] = {
    "today": "",
    "yesterday": "",
    "tomorrow": "",
}


def normalize_time_bound(
    value: str,
    *,
    role: str = "since",
    granularity: str = "datetime",
) -> str:
    """Normalize an LLM-supplied ``since``/``until`` bound for comparison.

    ``role`` is the bound's name ("since" or "until"), used by the
    date-only-``until`` rounding.  ``granularity`` matches the column being
    compared: ``"datetime"`` (a timestamp column) or ``"date"`` (a date-only
    column — values are reduced to ``YYYY-MM-DD`` and a date-only ``until``
    is not advanced).
    """
    today = date.today()

    # Populate / refresh cached relative dates.  Refresh on calendar-date
    # rollover — a long-running server must not serve yesterday's "today".
    today_iso = today.isoformat()
    if _RELATIVE_DATES["today"] != today_iso:
        _RELATIVE_DATES["today"] = today_iso
        _RELATIVE_DATES["yesterday"] = (today - timedelta(days=1)).isoformat()
        _RELATIVE_DATES["tomorrow"] = (today + timedelta(days=1)).isoformat()

    key = value.strip().lower()
    if key == "now":
        # ``now`` is a time-of-day, so it must stay fresh: day-caching it
        # would pin a search window to the day's first resolution (a
        # ``since=now`` at 14:00 after a 09:00 call would bound against 09:00).
        value = now_local_seconds()
    elif key in _RELATIVE_DATES:
        value = _RELATIVE_DATES[key]

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