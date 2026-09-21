"""Tests for slife.timeutil.normalize_time_bound — the shared since/until
time-window bound grammar used by memdb and memfiles search tools."""

import datetime as _dt

import pytest; pytestmark = pytest.mark.unit
from dateutil.relativedelta import relativedelta

from slife.timeutil import InvalidTimeBound, normalize_time_bound


def _yesterday() -> str:
    return (_dt.date.today() - _dt.timedelta(days=1)).isoformat()


def _today() -> str:
    return _dt.date.today().isoformat()


def _tomorrow() -> str:
    return (_dt.date.today() + _dt.timedelta(days=1)).isoformat()


class TestRelativeWords:
    """Day words are resolved against the local calendar."""

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

    def test_internal_whitespace_collapsed(self):
        assert normalize_time_bound("  last   month ", role="since") == (
            normalize_time_bound("last month", role="since")
        )


class TestPeriodWords:
    """``last|this <period>`` anchors to the period's EDGE.

    The bug this pins: the obvious implementation — ``today -
    relativedelta(months=1)`` — lands on the same day-of-month, which for a
    mid-month today is 20-odd days away from the period boundary the phrase
    actually names.
    """

    def test_last_month_since_is_first_day(self):
        assert normalize_time_bound("last month", role="since") == (
            _dt.date.today().replace(day=1) - relativedelta(months=1)
        ).isoformat()

    def test_last_month_until_is_the_exclusive_upper_bound(self):
        """The period's last day, then the usual +1-day `until` rounding —
        the bound excludes the next period, not the last day of this one."""
        assert normalize_time_bound("last month", role="until") == (
            _dt.date.today().replace(day=1)
        ).isoformat()

    def test_last_month_is_not_today_minus_one_month(self):
        """The naive reading is a different instant — never what we return."""
        naive = (_dt.date.today() - relativedelta(months=1)).isoformat()
        assert normalize_time_bound("last month", role="since") != naive

    def test_since_and_until_of_one_period_differ(self):
        """One word, two instants — bounded from either end."""
        since = normalize_time_bound("last month", role="since")
        until = normalize_time_bound("last month", role="until")
        assert since < until

    def test_this_month_spans_the_current_month(self):
        today = _dt.date.today()
        assert normalize_time_bound("this month", role="since") == (
            today.replace(day=1).isoformat()
        )
        assert normalize_time_bound("this month", role="until") == (
            today.replace(day=1) + relativedelta(months=1)
        ).isoformat()

    def test_last_week_is_monday_to_sunday(self):
        today = _dt.date.today()
        monday = today - _dt.timedelta(days=today.weekday())
        assert normalize_time_bound("last week", role="since") == (
            monday - _dt.timedelta(days=7)
        ).isoformat()
        assert normalize_time_bound("last week", role="until") == monday.isoformat()

    def test_last_quarter_starts_a_quarter_boundary(self):
        today = _dt.date.today()
        q_first = _dt.date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        expected = q_first - relativedelta(months=3)
        assert normalize_time_bound("last quarter", role="since") == expected.isoformat()
        assert (expected.month - 1) % 3 == 0  # a real quarter start

    def test_last_year(self):
        today = _dt.date.today()
        assert normalize_time_bound("last year", role="since") == (
            _dt.date(today.year - 1, 1, 1).isoformat()
        )
        assert normalize_time_bound("last year", role="until") == (
            _dt.date(today.year, 1, 1).isoformat()
        )

    @pytest.mark.parametrize("period,months,days", [
        ("week", 0, 7), ("month", 1, 0), ("quarter", 3, 0), ("year", 12, 0),
    ])
    def test_since_is_a_period_start_and_until_the_next(self, period, months, days):
        """Whatever today is: `since` lands on a period start, and the span it
        opens is exactly one period — `until` being the next start, which is
        the exclusive bound the +1-day rounding produces."""
        since = _dt.date.fromisoformat(
            normalize_time_bound(f"last {period}", role="since")
        )
        until = _dt.date.fromisoformat(
            normalize_time_bound(f"last {period}", role="until")
        )
        assert since < until
        expected_next = (
            since + relativedelta(months=months) if months
            else since + _dt.timedelta(days=days)
        )
        assert until == expected_next
        if period == "week":
            assert since.weekday() == 0                    # Monday
        elif period == "month":
            assert since.day == 1
        elif period == "quarter":
            assert since.day == 1 and since.month in (1, 4, 7, 10)
        else:
            assert (since.month, since.day) == (1, 1)


class TestAgoOffsets:
    """``<N> <unit> ago`` is a POINT, measured back from today — deliberately
    not snapped to a period boundary the way ``last <period>`` is."""

    def test_days_ago(self):
        expected = (_dt.date.today() - _dt.timedelta(days=3)).isoformat()
        assert normalize_time_bound("3 days ago", role="since") == expected

    def test_singular_unit(self):
        expected = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        assert normalize_time_bound("1 day ago", role="since") == expected

    def test_weeks_ago(self):
        expected = (_dt.date.today() - _dt.timedelta(weeks=2)).isoformat()
        assert normalize_time_bound("2 weeks ago", role="since") == expected

    def test_months_ago_clamps_at_month_ends(self):
        """relativedelta's calendar arithmetic, clamping included."""
        expected = (_dt.date.today() - relativedelta(months=5)).isoformat()
        assert normalize_time_bound("5 months ago", role="since") == expected

    def test_years_ago(self):
        expected = (_dt.date.today() - relativedelta(years=2)).isoformat()
        assert normalize_time_bound("2 years ago", role="since") == expected

    def test_ago_until_advances_like_any_date_only_until(self):
        """A bare date reaching an `until` against a timestamp column still
        gets the +1-day rounding — the offset resolves first, then the
        existing granularity rule applies."""
        assert normalize_time_bound("3 days ago", role="until") == (
            _dt.date.today() - _dt.timedelta(days=2)
        ).isoformat()


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

    def test_period_until_advances_day(self):
        """A period end is a bare date too, so the same rounding applies —
        which is what makes the period bound an exclusive upper bound."""
        last_day = _dt.date.today().replace(day=1) - _dt.timedelta(days=1)
        assert normalize_time_bound("last month", role="until") == (
            last_day + _dt.timedelta(days=1)
        ).isoformat()

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

    def test_period_until_not_advanced(self):
        """On a date column the period's last day IS the bound — no +1."""
        assert normalize_time_bound(
            "last month", role="until", granularity="date",
        ) == (
            _dt.date.today().replace(day=1) - _dt.timedelta(days=1)
        ).isoformat()


class TestInvalidBounds:
    """Unrecognized input RAISES.

    It used to pass through unchanged "so the SQL layer can reject it" — but
    SQLite does not reject it: ``created_at >= '上个月'`` is a string
    comparison that matches nothing, so a bound nobody understood was
    indistinguishable from a genuine no-match.  A Chinese relative word was a
    guaranteed empty result for exactly this reason.
    """

    def test_garbage_raises(self):
        with pytest.raises(InvalidTimeBound):
            normalize_time_bound("not-a-date", role="since")

    def test_garbage_raises_on_date_granularity_too(self):
        with pytest.raises(InvalidTimeBound):
            normalize_time_bound("not-a-date", role="until", granularity="date")

    def test_impossible_iso_date_raises(self):
        """Looks like ISO, isn't — previously a silent empty result."""
        with pytest.raises(InvalidTimeBound):
            normalize_time_bound("2026-13-45", role="since")

    def test_chinese_relative_word_raises_rather_than_matching_nothing(self):
        with pytest.raises(InvalidTimeBound):
            normalize_time_bound("昨天", role="since")

    def test_unknown_unit_raises(self):
        with pytest.raises(InvalidTimeBound):
            normalize_time_bound("3 fortnights ago", role="since")

    def test_unknown_period_raises(self):
        with pytest.raises(InvalidTimeBound):
            normalize_time_bound("last decade", role="since")

    def test_message_names_the_bound_and_the_grammar(self):
        with pytest.raises(InvalidTimeBound) as e:
            normalize_time_bound("whenever", role="until")
        msg = str(e.value)
        assert "'whenever'" in msg and "until" in msg
        assert "last|this week|month|quarter|year" in msg
