"""Scheduled-task trigger loop — the main-process scheduler.

The loop is deliberately thin: it *times* tasks and *injects* a trigger
message into the unified inbox.  Execution happens elsewhere — the main
agent handles the trigger by delegating to a subagent worker, which saves
its report via ``save_cron_report``.  The loop itself never runs a task.

State lives in the memfiles DB (``scheduled_tasks`` / ``scheduled_runs``),
not in memory.  Each poll recomputes from the DB, so the loop is stateless
and restart-safe:

  * anchor = newest ``due_at`` across all of a task's runs (ran *and* missed
    both advance the anchor — a missed fire is never re-detected forever),
    falling back to ``created_at`` before the first run.
  * candidate = ``next_run(anchor)``.  If it is in the future, wait.
  * If it is due-or-overdue, compare the newest fire ``<= now`` against a
    short grace window: within the window the fire just became due (fire it);
    past it, slife was down when the fire was due (mark it ``missed``).

Only the newest missed fire is recorded (not every fire inside a long
downtime) — it is the actionable one.  Missed runs persist so a restart can
surface them and offer a backfill (:func:`fire_task_now` runs a task now,
``scheduled_run_skip`` closes a missed/failed run the user declines).

Run outcome is **optimistically pending**: a fire is recorded as ``pending``
and only becomes ``ran`` when the worker's report arrives.  Everything else —
turn error, Esc-interrupt, restart — leaves it a ``failed``-by-default (the
turn's reply hook may write a best-effort error detail; startup reaps any
stale ``pending`` whose process lifetime is gone).  This inversion means an
incomplete run is *never* recorded as a success.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

#: TUI filter mark — a trigger turn's user message starts with this prefix.
SCHEDULE_MARK = "[Schedule"


def is_autonomous_trigger(text: str) -> bool:
    """True when *text* is a synthetic autonomous trigger (heartbeat or a
    schedule trigger), not a real user query.  Used to filter these turns
    from the TUI and to skip turn-footnote annotation."""
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


def trigger_text(name: str, description: str) -> str:
    """Build the trigger message injected into the inbox when a task fires."""
    desc = (description or "").strip() or "(no description)"
    return (
        f"{SCHEDULE_MARK} {name}] 定时任务触发。\n"
        f"任务描述：{desc}\n"
        f'请创建（或复用）名为 "{name}" 的 subagent，用 '
        f"subagent_send_task_async(mode=\"auto\") 异步执行该任务；任务文本里要求 "
        f"worker 完成后调用 save_cron_report(name=\"{name}\", title=..., content=...) "
        f"保存结果。你只需派发，不必自己执行；完成后简短确认即可。"
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


async def _fire(service, client, task: dict, due_at: datetime) -> None:
    """Record a run and inject the trigger message for *task*."""
    from slife.a2a.identity import SCHEDULE, AgentMessage
    from slife.agent.heartbeat import _SilentHandler

    due_iso = due_at.astimezone().isoformat(timespec="seconds")
    await _call(client, "__scheduled_record_run",
                {"task_id": task["id"], "due_at": due_iso})

    inbox = getattr(service, "inbox", None)
    if inbox is None:
        logger.warning("schedule_fire_no_inbox task=%s", task.get("name"))
        return
    await inbox.post(AgentMessage(
        source=SCHEDULE,
        content=trigger_text(task["name"], task.get("description", "")),
        handler=_SilentHandler(),
        on_reply=_schedule_reply(service, client, task, due_iso),
    ))
    logger.info("schedule_fired task=%s due_at=%s", task.get("name"), due_iso)


def _surface_reply(service):
    """``on_reply`` for notice turns — route the real reply to the TUI."""
    async def _reply(text: str, cancelled: bool = False) -> None:
        t = (text or "").strip()
        if t and t != ".":
            await service.surface_autonomous(t)
    return _reply


def _schedule_reply(service, client, task: dict, due_iso: str):
    """``on_reply`` for a schedule trigger turn.

    Surfaces a real reply to the user and, best effort only, records a
    failure reason on the pending run when the turn errored or was
    interrupted.  Correctness never depends on this writeback — a pending run
    without a report is a failure by default.
    """
    async def _reply(text: str, cancelled: bool = False) -> None:
        t = (text or "").strip()
        if cancelled:
            await _call(client, "__scheduled_mark_run_failed", {
                "task_id": task["id"], "due_at": due_iso,
                "error": "interrupted",
            })
        elif t.startswith("Error:"):
            await _call(client, "__scheduled_mark_run_failed", {
                "task_id": task["id"], "due_at": due_iso, "error": t[:200],
            })
        if t and t != ".":
            await service.surface_autonomous(t)
    return _reply


async def _missed_notice(service, missed: list[dict], stale: list[dict]) -> None:
    """Inject a notice listing missed and failed-incomplete runs.

    Surfaces everything the user may want to backfill in one turn: *missed*
    runs (slife was down at the due time) and *stale* runs a restart swept to
    ``failed`` (dispatched but never produced a report).
    """
    from slife.a2a.identity import SCHEDULE, AgentMessage
    from slife.agent.heartbeat import _SilentHandler

    parts: list[str] = []
    if missed:
        parts.append("错过（slife 未运行期间到期）: "
                     + ", ".join(f"{m['name']} @ {m['due_at']}" for m in missed))
    if stale:
        parts.append("未完成（已触发但无报告，重启中断）: "
                     + ", ".join(f"{m['name']} @ {m['due_at']}" for m in stale))
    if not parts:
        return
    inbox = getattr(service, "inbox", None)
    if inbox is None:
        return
    await inbox.post(AgentMessage(
        source=SCHEDULE,
        content=(
            f"{SCHEDULE_MARK} missed] 发现需要处理的定时执行：{'；'.join(parts)}。"
            "请告知用户，并询问是否用 run_schedule_now 补做；"
            "若不需要，用 scheduled_run_skip 标记为已跳过。"
        ),
        handler=_SilentHandler(),
        on_reply=_surface_reply(service),
    ))


async def schedule_startup_notice(service) -> None:
    """One-shot startup pass: surface runs that failed to complete.

    Runs exactly once per process lifetime, outside the timed loop.  The
    poll catches every fire within a few seconds (30 s cadence << 120 s
    grace), so while the process is up a run can never be missed — an
    incomplete run only exists across a downtime.  This pass is the single
    place that reaps it and tells the LLM:

      1. Mark every run that never produced a report ``failed``
         (``__scheduled_fail_unconfirmed`` — whatever the reason: turn
         error, interruption, restart).
      2. Detect fires that were due while slife was down and mark them
         ``missed``.
      3. Post one ``[Schedule missed]`` notice covering both, so the LLM
         can offer ``run_schedule_now`` backfill or ``scheduled_run_skip``.

    ``schedule_loop`` fires only and never re-announces — the missed
    notice fires exactly once, at startup.
    """
    # The notice must be the service's FIRST message: wait for the service to
    # open (every plugin spawn converged — memfiles confirmed serving by its
    # own __ready handshake), then read the client once.  Event-driven, no
    # polling, no timing guess — the inbox consumer and the TUI input gate
    # on the same event, so nothing can be queued ahead of this notice.
    await service.wait_startup_settled()
    client = _memfiles_client(service)
    if client is None:
        # memfiles unavailable (failed/degraded) — nothing coherent to sweep
        # or announce; the failure is surfaced elsewhere.
        return

    # 1. Reap dispatch-only runs from a previous lifetime → failed.
    stale_failed: list[dict] = []
    resp = await _call(client, "__scheduled_fail_unconfirmed")
    if isinstance(resp, dict):
        stale_failed = resp.get("runs") or []

    # 2. Detect fires that were due while slife was down → missed.
    now = datetime.now().astimezone()
    fresh_missed: list[dict] = []
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
        fresh_missed.append({"name": task.get("name"), "due_at": due_iso})

    # 3. Tell the LLM once — it decides backfill vs skip.
    if fresh_missed or stale_failed:
        await _missed_notice(service, fresh_missed, stale_failed)


async def schedule_loop(service) -> None:
    """Time enabled scheduled tasks and inject triggers (main agent only).

    Fires due runs only.  A fire is always observed within the grace window
    (30 s poll << 120 s grace), so while the process is up no run can slip
    past being fired; runs missed across a downtime are marked and announced
    exactly once at startup by :func:`schedule_startup_notice`.
    """
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            client = _memfiles_client(service)
            if client is None:
                continue
            now = datetime.now().astimezone()
            for task in await _load_task_states(client):
                decision = _classify(task, now)
                if decision is None:
                    continue
                action, due_dt = decision
                if action == "fire":
                    await _fire(service, client, task, due_dt)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("schedule_loop_error err=%s", e)


async def fire_task_now(service, name: str) -> str:
    """Run a scheduled task immediately (backfill / manual trigger).

    Records a run with ``due_at = now`` and injects the trigger.  Works for
    disabled tasks too (a manual run is explicit).  Returns a result string.
    """
    client = _memfiles_client(service)
    if client is None:
        return "Error: memfiles plugin not connected — cannot run scheduled task."
    task = await _call(client, "__scheduled_task_by_name", {"name": name})
    if not task:
        return f"Scheduled task not found: {name}"
    await _fire(service, client, task, datetime.now().astimezone())
    return f"Scheduled task '{name}' dispatched now."
