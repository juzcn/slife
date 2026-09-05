"""Schedule-loop timing logic: trigger text, fire/miss classification,
manual fire.  Pure-timing tests use no DB; the loop's DB interaction is
exercised via a mocked memfiles client.
"""

import asyncio
import json
import re
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

def test_trigger_text_has_mark_name_and_dispatch_hint():
    text = S.trigger_text("daily_diary", "Write today's diary")
    assert text.startswith(S.SCHEDULE_MARK + " daily_diary]")
    assert "Write today's diary" in text
    assert 'run_schedule_now(name="daily_diary")' in text
    # Dispatch is delegated to the tool — no subagent instructions leak.
    assert "subagent_send_task_async" not in text
    assert "spawn_subagent" not in text


def test_trigger_text_handles_empty_description():
    text = S.trigger_text("t", "")
    assert "(no description)" in text


# ── build_worker_task ───────────────────────────────────────────────

def test_build_worker_task_self_contained():
    task = S.build_worker_task("daily_report", "Write today's report")
    assert "daily_report" in task
    assert "Write today's report" in task
    assert "report_save" in task
    # the title example carries the MM-DD date context for relative tasks
    assert 'e.g. "daily_report: ' in task
    assert re.search(r"\d{2}-\d{2} summary", task)


def test_build_worker_task_handles_empty_description():
    task = S.build_worker_task("t", "")
    assert "(no description)" in task


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
async def test_fire_task_now_dispatches_directly(monkeypatch):
    """Records a pending run and dispatches the task to a worker named after
    the task — no inbox trigger, no next turn."""
    S._SCHEDULE_WORKERS.clear()
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
    ctx = MagicMock()
    ctx.memfiles_client = client
    service = MagicMock()
    service._tool_ctx = ctx
    service.inbox = MagicMock()
    service.inbox.post = AsyncMock()

    manager = AsyncMock()
    manager.spawn = AsyncMock(return_value="daily")
    manager.send_task_async = AsyncMock(return_value="rpc-1")
    monkeypatch.setattr("slife.subagent.process.get_manager", lambda: manager)

    result = await S.fire_task_now(service, "daily")
    assert "dispatched now to worker 'daily'" in result
    manager.spawn.assert_awaited_once_with(name="daily")
    manager.send_task_async.assert_awaited_once()
    agent_name, task = manager.send_task_async.call_args[0]
    assert agent_name == "daily"
    assert "report_save" in task
    assert manager.send_task_async.call_args.kwargs["mode"] == "auto"
    assert service.inbox.post.await_count == 0  # no inbox relay
    assert "daily" in S._SCHEDULE_WORKERS  # tracked for reword + recycle


@pytest.mark.asyncio
async def test_fire_task_now_marks_run_failed_on_dispatch_error(monkeypatch):
    calls: list[tuple[str, dict]] = []
    client = AsyncMock()

    async def fake_call_tool(name, arguments=None):
        calls.append((name, arguments))
        if name == "__scheduled_task_by_name":
            return ('{"id": 7, "name": "daily", "description": "d", '
                    '"schedule": "manual", "timezone": "", '
                    '"created_at": "2026-08-01T00:00:00", "last_run_due": null}')
        return "{}"

    client.call_tool = fake_call_tool
    ctx = MagicMock()
    ctx.memfiles_client = client
    service = MagicMock()
    service._tool_ctx = ctx

    manager = AsyncMock()
    manager.spawn = AsyncMock(side_effect=RuntimeError("max subagents reached"))
    monkeypatch.setattr("slife.subagent.process.get_manager", lambda: manager)

    result = await S.fire_task_now(service, "daily")
    assert "Error: dispatch failed" in result
    assert any(name == "__scheduled_mark_run_failed" for name, _ in calls)


@pytest.mark.asyncio
async def test_fire_task_now_backfill_transitions_given_due_at(monkeypatch):
    """A backfill passes the failed/missed run's due_at: that exact run is
    recorded pending (the ON-CONFLICT update, not a fresh now-run) and the
    worker task tells report_save to confirm it."""
    S._SCHEDULE_WORKERS.clear()
    records: list[dict] = []
    client = AsyncMock()

    async def fake_call_tool(name, arguments=None):
        if name == "__scheduled_task_by_name":
            return ('{"id": 7, "name": "daily", "description": "d", '
                    '"schedule": "0 9 * * *", "timezone": "", '
                    '"created_at": "2026-08-01T00:00:00", "last_run_due": null}')
        if name == "__scheduled_record_run":
            records.append(arguments or {})
            return "{}"
        return "null"

    client.call_tool = fake_call_tool
    ctx = MagicMock()
    ctx.memfiles_client = client
    service = MagicMock()
    service._tool_ctx = ctx

    manager = AsyncMock()
    manager.spawn = AsyncMock(return_value="daily")
    manager.send_task_async = AsyncMock(return_value="rpc-1")
    monkeypatch.setattr("slife.subagent.process.get_manager", lambda: manager)

    due = "2026-08-27T10:55:00+08:00"
    result = await S.fire_task_now(service, "daily", due_at=due)
    assert "dispatched now to worker 'daily'" in result
    assert records == [{"task_id": 7, "due_at": due}]  # the run, not a new now
    _, task = manager.send_task_async.call_args[0]
    assert f'due_at="{due}"' in task  # worker confirms the exact run


@pytest.mark.asyncio
async def test_fire_task_now_dispatch_error_fails_given_due_at(monkeypatch):
    """A failed dispatch of a backfill marks the targeted run failed (its
    original due_at), not some fresh now-run."""
    mark_calls: list[dict] = []
    client = AsyncMock()

    async def fake_call_tool(name, arguments=None):
        if name == "__scheduled_task_by_name":
            return ('{"id": 7, "name": "daily", "description": "d", '
                    '"schedule": "manual", "timezone": "", '
                    '"created_at": "2026-08-01T00:00:00", "last_run_due": null}')
        if name == "__scheduled_mark_run_failed":
            mark_calls.append(arguments or {})
        return "{}"

    client.call_tool = fake_call_tool
    ctx = MagicMock()
    ctx.memfiles_client = client
    service = MagicMock()
    service._tool_ctx = ctx

    manager = AsyncMock()
    manager.spawn = AsyncMock(side_effect=RuntimeError("max subagents reached"))
    monkeypatch.setattr("slife.subagent.process.get_manager", lambda: manager)

    due = "2026-08-27T10:55:00+08:00"
    result = await S.fire_task_now(service, "daily", due_at=due)
    assert "Error: dispatch failed" in result
    assert mark_calls == [{"task_id": 7, "due_at": due,
                           "error": "max subagents reached"}]


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


# ── pending-fire guard ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fire_marks_pending_guard_then_clears_on_dispatch(monkeypatch):
    S._pending_fires.clear()
    service = MagicMock()
    service.inbox = MagicMock()
    service.inbox.post = AsyncMock()
    await S._fire(service, {"name": "daily", "description": "d"})
    assert "daily" in S._pending_fires

    client = AsyncMock()

    async def fake_call_tool(name, arguments=None):
        if name == "__scheduled_task_by_name":
            return ('{"id": 7, "name": "daily", "description": "d", '
                    '"schedule": "0 9 * * *", "timezone": "", '
                    '"created_at": "2026-08-01T00:00:00", "last_run_due": null}')
        return "{}"

    client.call_tool = fake_call_tool
    ctx = MagicMock()
    ctx.memfiles_client = client
    service._tool_ctx = ctx

    manager = AsyncMock()
    manager.spawn = AsyncMock(return_value="daily")
    manager.send_task_async = AsyncMock(return_value="rpc-1")
    monkeypatch.setattr("slife.subagent.process.get_manager", lambda: manager)

    await S.fire_task_now(service, "daily")
    assert "daily" not in S._pending_fires  # cleared after dispatch


# ── worker recycling ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recycle_idle_workers(monkeypatch):
    S._SCHEDULE_WORKERS.clear()
    S._SCHEDULE_WORKERS.add("daily")

    idle_proc = MagicMock()
    idle_proc.is_running = True
    idle_proc.is_busy = False
    idle_proc.pending_async_count = 0
    busy_proc = MagicMock()
    busy_proc.is_running = True
    busy_proc.is_busy = True

    manager = AsyncMock()
    manager.get = MagicMock(side_effect=lambda name: {
        "daily": idle_proc, "other": busy_proc,
    }.get(name))
    monkeypatch.setattr("slife.subagent.process.get_manager", lambda: manager)

    states = [
        {"name": "daily", "has_pending_run": False},   # settled + idle → recycle
        {"name": "other", "has_pending_run": True},    # pending → keep
    ]
    await S._recycle_idle_workers(states)
    manager.stop.assert_awaited_once_with("daily")


@pytest.mark.asyncio
async def test_recycle_skips_non_schedule_workers(monkeypatch):
    S._SCHEDULE_WORKERS.clear()
    S._SCHEDULE_WORKERS.add("daily")
    proc = MagicMock()
    proc.is_running = True
    proc.is_busy = False
    proc.pending_async_count = 0
    manager = AsyncMock()
    manager.get = MagicMock(return_value=proc)
    monkeypatch.setattr("slife.subagent.process.get_manager", lambda: manager)

    # "other" is not a schedule-dispatched worker → never recycled.
    await S._recycle_idle_workers([{"name": "other", "has_pending_run": False}])
    manager.stop.assert_not_awaited()


# ── startup one-shot sweep: pending → failed, no message ─────────────

def _make_service(client, posted):
    inbox = MagicMock()

    async def fake_post(msg):
        posted.append(msg)

    inbox.post = fake_post
    ctx = MagicMock()
    ctx.memfiles_client = client
    service = MagicMock()
    service._tool_ctx = ctx
    service.inbox = inbox
    service.surface_schedule = AsyncMock()
    # Startup gate: the one-shot awaits service.wait_startup_settled().
    service.wait_startup_settled = AsyncMock()
    return service


@pytest.mark.asyncio
async def test_schedule_startup_sweep_reaps_failed_and_posts_nothing():
    client = AsyncMock()
    calls: list[str] = []

    async def fake_call_tool(name, arguments=None):
        calls.append(name)
        if name == "__scheduled_fail_unconfirmed":
            return ('{"failed": 2, "runs": [{"task_id": 7, "name": "daily", '
                    '"due_at": "2026-08-25T09:00:00", "status": "failed"}, '
                    '{"task_id": 8, "name": "weekly", '
                    '"due_at": "2026-08-24T18:00:00", "status": "failed"}]}')
        if name == "__scheduled_tasks_state":
            return "[]"
        return "{}"

    client.call_tool = fake_call_tool

    posted: list = []
    service = _make_service(client, posted)

    await S.schedule_startup_sweep(service)

    # The sweep settles state silently: no task is due-missed here (so no
    # missed-marking fires), nothing is ever posted to the inbox, and the
    # footer reminder is fed with the swept runs.
    assert posted == []
    assert "__scheduled_fail_unconfirmed" in calls
    assert "__scheduled_tasks_state" in calls
    assert "__scheduled_mark_missed" not in calls
    service.set_schedule_pending.assert_called_with([])


@pytest.mark.asyncio
async def test_schedule_startup_sweep_marks_missed_without_posting(monkeypatch):
    # Fix the clock so _classify deterministically sees a fire older than the
    # grace window (missed while slife was down).  Local-aware times in the
    # task mirror what the memfiles store emits, matching the other classify
    # tests.
    fixed_now = datetime(2026, 8, 25, 14, 0).astimezone()
    last_due = datetime(2026, 8, 24, 9, 0).astimezone()
    created = datetime(2026, 8, 20, 9, 0).astimezone()

    class _FixedClock:
        @staticmethod
        def now():
            return fixed_now

        fromisoformat = staticmethod(datetime.fromisoformat)

    monkeypatch.setattr(S, "datetime", _FixedClock)

    client = AsyncMock()
    mark_calls: list[dict] = []

    async def fake_call_tool(name, arguments=None):
        if name == "__scheduled_fail_unconfirmed":
            return '{"failed": 0, "runs": []}'
        if name == "__scheduled_tasks_state":
            return ('[{"id": 1, "name": "daily", "schedule": "0 9 * * *", '
                    '"timezone": "", '
                    f'"created_at": "{_iso(created)}", '
                    f'"last_run_due": "{_iso(last_due)}"}}]')
        if name == "__scheduled_mark_missed":
            mark_calls.append(arguments)
            return '{"status": "missed"}'
        return "{}"

    client.call_tool = fake_call_tool

    posted: list = []
    service = _make_service(client, posted)

    await S.schedule_startup_sweep(service)

    assert mark_calls == [
        {"task_id": 1, "due_at": _iso(datetime(2026, 8, 25, 9, 0).astimezone())},
    ]
    assert posted == []  # missed runs are recorded, never announced
    # footer reminder fed (no open runs in the fake → empty list)
    service.set_schedule_pending.assert_called_with([])


@pytest.mark.asyncio
async def test_pending_schedule_runs_merges_dedupes_and_sorts():
    """Failed and missed are one "backfill or skip?" list for the footer:
    merged, deduplicated, newest first, each run exactly once."""
    client = AsyncMock()

    async def fake_call_tool(name, arguments=None):
        if name == "__scheduled_tasks_list":
            return ('{"total": 2, "tasks": [{"id": 1, "name": "daily"}, '
                    '{"id": 2, "name": "weekly"}]}')
        if name == "__scheduled_runs_list":
            if arguments["status"] == "failed":
                # The same due_at appears twice → must render once.
                return ('{"total": 2, "runs": ['
                        '{"task_id": 1, "due_at": "2026-08-25T09:00:00", "status": "failed"}, '
                        '{"task_id": 1, "due_at": "2026-08-25T09:00:00", "status": "failed"}]}')
            return ('{"total": 1, "runs": ['
                    '{"task_id": 2, "due_at": "2026-08-24T18:00:00", "status": "missed"}]}')
        return "{}"

    client.call_tool = fake_call_tool

    runs = await S._pending_schedule_runs(client)

    assert runs == [
        {"name": "daily", "due_at": "2026-08-25T09:00:00", "status": "failed"},
        {"name": "weekly", "due_at": "2026-08-24T18:00:00", "status": "missed"},
    ]


@pytest.mark.asyncio
async def test_schedule_startup_sweep_missing_client_is_noop():
    service = MagicMock()
    service._tool_ctx = None
    service.wait_startup_settled = AsyncMock()
    await S.schedule_startup_sweep(service)  # must not raise or call out


@pytest.mark.asyncio
async def test_schedule_loop_never_announces_missed_or_stale(monkeypatch):
    # The timed loop fires only — even with unconfirmed runs around, it must
    # never call the sweep nor post a missed notice.  Regression: the notice
    # used to be posted on every poll while a failed run stayed unresolved.
    monkeypatch.setattr(S, "POLL_INTERVAL", 0.02)
    calls: list[str] = []

    async def fake_call_tool(name, arguments=None):
        calls.append(name)
        return "[]"

    client = AsyncMock()
    client.call_tool = fake_call_tool

    posted: list = []
    service = _make_service(client, posted)

    task = asyncio.create_task(S.schedule_loop(service))
    try:
        for _ in range(200):
            if calls.count("__scheduled_tasks_state") >= 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()

    assert "__scheduled_fail_unconfirmed" not in calls
    assert not posted


# ── run_schedule_now native tool ─────────────────────────────────────

@pytest.mark.asyncio
async def test_run_schedule_now_tool_requires_name():
    from slife.tools.schedule import RunScheduleNowTool

    tool = RunScheduleNowTool()
    result = await tool.execute(name="")
    assert result  # require_params error


@pytest.mark.asyncio
async def test_run_schedule_now_tool_no_hook():
    from slife.tools.schedule import RunScheduleNowTool

    tool = RunScheduleNowTool()
    object.__setattr__(tool, "_ctx", None)
    result = await tool.execute(name="daily")
    assert "not available" in result


@pytest.mark.asyncio
async def test_run_schedule_now_tool_calls_hook():
    from slife.tools.schedule import RunScheduleNowTool

    tool = RunScheduleNowTool()
    ctx = MagicMock()
    ctx.fire_schedule_now = AsyncMock(return_value="dispatched")
    object.__setattr__(tool, "_ctx", ctx)
    result = await tool.execute(name="daily")
    assert result == "dispatched"
    ctx.fire_schedule_now.assert_awaited_once_with("daily", "")


@pytest.mark.asyncio
async def test_run_schedule_now_tool_passes_backfill_due_at():
    from slife.tools.schedule import RunScheduleNowTool

    tool = RunScheduleNowTool()
    ctx = MagicMock()
    ctx.fire_schedule_now = AsyncMock(return_value="dispatched")
    object.__setattr__(tool, "_ctx", ctx)
    due = "2026-08-27T10:55:00+08:00"
    result = await tool.execute(name="daily", due_at=due)
    assert result == "dispatched"
    ctx.fire_schedule_now.assert_awaited_once_with("daily", due)


# ── completion reconciliation: run record, not worker narration ─────

def _client_with_runs(latest_runs, pending_runs, mark_calls):
    """Memfiles stub: newest run (status "") and the pending-run list."""
    client = AsyncMock()

    async def fake_call_tool(name, arguments=None):
        arguments = arguments or {}
        if name == "__scheduled_task_by_name":
            return '{"id": 7, "name": "daily"}'
        if name == "__scheduled_runs_list":
            runs = pending_runs if arguments.get("status") == "pending" else latest_runs
            return json.dumps({"runs": runs})
        if name == "__scheduled_mark_run_failed":
            mark_calls.append(arguments)
            return "{}"
        return "{}"

    client.call_tool = fake_call_tool
    return client


@pytest.mark.asyncio
async def test_completion_content_only_claims_saved_when_run_ran():
    due = "2026-08-25T09:00:00"
    mark_calls: list[dict] = []
    latest = [{"status": "ran", "due_at": due}]
    client = _client_with_runs(latest, [], mark_calls)
    service = _make_service(client, [])

    msg = await S._schedule_completion_content(service, "daily")

    assert "completed — report saved" in msg
    assert mark_calls == []  # confirmed run → nothing to settle


@pytest.mark.asyncio
async def test_completion_content_settles_unconfirmed_run_and_says_so():
    """A worker that ended with its run still pending (report_save never
    landed) must be reported as failed, not as "report saved" — and the run
    settles to failed so it is backfillable instead of silent."""
    due = "2026-08-25T09:00:00"
    mark_calls: list[dict] = []
    latest = [{"status": "pending", "due_at": due}]
    pending = [{"status": "pending", "due_at": due}]
    client = _client_with_runs(latest, pending, mark_calls)
    service = _make_service(client, [])

    msg = await S._schedule_completion_content(service, "daily")

    assert "report saved" not in msg
    assert "report was not saved" in msg
    assert "failed" in msg
    assert mark_calls == [{"task_id": 7, "due_at": due,
                           "error": "worker finished without confirming the run"}]


@pytest.mark.asyncio
async def test_completion_content_never_claims_saved_without_client():
    service = MagicMock()
    service._tool_ctx = None

    msg = await S._schedule_completion_content(service, "daily")

    assert "daily" in msg
    assert "report saved" not in msg
