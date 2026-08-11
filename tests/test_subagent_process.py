"""Tests for Slife.subagent.process — SubagentManager and SubagentProcess."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch, PropertyMock

import pytest

from slife.subagent.process import (
    SubagentProcess,
    SubagentManager,
    get_manager,
    set_manager,
    clear_manager,
    _current_manager,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _mock_config(**overrides):
    """Build a minimal mock Config for SubagentProcess / SubagentManager tests."""
    cfg = Mock()
    cfg.subagent_config = {"max_subagents": 5, "task_timeout": 120}
    cfg._path = None
    cfg.to_dict = Mock(return_value={
        "models": [], "active_model_ref": "", "tools": [],
        "max_iterations": 30, "agent_id": "slife",
        "mcp_config": None, "memdb_config": None,
        "wechat_config": None, "a2a_config": None,
        "subagent_config": {"max_subagents": 5, "task_timeout": 120},
    })
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# ── Module-level manager refs ───────────────────────────────────────────────


class TestModuleLevelRefs:
    """Tests for get_manager / set_manager / clear_manager."""

    def setup_method(self):
        clear_manager()

    def teardown_method(self):
        clear_manager()

    def test_get_manager_none_by_default(self):
        assert get_manager() is None

    def test_set_and_get_manager(self):
        manager = Mock(spec=SubagentManager)
        set_manager(manager)
        assert get_manager() is manager

    def test_clear_manager(self):
        manager = Mock()
        set_manager(manager)
        clear_manager()
        assert get_manager() is None


# ── SubagentProcess ─────────────────────────────────────────────────────────


class TestSubagentProcessInit:
    """Tests for SubagentProcess initialization."""

    def test_initial_state(self):
        cfg = _mock_config()
        proc = SubagentProcess("test-sub", cfg)
        assert proc.name == "test-sub"
        assert proc.is_running is False
        assert proc.is_ready is False
        assert proc.pid is None

    def test_stores_config_json(self):
        cfg = _mock_config()
        proc = SubagentProcess("worker", cfg)
        parsed = json.loads(proc._config_json)
        assert parsed["agent_id"] == "slife"
        assert parsed["max_iterations"] == 30


class TestSubagentProcessProperties:
    """Tests for SubagentProcess properties."""

    def test_pid_from_process(self):
        cfg = _mock_config()
        proc = SubagentProcess("test", cfg)
        mock_process = Mock()
        mock_process.pid = 12345
        proc._process = mock_process
        assert proc.pid == 12345

    def test_pid_none_without_process(self):
        cfg = _mock_config()
        proc = SubagentProcess("test", cfg)
        assert proc.pid is None

    def test_is_running_requires_process_and_running_flag(self):
        cfg = _mock_config()
        proc = SubagentProcess("test", cfg)
        assert not proc.is_running

        proc._running = True
        assert not proc.is_running

        mock_process = Mock()
        mock_process.returncode = None
        proc._process = mock_process
        assert proc.is_running

    def test_is_running_false_when_process_exited(self):
        cfg = _mock_config()
        proc = SubagentProcess("test", cfg)
        proc._running = True
        mock_process = Mock()
        mock_process.returncode = 0  # exited
        proc._process = mock_process
        assert not proc.is_running


class TestSubagentProcessCancelTask:
    """Tests for SubagentProcess.cancel_task (best-effort cancellation)."""

    def _proc(self, **overrides):
        cfg = _mock_config()
        return SubagentProcess("test", cfg)

    @pytest.mark.asyncio
    async def test_cancel_unknown_task(self):
        proc = self._proc()
        assert await proc.cancel_task("does-not-exist") is False

    @pytest.mark.asyncio
    async def test_cancel_pending_async_task(self):
        proc = self._proc()
        proc._record_send("rpc-1", "do X", mode="async")
        proc._inflight = 1
        proc._async_results["rpc-1"] = "old"

        assert await proc.cancel_task("rpc-1") is True
        assert proc._task_records["rpc-1"]["status"] == "cancelled"
        assert proc._inflight == 0
        assert "rpc-1" in proc._cancelled
        # Async result dropped.
        assert "rpc-1" not in proc._async_results

    @pytest.mark.asyncio
    async def test_cancel_already_completed(self):
        proc = self._proc()
        proc._record_send("rpc-1", "do X", mode="async")
        proc._record_update("rpc-1", "completed", "done")

        assert await proc.cancel_task("rpc-1") is False
        assert proc._task_records["rpc-1"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_cancel_sync_waiter_gets_exception(self):
        proc = self._proc()
        proc._record_send("rpc-1", "do X", mode="sync")
        fut = asyncio.get_event_loop().create_future()
        proc._pending["rpc-1"] = fut

        assert await proc.cancel_task("rpc-1") is True
        assert fut.done()
        with pytest.raises(RuntimeError):
            fut.result()


# ── _dispatch_message / _read_stdout (REVIEW C4) ─────────────────────────


class TestSubagentProcessReadStdout:
    """_dispatch_message routing + _read_stdout resilience.

    A malformed message must not kill the reader (it would strand the
    _pending sync futures until send_task's timeout), and when the reader
    ends, leftover waiters must be resolved so send_task fails fast.
    """

    def _proc(self):
        return SubagentProcess("test", _mock_config())

    @pytest.mark.asyncio
    async def test_dispatch_resolves_sync_waiter(self):
        proc = self._proc()
        proc._record_send("rpc-1", "do X", mode="sync")
        fut = asyncio.get_event_loop().create_future()
        proc._pending["rpc-1"] = fut
        proc._inflight = 1

        proc._dispatch_message({"id": "rpc-1", "result": "the answer"})

        assert fut.done()
        assert fut.result() == "the answer"
        assert proc._inflight == 0
        assert proc._task_records["rpc-1"]["status"] == "completed"
        assert "rpc-1" not in proc._pending

    @pytest.mark.asyncio
    async def test_dispatch_stores_async_result(self):
        proc = self._proc()
        proc._record_send("rpc-1", "do X", mode="async")
        proc._inflight = 1

        proc._dispatch_message({"id": "rpc-1", "result": "done"})

        assert proc._async_results["rpc-1"] == "done"
        assert proc._task_records["rpc-1"]["status"] == "completed"
        assert proc._inflight == 0

    @pytest.mark.asyncio
    async def test_dispatch_ignores_late_result_for_cancelled_task(self):
        proc = self._proc()
        proc._record_send("rpc-1", "do X", mode="async")
        proc._cancelled.add("rpc-1")

        proc._dispatch_message({"id": "rpc-1", "result": "late"})

        assert "rpc-1" not in proc._async_results
        assert "rpc-1" not in proc._pending
        assert proc._cancelled == set()  # late result consumed the cancel marker

    @pytest.mark.asyncio
    async def test_dispatch_tolerates_malformed_notification(self):
        """A non-dict result/params must not raise (REVIEW C4)."""
        proc = self._proc()
        proc._dispatch_message({
            "jsonrpc": "2.0", "result": "ready",
            "params": "x", "method": "worker/complete",
        })
        assert proc._ready.is_set() is False  # string result is not the ready signal

    @pytest.mark.asyncio
    async def test_read_stdout_resolves_leftover_pending_on_eof(self):
        proc = self._proc()
        proc._running = True
        proc._process = MagicMock()
        proc._process.stdout = MagicMock()

        async def _eof():
            return b""

        proc._process.stdout.readline = _eof

        fut = asyncio.get_event_loop().create_future()
        proc._pending["rpc-1"] = fut

        await proc._read_stdout()

        assert fut.done()
        with pytest.raises(RuntimeError, match="closed before"):
            fut.result()
        assert proc._pending == {}


# ── SubagentManager ─────────────────────────────────────────────────────────


class TestSubagentManagerInit:
    """Tests for SubagentManager initialization."""

    def test_initial_state(self):
        cfg = _mock_config()
        manager = SubagentManager(cfg)
        assert manager.count == 0
        assert manager._max == 5

    def test_stores_config(self):
        cfg = _mock_config()
        manager = SubagentManager(cfg)
        assert manager._config is cfg

    def test_custom_max_subagents(self):
        cfg = _mock_config(subagent_config={"max_subagents": 3, "task_timeout": 60})
        manager = SubagentManager(cfg)
        assert manager._max == 3
        assert manager._timeout == 60

    def test_defaults_from_config(self):
        cfg = _mock_config()
        manager = SubagentManager(cfg)
        assert manager._max == 5
        assert manager._timeout == 120


class TestSubagentManagerList:
    """Tests for SubagentManager.list."""

    def test_list_empty(self):
        manager = SubagentManager(_mock_config())
        assert manager.list() == []

    def test_list_only_running(self):
        manager = SubagentManager(_mock_config())
        mock_proc = Mock(spec=SubagentProcess)
        mock_proc.is_running = True
        mock_proc2 = Mock(spec=SubagentProcess)
        mock_proc2.is_running = False
        manager._subagents = {"sub-1": mock_proc, "sub-2": mock_proc2}
        assert manager.list() == ["sub-1"]


class TestSubagentManagerGet:
    """Tests for SubagentManager.get."""

    def test_get_existing(self):
        manager = SubagentManager(_mock_config())
        mock_proc = Mock()
        manager._subagents = {"sub-1": mock_proc}
        assert manager.get("sub-1") is mock_proc

    def test_get_missing(self):
        manager = SubagentManager(_mock_config())
        assert manager.get("nonexistent") is None


class TestSubagentManagerStop:
    """Tests for SubagentManager.stop."""

    @pytest.mark.asyncio
    async def test_stop_existing(self):
        manager = SubagentManager(_mock_config())
        mock_proc = Mock()
        mock_proc.stop = AsyncMock()
        manager._subagents = {"sub-1": mock_proc}

        result = await manager.stop("sub-1")
        assert result is True
        mock_proc.stop.assert_called_once()
        assert "sub-1" not in manager._subagents

    @pytest.mark.asyncio
    async def test_stop_missing(self):
        manager = SubagentManager(_mock_config())
        result = await manager.stop("nonexistent")
        assert result is False


class TestSubagentManagerStopAll:
    """Tests for SubagentManager.stop_all."""

    @pytest.mark.asyncio
    async def test_stop_all_empty(self):
        manager = SubagentManager(_mock_config())
        await manager.stop_all()  # Should not raise

    @pytest.mark.asyncio
    async def test_stop_all_stops_everything(self):
        manager = SubagentManager(_mock_config())
        p1 = Mock(); p1.stop = AsyncMock()
        p2 = Mock(); p2.stop = AsyncMock()
        manager._subagents = {"a": p1, "b": p2}

        await manager.stop_all()
        p1.stop.assert_called_once()
        p2.stop.assert_called_once()
        assert manager._subagents == {}


class TestSubagentManagerSendTask:
    """Tests for SubagentManager.send_task."""

    @pytest.mark.asyncio
    async def test_send_task_success(self):
        manager = SubagentManager(_mock_config())
        mock_proc = Mock()
        mock_proc.send_task = AsyncMock(return_value="task result")
        mock_proc.is_running = True
        manager._subagents = {"sub-1": mock_proc}

        result = await manager.send_task("sub-1", "do something")
        assert result == "task result"
        mock_proc.send_task.assert_called_once_with("do something", 120)

    @pytest.mark.asyncio
    async def test_send_task_custom_timeout(self):
        manager = SubagentManager(_mock_config())
        mock_proc = Mock()
        mock_proc.send_task = AsyncMock(return_value="ok")
        mock_proc.is_running = True
        manager._subagents = {"sub-1": mock_proc}

        await manager.send_task("sub-1", "task", timeout=60)
        mock_proc.send_task.assert_called_once_with("task", 60)

    @pytest.mark.asyncio
    async def test_send_task_unknown_agent(self):
        manager = SubagentManager(_mock_config())
        with pytest.raises(ValueError, match="not found"):
            await manager.send_task("ghost", "task")


class TestSubagentManagerSendTaskAsync:
    """Tests for SubagentManager.send_task_async."""

    @pytest.mark.asyncio
    async def test_send_task_async_success(self):
        manager = SubagentManager(_mock_config())
        mock_proc = Mock()
        mock_proc.send_task_async = AsyncMock(return_value="rpc-123")
        mock_proc.is_running = True
        manager._subagents = {"sub-1": mock_proc}

        rpc_id = await manager.send_task_async("sub-1", "async task")
        assert rpc_id == "rpc-123"

    @pytest.mark.asyncio
    async def test_send_task_async_unknown_agent(self):
        manager = SubagentManager(_mock_config())
        with pytest.raises(ValueError, match="not found"):
            await manager.send_task_async("ghost", "task")


class TestSubagentManagerGetTaskResult:
    """Tests for SubagentManager.get_task_result."""

    def test_result_from_proc(self):
        manager = SubagentManager(_mock_config())
        mock_proc = Mock()
        mock_proc.get_task_result = Mock(return_value="done")
        mock_proc.is_running = True
        manager._subagents = {"sub-1": mock_proc}

        assert manager.get_task_result("sub-1", "rpc-1") == "done"

    def test_unknown_agent_returns_none(self):
        manager = SubagentManager(_mock_config())
        assert manager.get_task_result("ghost", "rpc-1") is None
