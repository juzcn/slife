"""Async tool execution — background tasks with polling and cancellation.

The Agent Loop (``agent/loop.py``) detects ``_async: true`` on any tool
call and schedules it as a background ``asyncio.Task`` instead of awaiting
it.  The LLM can then poll with ``check_async`` or cancel with
``cancel_async``.

Module-level ``_tasks`` dict is the shared store between the Loop and
these tools.
"""

import asyncio
import logging
import uuid
from typing import ClassVar

from slife.tools.base import Tool

logger = logging.getLogger(__name__)

#: Shared task store — keyed by task_id (uuid4 hex, 8 chars).
_tasks: dict[str, asyncio.Task] = {}


def schedule(coro) -> str:
    """Wrap *coro* in an ``asyncio.Task``, store it, and return the task_id.

    Called by the Agent Loop when it sees ``_async: true`` on a tool call.
    """
    task_id = uuid.uuid4().hex[:8]
    task = asyncio.create_task(_runner(coro, task_id))
    _tasks[task_id] = task
    logger.info("async_task_scheduled id=%s", task_id)
    return task_id


async def _runner(coro, task_id: str) -> str:
    """Await *coro*, log completion, and return its result.

    Never raises — exceptions are caught and wrapped as error strings
    so the task always completes cleanly.
    """
    try:
        result = await coro
    except Exception as e:
        result = f"Error: {type(e).__name__}: {e}"
    logger.info("async_task_done id=%s len=%d", task_id, len(result))
    return result


def get(task_id: str) -> asyncio.Task | None:
    """Return the task for *task_id*, or None."""
    return _tasks.get(task_id)


def pop(task_id: str) -> asyncio.Task | None:
    """Remove and return the task for *task_id*."""
    return _tasks.pop(task_id, None)


# ═══════════════════════════════════════════════════════════════════════
# Tools
# ═══════════════════════════════════════════════════════════════════════


class CheckAsyncTool(Tool):
    """Poll for the result of an async task."""

    name: ClassVar[str] = "check_async"
    description: ClassVar[str] = (
        "查询异步任务的结果。"
        "如果任务仍在运行，返回状态提示；如果已完成，返回结果。"
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "异步任务返回的 task_id。",
            },
        },
        "required": ["task_id"],
    }

    async def execute(self, **kwargs) -> str:
        task_id: str = kwargs["task_id"]
        task = get(task_id)

        if task is None:
            return (
                f"Error: 未找到 task_id='{task_id}'。"
                f"任务可能已完成并被清理，或 task_id 不正确。"
            )

        if not task.done():
            return (
                f"⏳ 任务仍在运行中…\n"
                f"  task_id: {task_id}\n"
                f"  稍后再调用 check_async 查询。"
            )

        # Done — clean up and return result
        pop(task_id)

        try:
            result = task.result()
        except Exception as e:
            result = f"Error: 异步任务执行失败：{type(e).__name__}: {e}"

        return f"✓ 任务完成（task_id: {task_id}）\n\n{result}"


class CancelAsyncTool(Tool):
    """Cancel a running async task."""

    name: ClassVar[str] = "cancel_async"
    description: ClassVar[str] = (
        "取消一个正在运行的异步任务。"
        "只能取消尚未完成的任务；已完成的任务无法取消。"
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "要取消的异步任务的 task_id。",
            },
        },
        "required": ["task_id"],
    }

    async def execute(self, **kwargs) -> str:
        task_id: str = kwargs["task_id"]
        task = get(task_id)

        if task is None:
            return (
                f"Error: 未找到 task_id='{task_id}'。"
                f"任务可能已完成并被清理，或 task_id 不正确。"
            )

        if task.done():
            pop(task_id)
            return f"任务 '{task_id}' 已经完成，无需取消。"

        task.cancel()
        pop(task_id)
        logger.info("async_task_cancelled id=%s", task_id)
        return f"✓ 任务 '{task_id}' 已取消。"
