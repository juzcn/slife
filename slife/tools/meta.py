"""Meta tools — agent self-management.

list_native_tools  — native tool inventory (grouped, with harness markers)
check_async        — poll background task result
cancel_async       — cancel a running background task
clear_context      — reset the loaded turns
set_max_iterations — change the loop's iteration cap at runtime (0 = unlimited)
notify_user        — push a desktop notification to the human operator
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from typing import ClassVar

from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute
from slife.tools.base import Tool, make_params

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# list_native_tools
# ═══════════════════════════════════════════════════════════════════════


def _native_category(tool) -> str:
    """Display category for a tool in ``list_native_tools``.

    Source-based, no name-prefix guessing:
      - native tools carry their own ``category`` class attribute;
      - built-in plugin tools (MCP proxy tools) group by their plugin
        name — the same identity the ``[<server>] `` description prefix
        and the ``server`` field carry.
    External MCP proxy tools are filtered out before this is called.
    """
    if isinstance(tool, MCPProxyTool):
        return getattr(tool, "_server", "") or "Plugins"
    return getattr(tool, "category", "") or "Other"


class ListNativeToolsTool(Tool):
    name: ClassVar[str] = "list_native_tools"
    category: ClassVar[str] = "Meta"
    description: ClassVar[str] = (
        "Inventory of native tools (grouped, first-sentence summaries, "
        "harness/auto-invoked markers). Includes built-in plugin tools "
        "(Turns DB, File Cabinet, sharing, media, WeChat, A2A) — they are "
        "first-class like native tools. External MCP server tools are NOT "
        "listed — the model already receives their full schemas natively."
    )
    parameters: ClassVar[dict] = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        registry = ctx.registry if ctx is not None else None
        if registry is None:
            return "Tool registry is not available (called before initialization)."

        all_tools = registry.list_tools()
        if not all_tools:
            return "No tools are currently registered."

        # External MCP server tools are excluded: the model already receives
        # their full schemas in the native `tools` array of every request, so
        # a second listing here is pure redundant context.  Built-in plugin
        # tools (DIRECT/WRAPPER — bare names) ARE native: included here.
        natives = [
            t for t in all_tools
            if not (isinstance(t, MCPProxyTool) and t._route == ProxyRoute.EXTERNAL)
        ]
        if not natives:
            return "No native tools are currently registered."

        lines = [f"## Native Tools ({len(natives)} total)\n"]
        native_groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for t in sorted(natives, key=lambda t: t.name):
            cat = _native_category(t)
            desc = t.description.split(".")[0].strip() + "."
            native_groups[cat].append((t.name, desc))

        for cat in sorted(native_groups):
            items = native_groups[cat]
            lines.append(f"### {cat} ({len(items)})")
            for name, desc in items:
                marker = " — harness, auto-invoked" if name.startswith("_") else ""
                lines.append(f"- **`{name}`**{marker} — {desc}")
            lines.append("")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# Async tasks
# ═══════════════════════════════════════════════════════════════════════

_tasks: dict[str, asyncio.Task] = {}

#: Bound on ``_tasks`` — an ``_async: true`` call the LLM never polls would
#: otherwise keep its finished task in the dict (holding tool resources) for
#: the whole session.
_MAX_ASYNC_TASKS = 100


def _prune_old_done() -> None:
    """Drop the oldest completed tasks while over the cap; running tasks stay."""
    if len(_tasks) <= _MAX_ASYNC_TASKS:
        return
    for tid in list(_tasks.keys()):  # insertion order = oldest first
        if len(_tasks) <= _MAX_ASYNC_TASKS:
            break
        task = _tasks[tid]
        if task.done():
            _tasks.pop(tid, None)


def schedule(coro) -> str:
    task_id = uuid.uuid4().hex[:8]
    task = asyncio.create_task(_runner(coro, task_id))
    _tasks[task_id] = task
    _prune_old_done()
    logger.info("async_task_scheduled id=%s", task_id)
    return task_id


async def _runner(coro, task_id: str) -> str:
    try:
        result = await coro
    except Exception as e:
        result = f"Error: {type(e).__name__}: {e}"
    logger.info("async_task_done id=%s len=%d", task_id, len(result))
    return result


def _get_task(task_id: str) -> asyncio.Task | None:
    return _tasks.get(task_id)


def _pop_task(task_id: str) -> asyncio.Task | None:
    return _tasks.pop(task_id, None)


class CheckAsyncTool(Tool):
    name: ClassVar[str] = "check_async"
    category: ClassVar[str] = "Meta"
    description: ClassVar[str] = "Query an async task result. Returns status while running, the result when done."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task_id returned by the async task."},
        },
        "required": ["task_id"],
    }

    async def execute(self, **kwargs) -> str:
        task_id: str = kwargs["task_id"]
        task = _get_task(task_id)
        if task is None:
            return f"Error: Task '{task_id}' not found. It may have already completed and been cleaned up, or the task_id is incorrect."
        if not task.done():
            return f"⏳ Task is still running…\n  task_id: {task_id}\n  Try check_async again later."
        _pop_task(task_id)
        try:
            result = task.result()
        except Exception as e:
            result = f"Error: Async task failed: {type(e).__name__}: {e}"
        return f"✓ Task completed (task_id: {task_id})\n\n{result}"


class CancelAsyncTool(Tool):
    name: ClassVar[str] = "cancel_async"
    category: ClassVar[str] = "Meta"
    description: ClassVar[str] = "Cancel a running async task. Completed tasks cannot be cancelled."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task_id to cancel."},
        },
        "required": ["task_id"],
    }

    async def execute(self, **kwargs) -> str:
        task_id: str = kwargs["task_id"]
        task = _get_task(task_id)
        if task is None:
            return f"Error: Task '{task_id}' not found. It may have already completed and been cleaned up, or the task_id is incorrect."
        if task.done():
            _pop_task(task_id)
            return f"Task '{task_id}' already completed — nothing to cancel."
        task.cancel()
        _pop_task(task_id)
        logger.info("async_task_cancelled id=%s", task_id)
        return f"✓ Task '{task_id}' cancelled."


# ═══════════════════════════════════════════════════════════════════════
# clear_context
# ═══════════════════════════════════════════════════════════════════════

class ClearContextTool(Tool):
    name = "clear_context"
    category: ClassVar[str] = "Meta"
    description = "Clear the loaded turns from context, keeping only the system prompt."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        conv = ctx.message_history if ctx is not None else None
        if conv is None:
            return "MessageHistory is not yet initialised. This tool must be called after the agent service has started."
        removed = conv.clear_history()
        if removed == 0:
            return "Context is already clean — no old turns to remove."
        # Flush the persisted live-context boundary to the latest saved
        # turn, so the next restore is a genuine fresh start (only turns
        # saved afterwards come back).  Best-effort: an unreachable memdb
        # only makes the next restore a superset, never a loss.
        set_latest = getattr(ctx, "set_context_start_latest", None)
        if set_latest is not None:
            try:
                await set_latest()
            except Exception:
                logger.exception("context_start_latest_failed")
        # Restart the "Context covers" range — otherwise the next _sys_note
        # would keep reporting the pre-clear start.
        reset_time = getattr(ctx, "reset_context_time", None)
        if reset_time is not None:
            try:
                reset_time()
            except Exception:
                logger.exception("context_time_reset_failed")
        remaining = len(conv.messages)
        logger.info("clear_context removed=%d remaining=%d", removed, remaining)
        return f"[OK] Cleared {removed} old message(s); {remaining} remaining (system prompt + current turn)."


# ═══════════════════════════════════════════════════════════════════════
# set_max_iterations
# ═══════════════════════════════════════════════════════════════════════


class SetMaxIterationsTool(Tool):
    name = "set_max_iterations"
    category: ClassVar[str] = "Meta"
    description = (
        "Set the maximum tool-call iterations per turn (0 = unlimited); "
        "applies from the next turn."
    )
    parameters = make_params(
        max_iterations={
            "type": "integer",
            "description": "Max tool-call iterations per turn. 0 = unlimited (no cap).",
        },
    )

    async def execute(self, max_iterations: int = 0, **kwargs) -> str:
        setter = getattr(self, "_ctx", None)
        if setter is not None:
            setter = setter.set_max_iterations
        if setter is None:
            return "Error: agent loop is not available yet — call this after the agent service has started."
        return setter(max_iterations)


# ═══════════════════════════════════════════════════════════════════════
# notify_user
# ═══════════════════════════════════════════════════════════════════════


class NotifyUserTool(Tool):
    """Push a desktop notification to the human operator.

    A pure UI tool — it only triggers the display; the LLM never sees
    the notification itself.
    """

    name: ClassVar[str] = "notify_user"
    category: ClassVar[str] = "Meta"
    description: ClassVar[str] = (
        "Send a desktop notification to the human user."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short notification title (e.g. 'Task Complete', 'Alert').",
            },
            "message": {
                "type": "string",
                "description": "The notification body — be concise (one sentence).",
            },
        },
        "required": ["message"],
    }

    async def execute(self, title: str = "slife", message: str = "", **kwargs) -> str:
        if not message:
            return "Error: message is required."

        # Log for the session file at WARNING (the console is capped below
        # WARNING, so this is diagnostic-only; the notification below is the
        # user-facing channel).
        logging.getLogger(__name__).warning(
            "USER_NOTIFICATION title=%s message=%s", title, message,
        )

        # Fire desktop notification (best-effort, non-blocking).
        # Daemon thread: a hung notify backend must never block shutdown.
        from slife.platform import desktop_notify
        from slife.threads import run_daemon
        run_daemon(desktop_notify, title, message, name="desktop-notify")

        return f"Notification sent: [{title}] {message}"



