"""Scheduled-task native tools — the one operation that must run in the main
process (recording a run + injecting its trigger into the inbox).

Task definitions, run history, reports and confirmations live in the memfiles
plugin as MCP tools (``scheduled_task_set`` / ``scheduled_task_remove`` /
``scheduled_task_list`` / ``scheduled_run_list`` / ``scheduled_run_skip`` /
``save_cron_report`` / ``report_list`` / ``report_read``).  Only
``run_schedule_now`` needs the main agent's inbox, so it is native and reaches
the scheduler through the :class:`~slife.tools.context.ToolContext`
``fire_schedule_now`` hook.
"""

from __future__ import annotations

from typing import ClassVar

from slife.tools.base import Tool, make_params, require_params


class RunScheduleNowTool(Tool):
    """Run a scheduled task immediately (backfill a missed run / manual fire)."""

    name = "run_schedule_now"
    category: ClassVar[str] = "Schedule"
    description = (
        "Run a scheduled task immediately by name — records a run and "
        "dispatches the task to a subagent worker now, regardless of its cron "
        "schedule or enabled state.  Use to backfill a missed run (see "
        "scheduled_run_list) or to trigger a task on demand.  After "
        "backfilling a missed/failed run that won't be re-run, close it with "
        "scheduled_run_skip."
    )
    parameters = make_params(
        name={
            "type": "string",
            "description": "The scheduled task's name (from scheduled_task_list).",
        },
    )

    async def execute(self, name: str = "", **kwargs) -> str:
        if err := require_params(name=name):
            return err
        ctx = getattr(self, "_ctx", None)
        fire = getattr(ctx, "fire_schedule_now", None) if ctx is not None else None
        if fire is None:
            return (
                "Error: the scheduler is not available yet — call this after "
                "the agent service has started."
            )
        return await fire(name)
