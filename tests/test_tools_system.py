"""Tests for slife.tools.system — SystemToolsListTool, CheckAsyncTool, CancelAsyncTool."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
from unittest.mock import MagicMock, patch

import pytest

from slife.tools.system import (
    SystemToolsListTool,
    CheckAsyncTool,
    CancelAsyncTool,
    SetMaxIterationsTool,
    _system_category,
    _strip_server_prefix,
    _tasks,
    schedule,
    _get_task,
    _pop_task,
)


# ── _system_category ──────────────────────────────────────────────────────


class TestSystemCategory:
    """Tests for the source-based category helper (T-12)."""

    def test_builtin_tool_uses_its_own_category(self):
        from slife.tools.base import Tool

        class _T(Tool):
            name = "thing"
            description = "Does a thing."
            parameters = {"type": "object", "properties": {}}
            category = "Custom"
            async def execute(self, **kwargs): return "ok"

        assert _system_category(_T()) == "Custom"

    def test_plugin_tool_groups_by_plugin_name(self):
        from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute

        tool = MagicMock(spec=MCPProxyTool)
        tool._server = "wechat"
        tool._route = ProxyRoute.DIRECT
        assert _system_category(tool) == "wechat"

    def test_plugin_tool_without_server_falls_back_to_plugins(self):
        from slife.mcp.tool_adapter import MCPProxyTool

        tool = MagicMock(spec=MCPProxyTool)
        del tool._server
        assert _system_category(tool) == "Plugins"

    def test_unknown_builtin_tool_defaults_to_other(self):
        from slife.tools.base import Tool

        class _T(Tool):
            name = "thing"
            description = "Does a thing."
            parameters = {"type": "object", "properties": {}}
            category = ""
            async def execute(self, **kwargs): return "ok"

        assert _system_category(_T()) == "Other"


# ── _strip_server_prefix ───────────────────────────────────────────────


class TestStripServerPrefix:
    """Plugin proxy tools stamp `[<server>] ` on their description; the
    group heading already carries the plugin name, so system_tools_list
    strips it — tools are bare names, no prefix."""

    def _proxy(self, server: str, desc: str):
        from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute
        tool = MagicMock(spec=MCPProxyTool)
        tool._server = server
        tool._route = ProxyRoute.DIRECT
        tool.description = f"[{server}] {desc}"
        return tool

    def test_strips_matching_server_prefix(self):
        tool = self._proxy("memdb", "Search turns.")
        assert _strip_server_prefix(tool, "[memdb] Search turns.") == "Search turns."

    def test_non_proxy_desc_untouched(self):
        from slife.tools.base import Tool

        class _Native(Tool):
            name = "thing"
            description = "Does a thing."
            parameters = {"type": "object", "properties": {}}
            async def execute(self, **kwargs): return "ok"

        assert _strip_server_prefix(_Native(), "Native desc.") == "Native desc."

    def test_prefix_mismatch_untouched(self):
        tool = self._proxy("memdb", "Search turns.")
        assert _strip_server_prefix(tool, "[other] Search turns.") == "[other] Search turns."


# ── SystemToolsListTool ──────────────────────────────────────────────────


class TestSystemToolsListTool:
    """Tests for SystemToolsListTool."""

    def test_metadata(self):
        tool = SystemToolsListTool()
        assert tool.name == "system_tools_list"
        assert tool.category == "System"
        assert tool.parameters == {
            "type": "object", "properties": {}, "required": [],
            "additionalProperties": False,
        }

    @pytest.mark.asyncio
    async def test_registry_unavailable(self):
        """When registry is None, returns clear message."""
        tool = SystemToolsListTool()
        try:
            result = await tool.execute()
            assert "not available" in result.lower()
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_registry_empty(self):
        """When registry has no tools, returns appropriate message."""
        from slife.tools.registry import ToolRegistry
        tool = SystemToolsListTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(registry=ToolRegistry())
            result = await tool.execute()
            assert "no tools" in result.lower()
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_builtin_tools_listed(self):
        """Native tools are listed under their own category."""
        from slife.tools.registry import ToolRegistry
        from slife.tools.base import Tool

        class _TestNative(Tool):
            name = "check_something"
            description = "Checks something."
            parameters = {"type": "object", "properties": {}}
            category = "System"
            async def execute(self, **kwargs): return "ok"

        registry = ToolRegistry()
        registry.register(_TestNative())

        tool = SystemToolsListTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(registry=registry)
            result = await tool.execute()
            assert "check_something" in result
            assert "System" in result  # the tool's own category
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_mcp_proxy_tools_excluded(self):
        """External MCP proxy tools are NOT listed — the model already gets
        their full schemas natively, so a second listing is redundant cost."""
        from slife.tools.registry import ToolRegistry
        from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute
        from slife.tools.base import Tool

        class _Native(Tool):
            name = "echo"
            description = "Echo back."
            parameters = {"type": "object", "properties": {}}
            async def execute(self, **kwargs): return "ok"

        # Minimal MCPProxyTool-like mock so isinstance checks pass.
        # _route is an instance attribute (set in __init__), so spec= alone
        # does not expose it — set it explicitly to model an EXTERNAL proxy.
        mock_tool = MagicMock(spec=MCPProxyTool)
        mock_tool.name = "filesystem__read_file"
        mock_tool._server = "filesystem"
        mock_tool._route = ProxyRoute.EXTERNAL
        mock_tool.description = "Read a file."
        mock_tool.category = "MCP"
        mock_tool.parameters = {"type": "object", "properties": {}}

        registry = ToolRegistry()
        registry.register(mock_tool)
        registry.register(_Native())

        tool = SystemToolsListTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(registry=registry)
            result = await tool.execute()
            assert "echo" in result
            assert "filesystem__read_file" not in result
            assert "filesystem" not in result.lower()
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_builtin_plugin_tools_grouped_by_plugin_name(self):
        """Built-in plugin tools (bare names) group under their plugin name —
        source-based, no name-prefix guessing (T-12)."""
        from slife.tools.registry import ToolRegistry
        from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute

        plugin_tool = MagicMock(spec=MCPProxyTool)
        plugin_tool.name = "wechat_login"
        plugin_tool._server = "wechat"
        plugin_tool._route = ProxyRoute.DIRECT
        plugin_tool.description = "Log in to WeChat."
        plugin_tool.parameters = {"type": "object", "properties": {}}

        registry = ToolRegistry()
        registry.register(plugin_tool)

        tool = SystemToolsListTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(registry=registry)
            result = await tool.execute()
            assert "`wechat_login`" in result
            assert "wechat" in result  # grouped under the plugin name
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_job_tools_are_not_system_tools(self):
        """A job (``job-<function>``) is the USER's tool, not the system's:
        it is inventoried by ``job-list`` and searchable in the catalog, so it
        stays out of this listing — while the job-coding plugin's own tools
        (job-write …) are system tools and stay in."""
        from slife.tools.registry import ToolRegistry
        from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute
        from slife.tools.context import ToolContext

        def _job_tool(name):
            tool = MagicMock(spec=MCPProxyTool)
            tool.name = name
            tool._server = "job-coding"
            tool._route = ProxyRoute.DIRECT
            tool.description = "A job."
            tool.parameters = {"type": "object", "properties": {}}
            return tool

        registry = ToolRegistry()
        registry.register(_job_tool("job-translate"))     # the user's
        registry.register(_job_tool("job-write"))         # the plugin's own
        registry.register(_job_tool("turn_search"))       # another plugin's

        tool = SystemToolsListTool()
        try:
            tool._ctx = ToolContext(registry=registry)
            result = await tool.execute()
            assert "`job-translate`" not in result
            assert "`job-write`" in result
            assert "`turn_search`" in result
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_harness_marker_for_underscore_tools(self):
        """Native harness tools (_turn_prompt) are shown with a marker."""
        from slife.tools.registry import ToolRegistry
        from slife.tools.base import Tool

        class _Harness(Tool):
            name = "_turn_prompt"
            description = "Per-turn prompt."
            parameters = {"type": "object", "properties": {}}
            async def execute(self, **kwargs): return "ok"

        registry = ToolRegistry()
        registry.register(_Harness())

        tool = SystemToolsListTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(registry=registry)
            result = await tool.execute()
            assert "`_turn_prompt`" in result
            assert "harness, auto-invoked" in result
        finally:
            tool._ctx = None


# ── Async task helpers ────────────────────────────────────────────────────


class TestAsyncTaskHelpers:
    """Tests for schedule, _get_task, _pop_task."""

    def setup_method(self):
        """Clear the global task dict before each test."""
        _tasks.clear()

    def teardown_method(self):
        """Clear after each test."""
        _tasks.clear()

    @pytest.mark.asyncio
    async def test_schedule_returns_task_id(self):
        """schedule() returns an 8-char hex task_id."""
        async def dummy():
            return "done"
        tid = schedule(dummy())
        assert len(tid) == 8
        assert all(c in "0123456789abcdef" for c in tid)

    @pytest.mark.asyncio
    async def test_schedule_adds_to_tasks(self):
        """schedule() adds the task to the global _tasks dict."""
        async def dummy():
            return "done"
        tid = schedule(dummy())
        assert tid in _tasks
        assert isinstance(_tasks[tid], asyncio.Task)

    @pytest.mark.asyncio
    async def test_get_task_returns_task(self):
        """_get_task retrieves a scheduled task."""
        async def dummy():
            return "done"
        tid = schedule(dummy())
        task = _get_task(tid)
        assert task is not None
        assert isinstance(task, asyncio.Task)

    def test_get_task_returns_none_for_missing(self):
        """_get_task returns None for unknown task_id."""
        assert _get_task("nonexistent") is None

    @pytest.mark.asyncio
    async def test_pop_task_removes_and_returns(self):
        """_pop_task removes the task from _tasks and returns it."""
        async def dummy():
            return "done"
        tid = schedule(dummy())
        assert tid in _tasks
        task = _pop_task(tid)
        assert task is not None
        assert tid not in _tasks

    def test_pop_task_returns_none_for_missing(self):
        """_pop_task returns None for unknown task_id."""
        assert _pop_task("nonexistent") is None

    @pytest.mark.asyncio
    async def test_scheduled_task_completes(self):
        """A scheduled task eventually completes with its result."""
        async def return_value():
            return "the result"
        tid = schedule(return_value())
        task = _get_task(tid)
        assert task is not None
        result = await task
        assert result == "the result"

    @pytest.mark.asyncio
    async def test_scheduled_task_captures_exception(self):
        """A failing task captures the exception in its result string."""
        async def raise_error():
            raise ValueError("test error")
        tid = schedule(raise_error())
        task = _get_task(tid)
        assert task is not None
        result = await task
        assert "ValueError" in result
        assert "test error" in result

    @pytest.mark.asyncio
    async def test_multiple_scheduled_tasks(self):
        """Multiple tasks can be scheduled simultaneously."""
        async def return_x(x):
            return x
        tid1 = schedule(return_x("one"))
        tid2 = schedule(return_x("two"))
        tid3 = schedule(return_x("three"))
        assert len(_tasks) == 3
        assert tid1 != tid2 != tid3


# ── CheckAsyncTool ────────────────────────────────────────────────────────


class TestCheckAsyncTool:
    """Tests for CheckAsyncTool."""

    def setup_method(self):
        _tasks.clear()

    def teardown_method(self):
        _tasks.clear()

    def test_metadata(self):
        tool = CheckAsyncTool()
        assert tool.name == "check_async"
        assert tool.category == "System"
        assert "task_id" in tool.parameters["required"]

    @pytest.mark.asyncio
    async def test_task_not_found(self):
        """Non-existent task_id returns error message."""
        tool = CheckAsyncTool()
        result = await tool.execute(task_id="nonexistent")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_task_still_running(self):
        """A running task returns status message."""
        tool = CheckAsyncTool()

        async def slow_task():
            await asyncio.sleep(10)

        tid = schedule(slow_task())
        assert tid in _tasks

        result = await tool.execute(task_id=tid)
        assert "still running" in result

        # Clean up
        _tasks[tid].cancel()
        try:
            await _tasks[tid]
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_task_completed(self):
        """A completed task returns its result and is removed from _tasks."""
        tool = CheckAsyncTool()

        async def quick_task():
            return "all done!"

        tid = schedule(quick_task())
        # Wait for completion
        await asyncio.sleep(0.1)

        result = await tool.execute(task_id=tid)
        assert "Task completed" in result
        assert "all done!" in result
        assert tid not in _tasks  # popped after retrieval

    @pytest.mark.asyncio
    async def test_task_completed_with_error(self):
        """A failed task surfaces as an error (not a success banner) so the
        loop's is_error detection and the TUI's red render both work."""
        tool = CheckAsyncTool()

        async def failing():
            raise RuntimeError("boom")

        tid = schedule(failing())
        await asyncio.sleep(0.1)

        result = await tool.execute(task_id=tid)
        assert result.startswith("Error")
        assert "boom" in result
        assert tid not in _tasks


# ── CancelAsyncTool ───────────────────────────────────────────────────────


class TestCancelAsyncTool:
    """Tests for CancelAsyncTool."""

    def setup_method(self):
        _tasks.clear()

    def teardown_method(self):
        _tasks.clear()

    def test_metadata(self):
        tool = CancelAsyncTool()
        assert tool.name == "cancel_async"
        assert tool.category == "System"

    @pytest.mark.asyncio
    async def test_task_not_found(self):
        """Cancelling a non-existent task returns error."""
        tool = CancelAsyncTool()
        result = await tool.execute(task_id="nonexistent")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_task_already_done(self):
        """Cancelling a finished task returns appropriate message."""
        tool = CancelAsyncTool()

        async def quick():
            return "done"

        tid = schedule(quick())
        await asyncio.sleep(0.1)  # let it complete

        result = await tool.execute(task_id=tid)
        assert "already completed" in result
        assert tid not in _tasks

    @pytest.mark.asyncio
    async def test_cancel_running_task(self):
        """A running task can be cancelled."""
        tool = CancelAsyncTool()

        async def slow():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise

        tid = schedule(slow())
        assert tid in _tasks

        result = await tool.execute(task_id=tid)
        assert "cancelled" in result
        assert tid not in _tasks


# ── SetMaxIterationsTool ─────────────────────────────────────────────


class TestSetMaxIterationsTool:
    """set_max_iterations delegates to the ctx hook."""

    def test_parameters_schema(self):
        assert "max_iterations" in SetMaxIterationsTool.parameters["required"]
        prop = SetMaxIterationsTool.parameters["properties"]["max_iterations"]
        assert prop["type"] == "integer"

    @pytest.mark.asyncio
    async def test_sets_via_ctx_hook(self):
        from slife.tools.context import ToolContext

        tool = SetMaxIterationsTool()
        tool._ctx = ToolContext(
            set_max_iterations=lambda n: f"Max iterations set to {n}",
        )

        result = await tool.execute(max_iterations=0)

        assert result == "Max iterations set to 0"

    @pytest.mark.asyncio
    async def test_loop_unavailable(self):
        tool = SetMaxIterationsTool()  # no _ctx → no hook
        result = await tool.execute(max_iterations=0)
        assert result.startswith("Error")
