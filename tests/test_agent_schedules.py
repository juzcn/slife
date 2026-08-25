"""Schedule-loop timing logic: trigger text, fire/miss classification,
manual fire.  Pure-timing tests use no DB; the loop's DB interaction is
exercised via a mocked memfiles client.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

from slife.agent import schedules as S


def _aware(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s).astimezone()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


# ── trigger text ─────────────────────────────────────────────────────

def test_trigger_text_has_mark_name_and_instructions():
    text = S.trigger_text("daily_diary", "Write today's diary")
    assert text.startswith(S.SCHEDULE_MARK + " daily_diary]")
    assert "Write today's diary" in text
    assert "save_cron_report" in text
    assert "subagent_send_task_async" in text


def test_trigger_text_handles_empty_description():
    text = S.trigger_text("t", "")
    assert "(no description)" in text


# ── _parse_iso ───────────────────────────────────────────────────────

def test_parse_iso_roundtrip_and_bad():
    dt = _aware(2026, 8, 25, 9, 0)
    assert S._parse_iso(_iso(dt)) == dt
    assert S._parse_iso(None) is None
    assert S._parse_iso("") is None
    assert S._parse_iso("not-a-date") is None


# ── _latest_fire_at_or_before ────────────────────────────────────────

def test_latest_fire_single_and_multi():
    # daily 9am; anchor 3 days back → newest fire <= now is today 9am
    anchor = _aware(2026, 8, 22, 9, 0)
    now = _aware(2026, 8, 25, 10, 0)
    latest = S._latest_fire_at_or_before("0 9 * * *", anchor, now, None)
    assert latest == _aware(2026, 8, 25, 9, 0)

    # no fire in (anchor, now] → None
    assert S._latest_fire_at_or_before(
        "0 9 * * *", _aware(2026, 8, 25, 9, 0), _aware(2026, 8, 25, 9, 30), None,
    ) is None


# ── _classify ────────────────────────────────────────────────────────

def _task(schedule="0 9 * * *", last_run_due=None, created_at=None, **kw):
    t = {"id": 1, "name": "t", "description": "d", "schedule": schedule,
         "timezone": "", "created_at": created_at or _iso(_aware(2026, 8, 1))}
    if last_run_due is not None:
        t["last_run_due"] = last_run_due
    t.update(kw)
    return t


def test_classify_not_yet_due():
    # ran today 9am; now 10am → next fire tomorrow, nothing due
    task = _task(last_run_due=_iso(_aware(2026, 8, 25, 9, 0)))
    assert S._classify(task, _aware(2026, 8, 25, 10, 0)) is None


def test_classify_freshly_due_fires():
    # last ran yesterday 9am; now today 9:00:30 → due now (within grace)
    task = _task(last_run_due=_iso(_aware(2026, 8, 24, 9, 0)))
    decision = S._classify(task, _aware(2026, 8, 25, 9, 0, 30))
    assert decision == ("fire", _aware(2026, 8, 25, 9, 0))


def test_classify_overdue_marks_missed_latest():
    # ran 3 days ago; now today 10am → newest fire (today 9am) is missed
    task = _task(last_run_due=_iso(_aware(2026, 8, 22, 9, 0)))
    decision = S._classify(task, _aware(2026, 8, 25, 10, 0))
    assert decision == ("missed", _aware(2026, 8, 25, 9, 0))


def test_classify_manual_and_empty_and_bad():
    assert S._classify(_task(schedule="manual"), _aware(2026, 8, 25, 10, 0)) is None
    assert S._classify(_task(schedule=""), _aware(2026, 8, 25, 10, 0)) is None
    assert S._classify(_task(schedule="61 * * * *"), _aware(2026, 8, 25, 10, 0)) is None


def test_classify_anchors_to_created_at_when_no_runs():
    # never run; created long ago; first fire overdue → missed
    task = _task(created_at=_iso(_aware(2026, 8, 20)))
    decision = S._classify(task, _aware(2026, 8, 25, 10, 0))
    assert decision is not None
    action, due = decision
    assert action == "missed"
    assert due == _aware(2026, 8, 25, 9, 0)


def test_classify_grace_boundary():
    # exactly at GRACE → still fire (<=)
    task = _task(last_run_due=_iso(_aware(2026, 8, 24, 9, 0)))
    due_time = _aware(2026, 8, 25, 9, 0)
    at_grace = due_time + timedelta(seconds=S.MISS_GRACE)
    assert S._classify(task, at_grace)[0] == "fire"
    just_past = due_time + timedelta(seconds=S.MISS_GRACE + 1)
    assert S._classify(task, just_past)[0] == "missed"


# ── fire_task_now ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fire_task_now_no_client():
    service = MagicMock()
    service._tool_ctx = None
    result = await S.fire_task_now(service, "x")
    assert "not connected" in result


@pytest.mark.asyncio
async def test_fire_task_now_records_and_posts():
    client = AsyncMock()

    async def fake_call_tool(name, arguments=None):
        if name == "__scheduled_task_by_name":
            return ('{"id": 7, "name": "daily", "description": "d", '
                    '"schedule": "0 9 * * *", "timezone": "", '
                    '"created_at": "2026-08-01T00:00:00", "last_run_due": null}')
        if name == "__scheduled_record_run":
            return "{}"
        return "null"

    client.call_tool = fake_call_tool

    posted = []
    inbox = MagicMock()

    async def fake_post(msg):
        posted.append(msg)

    inbox.post = fake_post

    ctx = MagicMock()
    ctx.memfiles_client = client
    service = MagicMock()
    service._tool_ctx = ctx
    service.inbox = inbox
    service.surface_autonomous = AsyncMock()

    result = await S.fire_task_now(service, "daily")
    assert "dispatched" in result
    assert len(posted) == 1
    assert posted[0].content.startswith(S.SCHEDULE_MARK + " daily]")


@pytest.mark.asyncio
async def test_fire_task_now_unknown_task():
    client = AsyncMock()

    async def fake_call_tool(name, arguments=None):
        return "null"

    client.call_tool = fake_call_tool
    ctx = MagicMock()
    ctx.memfiles_client = client
    service = MagicMock()
    service._tool_ctx = ctx
    result = await S.fire_task_now(service, "ghost")
    assert "not found" in result


# ── turn outcome writeback (on_reply hook) ───────────────────────────

@pytest.mark.asyncio
async def test_schedule_reply_marks_run_failed_on_cancel_and_error():
    client = AsyncMock()
    client.call_tool = AsyncMock(return_value="{}")
    surfaced: list[str] = []
    service = MagicMock()

    async def fake_surface(t: str) -> None:
        surfaced.append(t)

    service.surface_autonomous = fake_surface
    task = {"id": 7, "name": "daily"}
    reply = S._schedule_reply(service, client, task, "2026-08-25T09:00:00")

    await reply("(Turn interrupted)", cancelled=True)   # Esc → interrupted
    await reply("Error: boom")                          # turn 400 → reason
    await reply("all good")                             # normal → no writeback

    calls = [(c.args[0], c.args[1]) for c in client.call_tool.await_args_list]
    assert [name for name, _ in calls] == [
        "__scheduled_mark_run_failed", "__scheduled_mark_run_failed",
    ]
    assert calls[0][1]["error"] == "interrupted"
    assert calls[1][1]["error"] == "Error: boom"
    assert surfaced == ["(Turn interrupted)", "Error: boom", "all good"]


# ── startup sweep on a previous process lifetime ─────────────────────

@pytest.mark.asyncio
async def test_schedule_loop_sweeps_stale_pending_into_notice(monkeypatch):
    monkeypatch.setattr(S, "POLL_INTERVAL", 0.01)
    results = [
        # __scheduled_fail_unconfirmed
        '{"failed": 1, "runs": [{"task_id": 7, "name": "daily", '
        '"due_at": "2026-08-25T09:00:00", "status": "failed"}]}',
        "[]",  # __scheduled_tasks_state
    ]

    async def fake_call_tool(name, arguments=None):
        return results.pop(0)

    client = AsyncMock()
    client.call_tool = fake_call_tool

    posted: list = []
    inbox = MagicMock()

    async def fake_post(msg):
        posted.append(msg)

    inbox.post = fake_post

    service = MagicMock()
    ctx = MagicMock()
    ctx.memfiles_client = client
    service._tool_ctx = ctx
    service.inbox = inbox
    service.surface_autonomous = AsyncMock()

    task = asyncio.create_task(S.schedule_loop(service))
    try:
        for _ in range(300):
            if posted:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()

    assert posted, "schedule loop never posted a notice"
    assert "daily @ 2026-08-25T09:00:00" in posted[0].content
    assert "run_schedule_now" in posted[0].content
    assert "scheduled_run_skip" in posted[0].content


# ── run_schedule_now native tool ─────────────────────────────────────

@pytest.mark.asyncio
async def test_run_schedule_now_tool_requires_name():
    from slife.tools.schedules import RunScheduleNowTool

    tool = RunScheduleNowTool()
    result = await tool.execute(name="")
    assert result  # require_params error


@pytest.mark.asyncio
async def test_run_schedule_now_tool_no_hook():
    from slife.tools.schedules import RunScheduleNowTool

    tool = RunScheduleNowTool()
    object.__setattr__(tool, "_ctx", None)
    result = await tool.execute(name="daily")
    assert "not available" in result


@pytest.mark.asyncio
async def test_run_schedule_now_tool_calls_hook():
    from slife.tools.schedules import RunScheduleNowTool

    tool = RunScheduleNowTool()
    ctx = MagicMock()
    ctx.fire_schedule_now = AsyncMock(return_value="dispatched")
    object.__setattr__(tool, "_ctx", ctx)
    result = await tool.execute(name="daily")
    assert result == "dispatched"
    ctx.fire_schedule_now.assert_awaited_once_with("daily")
