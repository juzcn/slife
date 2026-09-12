"""Tests for slife.timeutil.normalize_time_bound — the shared since/until
time-window bound normalization used by memdb and memfiles search tools."""

import datetime as _dt

import pytest; pytestmark = pytest.mark.unit

from slife.timeutil import normalize_time_bound


def _yesterday() -> str:
    return (_dt.date.today() - _dt.timedelta(days=1)).isoformat()


def _today() -> str:
    return _dt.date.today().isoformat()


def _tomorrow() -> str:
    return (_dt.date.today() + _dt.timedelta(days=1)).isoformat()


class TestRelativeWords:
    """LLM-passed relative words are resolved against the local calendar."""

    def test_yesterday(self):
        assert normalize_time_bound("yesterday", role="since") == _yesterday()

    def test_today(self):
        assert normalize_time_bound("today", role="since") == _today()

    def test_tomorrow(self):
        assert normalize_time_bound("tomorrow", role="since") == _tomorrow()

    def test_now(self):
        result = normalize_time_bound("now", role="since")
        assert "T" in result  # full ISO datetime

    def test_case_insensitive(self):
        assert normalize_time_bound("TODAY", role="since") == _today()
        assert normalize_time_bound("Yesterday", role="since") == _yesterday()

    def test_whitespace_stripped(self):
        assert normalize_time_bound("  today  ", role="since") == _today()


class TestISOOffsets:
    """Offset-aware datetimes are converted to the local offset so a
    lexicographic comparison against local timestamps stays correct."""

    def test_offset_datetime_since(self):
        expected = _dt.datetime.fromisoformat(
            "2026-07-20T14:39:19+08:00"
        ).astimezone().isoformat(timespec="seconds")
        assert normalize_time_bound(
            "2026-07-20T14:39:19+08:00", role="since",
        ) == expected

    def test_utc_z_datetime_normalized_to_local(self):
        result = normalize_time_bound("2026-07-20T06:39:19Z", role="since")
        assert result != "2026-07-20T06:39:19Z"
        assert _dt.datetime.fromisoformat(result) == _dt.datetime.fromisoformat(
            "2026-07-20T06:39:19Z"
        )


class TestDatetimeGranularity:
    """granularity="datetime" — a timestamp column (memdb ``created_at``):
    a bare-date ``until`` must advance a day so records on that day are
    included."""

    def test_date_only_since_passthrough(self):
        assert normalize_time_bound("2026-07-20", role="since") == "2026-07-20"

    def test_date_only_until_advances_day(self):
        assert normalize_time_bound("2026-07-20", role="until") == "2026-07-21"

    def test_today_until_advances_day(self):
        # "today" resolves to a bare date first, then advances like any
        # date-only until.
        assert normalize_time_bound(
            "today", role="until",
        ) == (_dt.date.today() + _dt.timedelta(days=1)).isoformat()

    def test_datetime_until_passthrough(self):
        """Full datetime until is left alone — the caller specified the time
        explicitly."""
        assert normalize_time_bound(
            "2026-07-20T23:59:59", role="until",
        ) == "2026-07-20T23:59:59"


class TestDateGranularity:
    """granularity="date" — a date-only column (memfiles diary ``date``): a
    bare-date ``until`` already includes the whole day, so it is left alone,
    and datetime bounds are reduced to their date part."""

    def test_date_only_until_not_advanced(self):
        assert normalize_time_bound(
            "2026-07-20", role="until", granularity="date",
        ) == "2026-07-20"

    def test_date_only_since_passthrough(self):
        assert normalize_time_bound(
            "2026-07-20", role="since", granularity="date",
        ) == "2026-07-20"

    def test_datetime_reduced_to_date(self):
        assert normalize_time_bound(
            "2026-07-20T14:39:19+08:00", role="since", granularity="date",
        ) == "2026-07-20"

    def test_relative_word_reduced_to_date(self):
        assert normalize_time_bound(
            "today", role="since", granularity="date",
        ) == _dt.date.today().isoformat()

    def test_relative_now_reduced_to_date(self):
        result = normalize_time_bound(
            "now", role="since", granularity="date",
        )
        assert result == _dt.date.today().isoformat()


class TestGarbage:
    """Unparseable input passes through unchanged so the SQL layer can
    reject it."""

    def test_invalid_date_passthrough(self):
        assert normalize_time_bound("not-a-date", role="since") == "not-a-date"
        assert normalize_time_bound(
            "not-a-date", role="until", granularity="date",
        ) == "not-a-date"