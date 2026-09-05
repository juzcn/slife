"""Scheduled-task tools — the "Schedule" category.

Owns the LLM-facing surface for scheduled tasks: creating / updating / removing
tasks, listing tasks and runs, skipping a missed/failed run, and firing a task
now.  The state itself lives in the memfiles plugin's database
(``scheduled_tasks`` / ``scheduled_runs``), so every tool delegates over the
memfiles MCP client to the plugin's internal ``__scheduled_*`` data tools —
the main process never touches the plugin's SQLite directly.  The schedule
trigger loop (``slife/agent/schedules.py``) calls those same internal tools.

Only idempotent-free pure logic (safe task-name regex, cron validation, JSON
formatting) lives here in-process.  ``run_schedule_now`` was moved here from
``exec.py`` (now "Schedule" instead of "Execution").

Tools:
    scheduled_task_set    — create or update a scheduled task (upsert by name)
    scheduled_task_remove — delete a task (run history cleared, reports kept)
    scheduled_task_list   — list tasks (enabled_only filter)
    scheduled_run_list    — list run records (name / status filters)
    scheduled_run_skip    — mark a missed/failed run as skipped
    run_schedule_now      — trigger a scheduled task immediately (dispatch worker)
"""

from __future__ import annotations

import json
import logging
import re
from typing import ClassVar

from slife.schedules import is_valid
from slife.tools.base import Tool, make_params, require_params

logger = logging.getLogger(__name__)

#: Task names double as the subagent worker name (``run_schedule_now`` spawns a
#: worker named after the task), so they must satisfy the subagent safe-name
#: rule — the same pattern as ``slife/subagent/process.py _SAFE_SUBAGENT_NAME``.
_SAFE_TASK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

_OFFLINE = (
    "Error: memfiles plugin not connected — scheduled-task tools are unavailable."
)


def _parse(raw: str | None):
    """Parse an internal tool's JSON result; pass non-JSON strings through."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


class _ScheduleMixin:
    """Client plumbing shared by the schedule tools.

    A plain (non-``Tool``) mixin: every data-touching op delegates to the
    memfiles plugin's internal ``__scheduled_*`` tools over the MCP client on
    ``ToolContext`` (``memfiles_client``).  Pure validation / formatting stays
    in-process.  Mirrors the ``CheckMemfilesTool`` helper split in
    ``slife/tools/system.py``.
    """

    def _client(self):
        ctx = getattr(self, "_ctx", None)
        return getattr(ctx, "memfiles_client", None) if ctx is not None else None

    async def _call(self, tool: str, arguments: dict | None = None):
        client = self._client()
        if client is None:
            return _OFFLINE
        try:
            raw = await client.call_tool(tool, arguments)
        except Exception as e:
            logger.debug("schedule_tool_error tool=%s err=%s", tool, e)
            return f"Error: {tool} failed — {e}"
        return _parse(raw)

    def _format(self, data):
        """Pretty-print dict/list results; pass error strings straight back."""
        if isinstance(data, (dict, list)):
            return json.dumps(data, ensure_ascii=False, indent=2)
        return data if data else _OFFLINE


class ScheduledTaskSetTool(_ScheduleMixin, Tool):
    """Create or update a scheduled task (idempotent upsert by name)."""

    name = "scheduled_task_set"
    category: ClassVar[str] = "Schedule"
    description = (
        "Create or update a scheduled task (upsert by name)."
    )
    parameters = make_params(
        name={
            "type": "string",
            "description": (
                "Unique task name — ASCII slug (letter/digit start, "
                "A-Za-z0-9_.-, max 64); also the subagent worker name."
            ),
        },
        description={
            "type": "string",
            "default": "",
            "description": "The task text handed to the worker when it fires.",
        },
        schedule={
            "type": "string",
            "default": "",
            "description": (
                "5-field cron expression (\"minute hour dom month dow\"), or "
                "\"manual\" (runs only via run_schedule_now)."
            ),
        },
        timezone={
            "type": "string",
            "default": "",
            "description": "IANA timezone (empty = system local).",
        },
        enabled={
            "type": "boolean",
            "default": True,
            "description": "Fire this task on schedule.",
        },
    )

    async def execute(
        self, name: str = "", description: str = "", schedule: str = "",
        timezone: str = "", enabled: bool = True, **kwargs,
    ) -> str:
        name = (name or "").strip()
        if not name:
            return "Error: name is required."
        if not _SAFE_TASK_NAME_RE.match(name):
            return (
                f"Error: name {name!r} is not a valid task/worker name — it is "
                "also the subagent worker name, so it must start with a letter "
                "or digit and contain only A-Za-z0-9_.- (max 64 chars).  Use an "
                'ASCII slug like "daily_report".'
            )
        if schedule and schedule != "manual" and not is_valid(schedule):
            return f"Error: invalid cron expression {schedule!r}."
        return self._format(await self._call("__scheduled_task_upsert", {
            "name": name, "description": description, "schedule": schedule,
            "timezone": timezone, "enabled": enabled,
        }))


class ScheduledTaskRemoveTool(_ScheduleMixin, Tool):
    """Delete a scheduled task and its run history by name (reports kept)."""

    name = "scheduled_task_remove"
    category: ClassVar[str] = "Schedule"
    description = (
        "Delete a scheduled task and its run history by name (saved reports kept)."
    )
    parameters = make_params(
        name={
            "type": "string",
            "description": "The task name to remove (from scheduled_task_list).",
        },
    )

    async def execute(self, name: str = "", **kwargs) -> str:
        if not (name or "").strip():
            return "Error: name is required."
        return self._format(await self._call("__scheduled_task_remove", {"name": name}))


class ScheduledTaskListTool(_ScheduleMixin, Tool):
    """List scheduled tasks."""

    name = "scheduled_task_list"
    category: ClassVar[str] = "Schedule"
    description = (
        "List scheduled tasks (name, schedule, timezone, enabled)."
    )
    parameters = make_params(
        enabled_only={
            "type": "boolean",
            "default": False,
            "description": "When true, list only enabled tasks.",
        },
    )

    async def execute(self, enabled_only: bool = False, **kwargs) -> str:
        return self._format(
            await self._call("__scheduled_tasks_list", {"enabled_only": enabled_only})
        )


class ScheduledRunListTool(_ScheduleMixin, Tool):
    """List scheduled-task run records."""

    name = "scheduled_run_list"
    category: ClassVar[str] = "Schedule"
    description = (
        "List scheduled-task run records, newest first (name/status filter)."
    )
    parameters = make_params(
        name={
            "type": "string",
            "default": "",
            "description": "Filter by task name (omitted = all).",
        },
        status={
            "type": "string",
            "default": "",
            "description": "pending/ran/failed/missed/skipped",
        },
        limit={
            "type": "integer",
            "default": 50,
            "description": "Maximum records to return.",
        },
    )

    async def execute(
        self, name: str = "", status: str = "", limit: int = 50, **kwargs,
    ) -> str:
        return self._format(await self._call("__scheduled_runs_list", {
            "name": name, "status": status, "limit": limit,
        }))


class ScheduledRunSkipTool(_ScheduleMixin, Tool):
    """Mark a missed/failed run as skipped (user declined to backfill)."""

    name = "scheduled_run_skip"
    category: ClassVar[str] = "Schedule"
    description = (
        "Mark a missed/failed scheduled run as skipped (only 'missed' or "
        "'failed' statuses change)."
    )
    parameters = make_params(
        name={
            "type": "string",
            "description": "The task name the run belongs to.",
        },
        due_at={
            "type": "string",
            "description": "The run's scheduled time (ISO, from scheduled_run_list).",
        },
    )

    async def execute(self, name: str = "", due_at: str = "", **kwargs) -> str:
        if err := require_params(name=name, due_at=due_at):
            return err
        return self._format(
            await self._call("__scheduled_run_skip", {"name": name, "due_at": due_at})
        )


class RunScheduleNowTool(Tool):
    """Trigger a scheduled task now (cron fire / missed backfill)."""

    name = "run_schedule_now"
    category: ClassVar[str] = "Schedule"
    description = (
        "Trigger a scheduled task now; backfill: pass the run's due_at."
    )
    parameters = make_params(
        name={
            "type": "string",
            "description": "The scheduled task's name (from scheduled_task_list).",
        },
        due_at={
            "type": "string",
            "default": "",
            "description": (
                "ISO due time of the run to backfill (from scheduled_run_list); "
                "omit for a fresh run."
            ),
        },
    )

    async def execute(self, name: str = "", due_at: str = "", **kwargs) -> str:
        if err := require_params(name=name):
            return err
        ctx = getattr(self, "_ctx", None)
        fire = getattr(ctx, "fire_schedule_now", None) if ctx is not None else None
        if fire is None:
            return (
                "Error: the scheduler is not available yet — call this after "
                "the agent service has started."
            )
        return await fire(name, due_at)