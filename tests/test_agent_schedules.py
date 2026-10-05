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
import slife.timeouts as _timeouts


def _pacing():
    """The live cadence registry — a test patches HERE, never a module constant."""
    return _timeouts.timeouts.pacing


def _aware(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s).astimezone()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


# ── trigger text ─────────────────────────────────────────────────────

def test_trigger_text_has_mark_name_and_dispatch_hint():
    text = S.trigger_text("daily_diary", "Write today's diary")
    assert text.startswith(S.SCHEDULE_MARK + " daily_diary]")
    assert "Write today's diary" in text
    # The dispatch hint shows the tool shape for a fresh run: name known,
    # due_at omitted (default "").
    assert 'run_schedule_now(name="daily_diary")' in text
    # Dispatch is delegated to the tool — no subagent instructions leak.
    assert "subagent_send_task_async" not in text
    assert "spawn_subagent" not in text


def test_trigger_text_handles_empty_description():
    """A description-less task (a legacy row, or one written straight through
    the plugin's internal tool) is surfaced to the dispatcher as misconfigured
    rather than behind a placeholder that reads like ordinary content."""
    text = S.trigger_text("t", "")
    assert "no description" in text
    assert "scheduled_task_set" in text


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
    """The worker must be told to STOP, not handed a blank task: with the full
    toolset and the parent's mesh identity, an un-instructed worker invents
    work and emits real side effects."""
    task = S.build_worker_task("t", "")
    low = task.lower()
    assert "no description" in low
    assert "no instruction" in low
    assert "do not invent work" in low
    # report_save is the one tool it may still use — the run must be closed.
    assert "report_save" in task
    # It is not given the "carry out the task fully" mandate.
    assert "carry out the task fully" not in low


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


def test_latest_fire_long_downtime_not_capped():
    """The old bounded forward-stepping loop (5000 steps) returned None for a
    per-minute schedule with >~3.5 days of downtime, silently unclassifying
    the fire.  ``croniter.get_prev`` has no step bound — the newest fire in
    ``(anchor, now]`` must always come back."""
    anchor = _aware(2026, 8, 20, 9, 0)
    now = _aware(2026, 9, 10, 9, 30)  # 21 days later — 20k+ missed minutes
    latest = S._latest_fire_at_or_before("* * * * *", anchor, now, None)
    assert latest is not None
    assert anchor < latest <= now


def test_latest_fire_now_at_fire_time_returns_now():
    """A fire exactly at *now* is the newest fire in ``(anchor, now]`` —
    get_prev is strict-before, so the boundary must be closed explicitly."""
    anchor = _aware(2026, 8, 24, 9, 0)
    now = _aware(2026, 8, 25, 9, 0)  # exactly the 9am fire
    assert S._latest_fire_at_or_before("0 9 * * *", anchor, now, None) == now
    # ...but only when *now* is strictly after the anchor (fire == anchor is excluded)
    assert S._latest_fire_at_or_before(
        "0 9 * * *", now, _aware(2026, 8, 25, 9, 0), None,
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
    at_grace = due_time + timedelta(seconds=_pacing().miss_grace)
    assert S._classify(task, at_grace)[0] == "fire"
    just_past = due_time + timedelta(seconds=_pacing().miss_grace + 1)
    assert S._classify(task, just_past)[0] == "missed"


# ── fire_task_now ────────────────────────────────────────────────────

#: The scheduled task every dispatch test fires.
_TASK_JSON = ('{"id": 7, "name": "daily", "description": "d", '
              '"schedule": "0 9 * * *", "timezone": "", '
              '"created_at": "2026-08-01T00:00:00", "last_run_due": null}')


def _scheduled_client(records: list | None = None):
    """A memfiles client answering the dispatch's own calls."""
    client = AsyncMock()

    async def fake_call_tool(name, arguments=None):
        if name == "__scheduled_task_by_name":
            return _TASK_JSON
        if name == "__scheduled_record_run":
            if records is not None:
                records.append(arguments or {})
            return "{}"
        return "null"

    client.call_tool = fake_call_tool
    return client


def _pool(worker: str = "worker-2", task_id: str = "rpc-1", reused: bool = True):
    """A manager whose pool hands out *worker* — the caller never names one."""
    manager = MagicMock()
    manager.send_task_to_pool = AsyncMock(return_value=(worker, task_id, reused))
    return manager


@pytest.mark.asyncio
async def test_fire_task_now_no_client():
    service = MagicMock()
    service._tool_ctx = None
    result = await S.fire_task_now(service, "x")
    assert "not connected" in result


@pytest.mark.asyncio
async def test_fire_task_now_dispatches_to_the_pool(monkeypatch):
    """Records a pending run and hands the task to the pool — no inbox
    trigger, no next turn, and no worker named by us."""
    S._SCHEDULE_TASKS.clear()
    records: list[dict] = []
    ctx = MagicMock()
    ctx.memfiles_client = _scheduled_client(records)
    service = MagicMock()
    service._tool_ctx = ctx
    service.inbox = MagicMock()
    service.inbox.post = AsyncMock()

    manager = _pool()
    monkeypatch.setattr("slife.subagent.process.get_manager", lambda: manager)

    result = await S.fire_task_now(service, "daily")
    assert "dispatched now to worker 'worker-2'" in result
    assert "rpc-1" in result
    manager.send_task_to_pool.assert_awaited_once()
    task = manager.send_task_to_pool.call_args.args[0]
    assert "report_save" in task
    assert manager.send_task_to_pool.call_args.kwargs["mode"] == "auto"
    assert service.inbox.post.await_count == 0  # no inbox relay
    # The dispatch is tracked by task id, against the run it recorded.
    assert records == [{"task_id": 7, "due_at": records[0]["due_at"]}]
    assert S._SCHEDULE_TASKS == {"rpc-1": ("daily", records[0]["due_at"])}


@pytest.mark.asyncio
async def test_the_worker_is_the_pool_s_and_the_task_keeps_its_own_name(monkeypatch):
    """The task name goes where the task's identity belongs and nowhere else.

    The pool answers with a minted worker name that has nothing to do with the
    task: the worker's instructions, the report it saves and the run it
    confirms must all still be the TASK's, or the report binds no scheduled
    task and the run never turns ``ran``.
    """
    S._SCHEDULE_TASKS.clear()
    ctx = MagicMock()
    ctx.memfiles_client = _scheduled_client()
    service = MagicMock()
    service._tool_ctx = ctx

    manager = _pool(worker="worker-9", task_id="rpc-9")
    monkeypatch.setattr("slife.subagent.process.get_manager", lambda: manager)

    await S.fire_task_now(service, "daily")
    task = manager.send_task_to_pool.call_args.args[0]
    assert 'report_save(name="daily"' in task  # never "worker-9"
    assert "worker-9" not in task
    assert S._SCHEDULE_TASKS["rpc-9"][0] == "daily"


@pytest.mark.asyncio
async def test_fire_task_now_sends_the_context_with_the_task(monkeypatch):
    """Every dispatched task carries `service._tool_ctx`'s settled turns.

    The main agent's context at dispatch time, minus its system message and
    minus the turn being run (it carries no turn rowid yet).  A scheduled task
    is a send like any other, so it gets the same rule."""
    S._SCHEDULE_TASKS.clear()
    ctx = MagicMock()
    ctx.memfiles_client = _scheduled_client()
    ctx.message_history = MagicMock(
        messages=[{"role": "system", "content": "sys"},
                 {"role": "user", "content": "u1", "_turn_id": 4},
                 {"role": "assistant", "content": "a1"},
                 # The turn being run: no rowid, so it is not context.
                 {"role": "user", "content": "u2"},
                 {"role": "assistant", "content": "a2"}],
    )
    service = MagicMock()
    service._tool_ctx = ctx

    manager = _pool()
    monkeypatch.setattr("slife.subagent.process.get_manager", lambda: manager)

    await S.fire_task_now(service, "daily")
    seed = manager.send_task_to_pool.call_args.kwargs["seed"]
    # Verbatim, ``_turn_id`` included: the worker's rebuild keys on it to
    # re-fetch a turn from the shared store.
    assert seed == [{"role": "user", "content": "u1", "_turn_id": 4},
                    {"role": "assistant", "content": "a1"}]


@pytest.mark.asyncio
async def test_fire_task_now_without_a_reachable_context(monkeypatch):
    """No reachable history seeds nothing — the task still dispatches (a
    background task must not be refused because the context was not in hand)."""
    S._SCHEDULE_TASKS.clear()
    ctx = MagicMock()
    ctx.memfiles_client = _scheduled_client()
    ctx.message_history = None  # _serialize_cloned_context → None
    service = MagicMock()
    service._tool_ctx = ctx

    manager = _pool()
    monkeypatch.setattr("slife.subagent.process.get_manager", lambda: manager)

    await S.fire_task_now(service, "daily")
    assert manager.send_task_to_pool.call_args.kwargs["seed"] is None


@pytest.mark.asyncio
async def test_fire_task_now_marks_run_failed_on_dispatch_error(monkeypatch):
    """A full pool is an answer, not a fault — and the run it could not
    dispatch is settled failed so the task shows up for backfill."""
    S._SCHEDULE_TASKS.clear()
    calls: list[tuple[str, dict]] = []
    client = AsyncMock()

    async def fake_call_tool(name, arguments=None):
        calls.append((name, arguments or {}))
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

    from slife.subagent.process import PoolFullError

    manager = MagicMock()
    manager.send_task_to_pool = AsyncMock(
        side_effect=PoolFullError("no subagent is idle and the pool is at its "
                                  "limit (2) — worker-1 (1 in flight)"),
    )
    monkeypatch.setattr("slife.subagent.process.get_manager", lambda: manager)

    result = await S.fire_task_now(service, "daily")
    assert "Error: dispatch failed" in result
    assert "pool is at its limit" in result
    failed = [a for n, a in calls if n == "__scheduled_mark_run_failed"]
    assert failed and failed[0]["task_id"] == 7
    assert S._SCHEDULE_TASKS == {}  # nothing dispatched, nothing tracked


@pytest.mark.asyncio
async def test_fire_task_now_marks_run_failed_when_no_manager(monkeypatch):
    """The run is recorded before the manager is even reached: an unavailable
    manager must settle it rather than leave it pending for the next process's
    startup sweep."""
    calls: list[tuple[str, dict]] = []
    client = AsyncMock()

    async def fake_call_tool(name, arguments=None):
        calls.append((name, arguments or {}))
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
    monkeypatch.setattr("slife.subagent.process.get_manager", lambda: None)

    result = await S.fire_task_now(service, "daily")
    assert "Error: dispatch failed" in result
    assert any(n == "__scheduled_mark_run_failed" for n, _ in calls)


@pytest.mark.asyncio
async def test_fire_task_now_backfill_transitions_given_due_at(monkeypatch):
    """A backfill passes the failed/missed run's due_at: that exact run is
    recorded pending (the ON-CONFLICT update, not a fresh now-run) and the
    worker task tells report_save to confirm it."""
    S._SCHEDULE_TASKS.clear()
    records: list[dict] = []
    ctx = MagicMock()
    ctx.memfiles_client = _scheduled_client(records)
    service = MagicMock()
    service._tool_ctx = ctx

    manager = _pool()
    monkeypatch.setattr("slife.subagent.process.get_manager", lambda: manager)

    due = "2026-08-27T10:55:00+08:00"
    result = await S.fire_task_now(service, "daily", due_at=due)
    assert "dispatched now to worker 'worker-2'" in result
    assert records == [{"task_id": 7, "due_at": due}]  # the run, not a new now
    task = manager.send_task_to_pool.call_args.args[0]
    assert f'due_at="{due}"' in task  # worker confirms the exact run
    assert S._SCHEDULE_TASKS == {"rpc-1": ("daily", due)}  # tracked against it


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

    manager = MagicMock()
    manager.send_task_to_pool = AsyncMock(
        side_effect=RuntimeError("max subagents reached"),
    )
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

    ctx = MagicMock()
    ctx.memfiles_client = _scheduled_client()
    service._tool_ctx = ctx

    manager = _pool()
    monkeypatch.setattr("slife.subagent.process.get_manager", lambda: manager)

    await S.fire_task_now(service, "daily")
    # Keyed by TASK name — the worker's name is not the task's, so a guard
    # popped by worker would leave the task suppressed for a whole grace
    # window.
    assert "daily" not in S._pending_fires  # cleared after dispatch


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
    # turn-prompt reminder is fed with the swept runs.
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
    # turn-prompt reminder fed (no open runs in the fake → empty list)
    service.set_schedule_pending.assert_called_with([])


@pytest.mark.asyncio
async def test_pending_schedule_runs_merges_dedupes_and_sorts():
    """Failed and missed are one "backfill or skip?" list for the turn prompt:
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
    monkeypatch.setattr(_pacing(), "schedule_poll", 0.02)
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

def _client_with_run(status, mark_calls):
    """Memfiles stub: one run's status, keyed by the ``due_at`` asked for.

    *status* is a ``due_at`` → status mapping (or a plain status for every
    run), so a test can give one run an answer and another a different one.
    """
    client = AsyncMock()

    async def fake_call_tool(name, arguments=None):
        arguments = arguments or {}
        if name == "__scheduled_task_by_name":
            return '{"id": 7, "name": "daily"}'
        if name == "__scheduled_run_status":
            due = arguments.get("due_at")
            answer = status.get(due) if isinstance(status, dict) else status
            return json.dumps({"task_id": 7, "due_at": due, "status": answer})
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
    service = _make_service(_client_with_run("ran", mark_calls), [])

    msg = await S._schedule_completion_content(service, "daily", due)

    assert "completed — report saved" in msg
    assert mark_calls == []  # confirmed run → nothing to settle


@pytest.mark.asyncio
async def test_completion_content_settles_unconfirmed_run_and_says_so():
    """A worker that ended with its run still pending (report_save never
    landed) must be reported as failed, not as "report saved" — and the run
    settles to failed so it is backfillable instead of silent."""
    due = "2026-08-25T09:00:00"
    mark_calls: list[dict] = []
    service = _make_service(_client_with_run("pending", mark_calls), [])

    msg = await S._schedule_completion_content(service, "daily", due)

    assert "report saved" not in msg
    assert "report was not saved" in msg
    assert "failed" in msg
    assert mark_calls == [{"task_id": 7, "due_at": due,
                           "error": "worker finished without confirming the run"}]


@pytest.mark.asyncio
async def test_completion_content_judges_its_own_run_not_the_newest_one():
    """Overlapping runs of one task: the worker settled its OWN run.

    Any idle worker can take a fire now, so run A can finish while a newer run
    B is still in flight.  Judging A by "the newest run" would announce A as a
    failure AND flip B — a live run — to failed.
    """
    due_a, due_b = "2026-08-25T09:00:00", "2026-08-25T10:00:00"
    mark_calls: list[dict] = []
    service = _make_service(
        _client_with_run({due_a: "ran", due_b: "pending"}, mark_calls), [],
    )

    msg = await S._schedule_completion_content(service, "daily", due_a)

    assert "completed — report saved" in msg
    assert mark_calls == []  # run B is not this completion's to settle


@pytest.mark.asyncio
async def test_completion_content_reports_an_unknown_run_honestly():
    """No row for the dispatch's due_at — say nothing was confirmed rather
    than claiming a failure that was never recorded."""
    due = "2026-08-25T09:00:00"
    mark_calls: list[dict] = []
    service = _make_service(_client_with_run(None, mark_calls), [])

    msg = await S._schedule_completion_content(service, "daily", due)

    assert "report saved" not in msg
    assert "no run record matches" in msg


@pytest.mark.asyncio
async def test_completion_content_never_claims_saved_without_client():
    service = MagicMock()
    service._tool_ctx = None

    msg = await S._schedule_completion_content(service, "daily", "2026-08-25T09:00:00")

    assert "daily" in msg
    assert "report saved" not in msg
