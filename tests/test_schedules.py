"""Schedule expression wrapper: validation, next-run, timezone policy."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from slife.schedules import ScheduleError, is_valid, next_run


# ── Validation ──────────────────────────────────────────────────────

def test_valid_expressions():
    assert is_valid("0 9 * * *")
    assert is_valid("*/15 * * * *")
    assert is_valid("0 9,18 * * 1,3,5")
    assert is_valid("0 9-17 * * *")
    assert is_valid("0 9 1 * 1")  # dom OR dow


def test_invalid_expressions():
    assert not is_valid("60 * * * *")    # minute out of range
    assert not is_valid("0 24 * * *")    # hour out of range
    assert not is_valid("0 9 * 13 *")    # month out of range
    assert not is_valid("0 9 0 * *")     # dom out of range
    assert not is_valid("0 x * * *")     # non-numeric
    assert not is_valid("*/0 * * * *")   # step 0
    assert not is_valid("0 9 * *")       # wrong field count


def test_dow_7_sunday_accepted():
    # croniter accepts dow=7 as an alias for Sunday (common extension);
    # a schedule on Sunday fires on the 7th (Sunday) too.
    assert is_valid("0 9 * * 7")
    r = next_run("0 9 * * 7", datetime(2026, 1, 1, 0, 0))
    assert r.weekday() == 6  # Sunday


def test_invalid_cross_field():
    # Feb 31 never fires — caught only in strict mode
    assert is_valid("0 9 31 2 *")
    assert not is_valid("0 9 31 2 *", strict=True)


def test_next_run_raises_on_bad_expr():
    with pytest.raises(ScheduleError):
        next_run("60 * * * *", datetime(2026, 1, 1))
    with pytest.raises(ScheduleError):
        next_run("0 9 31 2 *", datetime(2026, 1, 1), strict=True)


# ── Next-run computation ────────────────────────────────────────────

def test_every_minute():
    assert next_run("* * * * *", datetime(2026, 1, 1, 0, 0, 0)) == \
        datetime(2026, 1, 1, 0, 1, 0).astimezone()


def test_hourly_at_minute_0():
    assert next_run("0 * * * *", datetime(2026, 1, 1, 9, 0, 30)) == \
        datetime(2026, 1, 1, 10, 0, 0).astimezone()


def test_daily_at_9():
    assert next_run("0 9 * * *", datetime(2026, 1, 1, 8, 59)) == \
        datetime(2026, 1, 1, 9, 0).astimezone()
    assert next_run("0 9 * * *", datetime(2026, 1, 1, 9, 0, 1)) == \
        datetime(2026, 1, 2, 9, 0).astimezone()


def test_strictly_after_on_tick():
    # exactly on the tick → next day, not the same instant
    assert next_run("0 9 * * *", datetime(2026, 1, 1, 9, 0, 0)) == \
        datetime(2026, 1, 2, 9, 0).astimezone()


def test_weekday_only():
    # every Monday 9am (dow=1). 2026-01-05 is a Monday.
    assert next_run("0 9 * * 1", datetime(2026, 1, 1, 0, 0)) == \
        datetime(2026, 1, 5, 9, 0).astimezone()
    assert next_run("0 9 * * 1", datetime(2026, 1, 5, 10, 0)) == \
        datetime(2026, 1, 12, 9, 0).astimezone()


def test_dom_and_dow_or():
    # day 1 OR Monday (croniter default day_or=True). 2026-02-01 is Sunday.
    assert next_run("0 9 1 * 1", datetime(2026, 1, 30, 0, 0)) == \
        datetime(2026, 2, 1, 9, 0).astimezone()


def test_monthly_on_1st():
    assert next_run("0 9 1 * *", datetime(2026, 1, 15)) == \
        datetime(2026, 2, 1, 9, 0).astimezone()


def test_leap_day():
    assert next_run("0 9 29 2 *", datetime(2026, 3, 1)) == \
        datetime(2028, 2, 29, 9, 0).astimezone()


def test_stepped_minutes():
    assert next_run("*/15 * * * *", datetime(2026, 1, 1, 9, 3)) == \
        datetime(2026, 1, 1, 9, 15).astimezone()


# ── Timezone policy ─────────────────────────────────────────────────

def test_naive_after_treated_local():
    r = next_run("0 9 * * *", datetime(2026, 1, 1, 8, 0))
    assert r.tzinfo is not None  # result is tz-aware
    assert r.hour == 9 and r.day == 1


def test_explicit_tz():
    r = next_run("0 9 * * *", datetime(2026, 1, 1, 0, 0), tz="Asia/Shanghai")
    assert r.tzinfo is not None
    assert r.utcoffset().total_seconds() == 8 * 3600  # +08:00


def test_after_converted_to_target_tz():
    # naive local after (e.g. 2026-01-01 09:00 local) in Asia/Shanghai
    local = datetime(2026, 1, 1, 9, 0).astimezone()
    sh = local.astimezone(ZoneInfo("Asia/Shanghai"))
    r = next_run("0 9 * * *", local, tz="Asia/Shanghai")
    assert r.tzinfo is ZoneInfo("Asia/Shanghai")
    # the first 09:00 Shanghai strictly after local 09:00 is next day
    # (local is ahead of Shanghai) or same day depending on zone; assert
    # only that the result is a 09:00 in Shanghai and strictly after.
    assert r.hour == 9 and r.minute == 0
    assert r > sh
