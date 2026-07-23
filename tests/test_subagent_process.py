"""Tests for Slife.subagent.process — SubagentManager and SubagentProcess."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

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
    cfg = MagicMock()
    cfg.subagent_config = {"max_subagents": 5, "task_timeout": 120}
    cfg._path = None
    cfg.to_dict = MagicMock(return_value={
        "models": [], "active_model_ref": "", "tools": [],
        "max_iterations": 30, "agent_id": "slife",
        "mcp_config": None, "memory_config": None,
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
        manager = MagicMock(spec=SubagentManager)
        set_manager(manager)
        assert get_manager() is manager

    def test_clear_manager(self):
        manager = MagicMock()
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
        mock_process = MagicMock()
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

        mock_process = MagicMock()
        mock_process.returncode = None
        proc._process = mock_process
        assert proc.is_running

    def test_is_running_false_when_process_exited(self):
        cfg = _mock_config()
        proc = SubagentProcess("test", cfg)
        proc._running = True
        mock_process = MagicMock()
        mock_process.returncode = 0  # exited
        proc._process = mock_process
        assert not proc.is_running


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
        mock_proc = MagicMock(spec=SubagentProcess)
        mock_proc.is_running = True
        mock_proc2 = MagicMock(spec=SubagentProcess)
        mock_proc2.is_running = False
        manager._subagents = {"sub-1": mock_proc, "sub-2": mock_proc2}
        assert manager.list() == ["sub-1"]


class TestSubagentManagerGet:
    """Tests for SubagentManager.get."""

    def test_get_existing(self):
        manager = SubagentManager(_mock_config())
        mock_proc = MagicMock()
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
        mock_proc = MagicMock()
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
        p1 = MagicMock(); p1.stop = AsyncMock()
        p2 = MagicMock(); p2.stop = AsyncMock()
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
        mock_proc = MagicMock()
        mock_proc.send_task = AsyncMock(return_value="task result")
        mock_proc.is_running = True
        manager._subagents = {"sub-1": mock_proc}

        result = await manager.send_task("sub-1", "do something")
        assert result == "task result"
        mock_proc.send_task.assert_called_once_with("do something", 120)

    @pytest.mark.asyncio
    async def test_send_task_custom_timeout(self):
        manager = SubagentManager(_mock_config())
        mock_proc = MagicMock()
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
        mock_proc = MagicMock()
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
        mock_proc = MagicMock()
        mock_proc.get_task_result = MagicMock(return_value="done")
        mock_proc.is_running = True
        manager._subagents = {"sub-1": mock_proc}

        assert manager.get_task_result("sub-1", "rpc-1") == "done"

    def test_unknown_agent_returns_none(self):
        manager = SubagentManager(_mock_config())
        assert manager.get_task_result("ghost", "rpc-1") is None
