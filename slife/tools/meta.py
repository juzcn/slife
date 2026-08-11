"""Meta tools — agent self-management.

list_tools      — inventory with category filter
check_async     — poll background task result
cancel_async    — cancel a running background task
clear_context   — reset conversation history
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from typing import ClassVar

from slife.tools.base import Tool

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# list_tools
# ═══════════════════════════════════════════════════════════════════════

_PLUGIN_LABELS: dict[str, str] = {
    "memdb": "MemDB (built-in plugin)",
    "wechat": "WeChat (built-in plugin)",
}


def _classify(name: str) -> str:
    if name.startswith("a2a_"):
        return "Agent Communication (A2A)"
    if name.startswith("subagent_"):
        return "Subagent (local workers)"
    if name.startswith("cli_"):
        return "CLI"
    if name.startswith("rest_api_"):
        return "REST API"
    if name.startswith("model_") or name == "switch_to_nvidia_free":
        return "Models"
    if name.startswith("config_env") or name == "native_tool_set":
        return "Config"
    if name.startswith("skill_"):
        return "Skills"
    if name.startswith("check_") or name == "system_health":
        return "System"
    if name.startswith("execute_") or name.startswith("install_") or name.startswith("run_"):
        return "Execution"
    if name.startswith("credential_"):
        return "Credentials"
    if name in ("list_tools", "check_async", "cancel_async", "clear_context"):
        return "Meta"
    return "Other"


class ListToolsTool(Tool):
    name: ClassVar[str] = "list_tools"
    category: ClassVar[str] = "Meta"
    description: ClassVar[str] = "List available tools. category: all (default), native, or mcp."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["all", "native", "mcp"],
                "description": "all (default) / native / mcp.",
            },
        },
        "required": [],
    }

    async def execute(self, category: str = "all", **kwargs) -> str:
        from slife.mcp.tool_adapter import MCPProxyTool

        ctx = getattr(self, "_ctx", None)
        registry = ctx.registry if ctx is not None else None
        if registry is None:
            return "Tool registry is not available (called before initialization)."

        all_tools = registry.list_tools()
        if not all_tools:
            return "No tools are currently registered."

        natives: list[Tool] = []
        mcp_proxies: dict[str, list[Tool]] = defaultdict(list)
        for t in all_tools:
            if isinstance(t, MCPProxyTool):
                mcp_proxies[t._server].append(t)
            else:
                natives.append(t)

        show_native = category in ("all", "native")
        show_mcp = category in ("all", "mcp")
        lines: list[str] = []

        if show_native:
            lines.append(f"## Native Tools ({len(natives)} total)\n")
            native_groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
            for t in sorted(natives, key=lambda t: t.name):
                cat = getattr(t, "category", "") or _classify(t.name)
                desc = t.description.split(".")[0].strip() + "."
                native_groups[cat].append((t.name, desc))

            for cat in sorted(native_groups):
                items = native_groups[cat]
                lines.append(f"### {cat} ({len(items)})")
                for name, desc in items:
                    marker = " — harness, auto-invoked" if name.startswith("_") else ""
                    lines.append(f"- **`{name}`**{marker} — {desc}")
                lines.append("")

        if show_mcp and mcp_proxies:
            lines.append(f"## MCP-Connected Servers ({len(mcp_proxies)} servers)\n")
            for server in sorted(mcp_proxies):
                tools = mcp_proxies[server]
                label = _PLUGIN_LABELS.get(server, f"MCP: {server}")
                tool_names = sorted(t.name for t in tools)
                lines.append(f"- **{label}** ({len(tools)} tools): "
                             + ", ".join(f"`{n}`" for n in tool_names))
            lines.append("")
        elif show_mcp and not mcp_proxies:
            lines.append("## MCP-Connected Servers\n\nNo MCP servers connected.\n")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# Async tasks
# ═══════════════════════════════════════════════════════════════════════

_tasks: dict[str, asyncio.Task] = {}

#: Bound on ``_tasks`` — an ``_async: true`` call the LLM never polls would
#: otherwise keep its finished task in the dict (holding tool resources) for
#: the whole session (REVIEW §1-13).
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
    description = "Clear conversation history, keeping only the system prompt. Use when context is polluted."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        conv = getattr(self, "_ctx", None)
        if conv is not None:
            conv = conv.conversation
        if conv is None:
            return "Conversation is not yet initialised. This tool must be called after the agent service has started."
        removed = conv.clear_history()
        if removed == 0:
            return "Context is already clean — no old turns to remove."
        remaining = len(conv.messages)
        logger.info("clear_context removed=%d remaining=%d", removed, remaining)
        return f"[OK] Cleared {removed} old message(s); {remaining} remaining (system prompt + current turn)."



