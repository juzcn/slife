"""Scheduled-task trigger loop — the main-process scheduler.

The loop is deliberately thin: it *times* tasks and *injects* a trigger
message into the unified inbox.  Execution happens elsewhere — the agent
calls ``run_schedule_now`` on the trigger turn, which records a pending run
and dispatches the task to a subagent worker (worker name = task name); the
worker saves its report via ``save_cron_report``.  The loop itself never
runs a task.

State lives in the memfiles DB (``scheduled_tasks`` / ``scheduled_runs``),
not in memory.  Each poll recomputes from the DB, so the loop is stateless
and restart-safe:

  * anchor = newest ``due_at`` across all of a task's runs (any run advances
    the anchor — a fire is never re-detected forever), falling back to
    ``created_at`` before the first run.
  * candidate = ``next_run(anchor)``.  If it is in the future, wait.
  * If it is due-or-overdue, compare the newest fire ``<= now`` against a
    short grace window: within the window the fire just became due (fire it);
    past it, slife was down when the fire was due — the loop leaves it, and
    the one-shot startup sweep (:func:`schedule_startup_sweep`) records the
    fire as ``missed``.

Run outcome is **optimistically pending**: a fire is recorded as ``pending``
and only becomes ``ran`` when the worker's report arrives.  Everything else —
turn error, Esc-interrupt, restart — leaves it a ``failed``-by-default (the
turn's reply hook may write a best-effort error detail; a one-shot startup
sweep (:func:`schedule_startup_sweep`) reaps any ``pending`` a previous
process lifetime could never complete).  This inversion means an incomplete
run is *never* recorded as a success.  :func:`fire_task_now` runs or
backfills a task on demand; ``scheduled_run_skip`` closes a failed run the
user declines.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from datetime import datetime

from slife.agent.system_prompt import render_template

logger = logging.getLogger(__name__)

#: TUI filter mark — a trigger turn's user message starts with this prefix.
SCHEDULE_MARK = "[Schedule"


def is_schedule_trigger(text: str) -> bool:
    """True when *text* is a schedule trigger (a cron fire or a manual
    ``run_schedule_now`` backfill) — a scheduler-driven turn, not a real
    user query and not autonomous."""
    return text.startswith(SCHEDULE_MARK)


def is_autonomous_trigger(text: str) -> bool:
    """True when *text* is a synthetic non-user trigger: a heartbeat
    (autonomous) or a schedule trigger (scheduler-driven).  Used to filter
    these turns from the TUI and to skip turn-footnote annotation.

    Covers both trigger families for the *suppression* they share (hide the
    synthetic user message, skip the turn footnote); distinguishing them for
    *rendering* (⚡ 自主 vs 📅 定时) uses :func:`is_schedule_trigger`."""
    from slife.agent.heartbeat import HEARTBEAT_MARK

    return text.startswith(HEARTBEAT_MARK) or text.startswith(SCHEDULE_MARK)

#: Loop cadence (seconds).  Cron's smallest unit is a minute, so a 30 s poll
#: fires tasks within half a minute of their due time.
POLL_INTERVAL = 30

#: A fire whose due time is within this many seconds of "now" is treated as
#: freshly due (fire it); older than this it was missed while slife was down.
MISS_GRACE = 120

#: Bound on stepping through fires when hunting the newest missed one.
_MAX_FIRE_STEPS = 5000

#: Scheduled-task worker names dispatched this session — used to reword the
#: completion notification (hide the subagent) and to target recycling.
_SCHEDULE_WORKERS: set[str] = set()

#: Tasks whose trigger was injected but whose dispatch is not yet confirmed by
#: ``run_schedule_now`` — prevents the 30s poll re-firing mid-turn.
#: name → monotonic fire time (for stale purging).
_pending_fires: dict[str, float] = {}


def trigger_text(name: str, description: str) -> str:
    """Build the trigger message injected into the inbox when a task fires.

    The message prompts the agent to dispatch via ``run_schedule_now``; the
    worker then does the task, saves the report and notifies the user.
    Template-managed in ``schedule_trigger.j2`` (whose first line must keep
    the ``[Schedule `` prefix — ``SCHEDULE_MARK`` / ``is_autonomous_trigger``
    rely on it).
    """
    desc = (description or "").strip() or "(no description)"
    return render_template(
        "schedule_trigger.j2",
        name=name,
        description=desc,
    )


def build_worker_task(name: str, description: str) -> str:
    """Self-contained task text for the worker running scheduled task *name*.

    The worker starts with a clean context but has the full toolset (incl.
    ``save_cron_report`` and a way to notify the user).  The headless worker
    wraps this as ``[Task <rpc_id> from <agent>] …``.  The text lives in
    ``schedule.j2`` (rendered through ``system_prompt.render_template``).
    """
    desc = (description or "").strip() or "(no description)"
    now = datetime.now().astimezone()
    return render_template(
        "schedule.j2",
        name=name,
        description=desc,
        month_day=now.strftime("%m-%d"),
    )


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO timestamp to an aware local datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt.astimezone()


def _memfiles_client(service):
    """Return the live memfiles MCP client, or None if not connected."""
    ctx = getattr(service, "_tool_ctx", None)
    return getattr(ctx, "memfiles_client", None) if ctx is not None else None


async def _call(client, tool: str, arguments: dict | None = None):
    """Call a memfiles MCP tool and parse its JSON result (None on failure)."""
    try:
        raw = await client.call_tool(tool, arguments)
        return json.loads(raw) if raw else None
    except Exception as e:
        logger.debug("schedule_tool_error tool=%s err=%s", tool, e)
        return None


async def _load_task_states(client) -> list[dict]:
    """Enabled tasks, each annotated with its newest run ``due_at``."""
    data = await _call(client, "__scheduled_tasks_state")
    return data if isinstance(data, list) else []


def _latest_fire_at_or_before(
    schedule: str, anchor: datetime, now: datetime, tz: str | None,
) -> datetime | None:
    """Return the newest fire time in ``(anchor, now]``, or None.

    Steps :func:`slife.schedules.next_run` forward from *anchor* until it
    passes *now*.  Cheap in the common case (the anchor is the last run, so
    one step reaches the current fire); bounded by ``_MAX_FIRE_STEPS`` for
    pathological long downtimes.
    """
    from slife.schedules import ScheduleError, next_run

    latest = None
    cur = anchor
    for _ in range(_MAX_FIRE_STEPS):
        try:
            nxt = next_run(schedule, cur, tz=tz)
        except ScheduleError:
            break
        if nxt > now:
            break
        latest = nxt
        cur = nxt
    return latest


def _classify(task: dict, now: datetime):
    """Decide what to do with *task* at *now*.

    Returns ``("fire", due_dt)`` when a run is freshly due, ``("missed",
    due_dt)`` when the newest fire is older than the grace window (slife was
    down), or ``None`` when nothing is due (not yet time, manual, bad expr).
    Pure — no DB access — so the timing decision is unit-testable.
    """
    from slife.schedules import ScheduleError, next_run

    schedule = (task.get("schedule") or "").strip()
    if not schedule or schedule == "manual":
        return None
    tz = task.get("timezone") or None
    anchor = (
        _parse_iso(task.get("last_run_due"))
        or _parse_iso(task.get("created_at"))
        or now
    )
    try:
        candidate = next_run(schedule, anchor, tz=tz)
    except ScheduleError:
        return None
    if candidate > now:
        return None
    latest = _latest_fire_at_or_before(schedule, anchor, now, tz)
    if latest is None:
        return None
    if (now - latest).total_seconds() <= MISS_GRACE:
        return ("fire", latest)
    return ("missed", latest)


async def _fire(service, task: dict) -> None:
    """Inject the trigger message for *task*.

    The run itself is recorded by ``run_schedule_now`` when the agent
    dispatches; this only prompts it and holds the pending-fire guard so the
    30s poll does not re-fire while the agent is mid-turn.
    """
    from slife.a2a.identity import SYSTEM, AgentMessage, Channel
    from slife.agent.heartbeat import _SilentHandler

    name = task.get("name", "")
    _pending_fires[name] = _time.monotonic()

    inbox = getattr(service, "inbox", None)
    if inbox is None:
        logger.warning("schedule_fire_no_inbox task=%s", name)
        return
    await inbox.post(AgentMessage(
        source=SYSTEM,
        content=trigger_text(name, task.get("description", "")),
        handler=_SilentHandler(),
        on_reply=_surface_reply(service),
        channel=Channel.system(),
    ))
    logger.info("schedule_fired task=%s", name)


def _surface_reply(service):
    """``on_reply`` for schedule turns — route the real reply to the TUI."""
    async def _reply(text: str, cancelled: bool = False) -> None:
        t = (text or "").strip()
        if t and t != ".":
            await service.surface_schedule(t)
    return _reply


#: Cap on runs shown in the per-turn footer reminder.
_SYS_NOTE_MAX_RUNS = 20


async def _pending_schedule_runs(client) -> list[dict]:
    """Open failed/missed runs, ready for the ``_sys_note`` footer.

    Failed and missed are the same question to the user ("backfill or
    skip?"), so they are merged into one deduplicated list of
    ``{name, due_at, status}`` items, newest first.  ``status`` keeps the
    failed/missed distinction visible per line.  A run has exactly one
    status, and the ``(task_id, due_at)`` guard guarantees it is never
    rendered twice.
    """
    names: dict[int, str] = {}
    tasks = await _call(client, "scheduled_task_list")
    if isinstance(tasks, dict):
        for t in tasks.get("tasks", []):
            tid = t.get("id")
            if tid is not None:
                names[tid] = t.get("name", "")

    seen: set[tuple] = set()
    runs: list[dict] = []
    for status in ("failed", "missed"):
        data = await _call(client, "scheduled_run_list", {"status": status})
        if not isinstance(data, dict):
            continue
        for r in data.get("runs", []):
            key = (r.get("task_id"), r.get("due_at"))
            if key in seen:
                continue  # never render the same run twice
            seen.add(key)
            runs.append({
                "name": names.get(r.get("task_id"), ""),
                "due_at": r.get("due_at", ""),
                "status": r.get("status", status),
            })

    runs.sort(key=lambda r: r["due_at"], reverse=True)
    return runs[:_SYS_NOTE_MAX_RUNS]


async def _refresh_schedule_status(service, client) -> None:
    """Re-publish the open failed/missed runs for the ``_sys_note`` footer."""
    runs = await _pending_schedule_runs(client)
    try:
        service.set_schedule_pending(runs)
    except Exception:
        logger.debug("schedule_note_refresh_error", exc_info=True)


async def schedule_startup_sweep(service) -> None:
    """One-shot startup sweep: settle runs a previous lifetime left behind.

    Runs exactly once per process lifetime, outside the timed loop, and
    posts no message (nothing is announced; the settled records feed the
    per-turn footer reminder and ``scheduled_run_list``):

      1. Sweep unconfirmed runs to ``failed``
         (``__scheduled_fail_unconfirmed``) — a run is recorded ``pending``
         optimistically and only becomes ``ran`` when the worker's report
         arrives, so anything still ``pending`` (or a legacy
         ``ran``-without-report) at startup can never complete: the process
         that dispatched it is gone.
      2. Mark fires that were due while slife was down ``missed``
         (``__scheduled_mark_missed``), so the downtime shows in history
         and the anchor advances past the missed fire.

    ``schedule_loop`` fires due tasks only and never sweeps, so these run
    records survive across a downtime precisely until this pass settles
    them.
    """
    # The sweep must read the client only after every plugin has converged
    # (memfiles confirms serving by its own __ready handshake).  Event-
    # driven, no polling, no timing guess.
    await service.wait_startup_settled()
    client = _memfiles_client(service)
    if client is None:
        # memfiles unavailable (failed/degraded) — nothing coherent to
        # settle; the failure is surfaced elsewhere.
        return

    # 1. Reap dispatch-only runs from a previous lifetime → failed.
    resp = await _call(client, "__scheduled_fail_unconfirmed")
    if isinstance(resp, dict) and (resp.get("runs") or []):
        logger.info("schedule_startup_swept_failed count=%d", len(resp["runs"]))

    # 2. Mark fires that were due while slife was down → missed.
    now = datetime.now().astimezone()
    for task in await _load_task_states(client):
        decision = _classify(task, now)
        if decision is None or decision[0] != "missed":
            continue
        _, due_dt = decision
        due_iso = due_dt.astimezone().isoformat(timespec="seconds")
        await _call(client, "__scheduled_mark_missed",
                    {"task_id": task["id"], "due_at": due_iso})
        logger.info("schedule_missed task=%s due_at=%s",
                    task.get("name"), due_iso)

    # Feed the footer's backfill reminder with what we just settled.
    await _refresh_schedule_status(service, client)


async def _recycle_idle_workers(states) -> None:
    """Stop schedule workers whose task is settled (no pending run) and idle.

    Runs on the schedule cadence.  Only workers named after a scheduled task
    that was dispatched this session (in ``_SCHEDULE_WORKERS``) are touched,
    and only when they are not busy and have no pending async tasks — so a
    result still in flight is never cut off.
    """
    from slife.subagent.process import get_manager

    manager = get_manager()
    if manager is None:
        return
    for task in states:
        name = task.get("name")
        if not name or name not in _SCHEDULE_WORKERS:
            continue
        if task.get("has_pending_run"):
            continue  # a run is still in flight — keep the worker
        proc = manager.get(name)
        if proc is None or not proc.is_running:
            continue
        if proc.is_busy or proc.pending_async_count > 0:
            continue
        try:
            await manager.stop(name)
            logger.info("schedule_worker_recycled name=%s", name)
        except Exception as e:
            logger.debug("schedule_worker_recycle_error name=%s err=%s", name, e)


async def schedule_loop(service) -> None:
    """Time enabled scheduled tasks and inject triggers (main agent only).

    Fires due runs only.  A fire is always observed within the grace window
    (30 s poll << 120 s grace), so while the process is up no run can slip
    past being fired; runs a previous process lifetime left unfinished are
    settled exactly once at startup by :func:`schedule_startup_sweep`
    (unconfirmed runs → ``failed``, fires due while down → ``missed``).
    Also reaps idle schedule workers whose task has settled.
    """
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            client = _memfiles_client(service)
            if client is None:
                continue
            now = datetime.now().astimezone()
            states = await _load_task_states(client)
            await _recycle_idle_workers(states)

            # Keep the footer's failed/missed reminder current (it renders
            # only while open runs exist).
            await _refresh_schedule_status(service, client)

            # Drop stale pending-fire guards (the agent never dispatched).
            stale = [n for n, t in _pending_fires.items()
                     if _time.monotonic() - t > MISS_GRACE]
            for n in stale:
                _pending_fires.pop(n, None)

            for task in states:
                if task.get("name") in _pending_fires:
                    continue  # trigger injected, dispatch pending
                decision = _classify(task, now)
                if decision is None:
                    continue
                action = decision[0]
                if action == "fire":
                    await _fire(service, task)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("schedule_loop_error err=%s", e)


async def fire_task_now(service, name: str) -> str:
    """Trigger a scheduled task now — the single dispatch path.

    Used by both the cron trigger (the agent calls ``run_schedule_now`` after
    the ``[Schedule <name>]`` message) and missed-run backfill.  Records a
    pending run (``due_at = now``) and dispatches the task to its worker
    (worker name = task name) asynchronously; the worker must call
    ``save_cron_report`` so the run's ``pending`` transitions to ``ran``.
    Works for disabled tasks too (an explicit run is explicit).
    """
    from slife.subagent.process import get_manager

    client = _memfiles_client(service)
    if client is None:
        return "Error: memfiles plugin not connected — cannot run scheduled task."
    task = await _call(client, "__scheduled_task_by_name", {"name": name})
    if not task:
        return f"Scheduled task not found: {name}"

    due_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    await _call(client, "__scheduled_record_run",
                {"task_id": task["id"], "due_at": due_iso})

    worker = task["name"]
    manager = get_manager()
    if manager is None:
        return (
            "Error: the subagent manager is not available yet — call this "
            "after the agent service has started."
        )
    try:
        await manager.spawn(name=worker)
        rpc_id = await manager.send_task_async(
            worker,
            build_worker_task(worker, task.get("description", "")),
            mode="auto",
        )
    except Exception as e:
        logger.warning("schedule_dispatch_failed task=%s err=%s", name, e)
        await _call(client, "__scheduled_mark_run_failed",
                    {"task_id": task["id"], "due_at": due_iso,
                     "error": str(e)[:200]})
        return f"Error: dispatch failed — {e}"

    _SCHEDULE_WORKERS.add(worker)
    _pending_fires.pop(worker, None)
    return (
        f"Scheduled task '{name}' dispatched now to worker "
        f"'{worker}' (task_id: {rpc_id})."
    )
