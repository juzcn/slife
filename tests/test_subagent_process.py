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
        proc = SubagentProcess("test-sub", "config.json5")
        assert proc.name == "test-sub"
        assert proc.is_running is False
        assert proc.is_ready is False
        assert proc.pid is None

    def test_default_config_path(self):
        proc = SubagentProcess("worker", "/path/to/slife.json5")
        assert proc._config_path == "/path/to/slife.json5"


class TestSubagentProcessProperties:
    """Tests for SubagentProcess properties."""

    def test_pid_from_process(self):
        proc = SubagentProcess("test", "config.json5")
        mock_process = MagicMock()
        mock_process.pid = 12345
        proc._process = mock_process
        assert proc.pid == 12345

    def test_pid_none_without_process(self):
        proc = SubagentProcess("test", "config.json5")
        assert proc.pid is None

    def test_is_running_requires_process_and_running_flag(self):
        proc = SubagentProcess("test", "config.json5")
        # Neither running flag nor process set
        assert not proc.is_running

        # Flag set but no process
        proc._running = True
        assert not proc.is_running

        # Process set with returncode None and flag true
        mock_process = MagicMock()
        mock_process.returncode = None
        proc._process = mock_process
        assert proc.is_running

    def test_is_running_false_when_process_exited(self):
        proc = SubagentProcess("test", "config.json5")
        proc._running = True
        mock_process = MagicMock()
        mock_process.returncode = 0  # exited
        proc._process = mock_process
        assert not proc.is_running


# ── SubagentManager ─────────────────────────────────────────────────────────


class TestSubagentManagerInit:
    """Tests for SubagentManager initialization."""

    def test_initial_state(self, sample_config):
        manager = SubagentManager(sample_config)
        assert manager.count == 0
        assert manager._max == 5

    def test_custom_config_path(self, sample_config):
        sample_config._path = "/test/slife.json5"
        manager = SubagentManager(sample_config)
        assert manager._config_path == "/test/slife.json5"

    def test_default_config_path(self, sample_config):
        sample_config._path = None
        manager = SubagentManager(sample_config)
        from slife.paths import get_config_path
        assert manager._config_path == str(get_config_path())

    def test_custom_max_subagents(self, sample_config):
        sample_config.subagent_config = {"max_subagents": 3, "task_timeout": 60}
        manager = SubagentManager(sample_config)
        assert manager._max == 3
        assert manager._timeout == 60

    def test_no_subagent_config(self, sample_config):
        sample_config.subagent_config = None
        manager = SubagentManager(sample_config)
        assert manager._max == 5
        assert manager._timeout == 120


class TestSubagentManagerList:
    """Tests for SubagentManager.list."""

    def test_list_empty(self, sample_config):
        manager = SubagentManager(sample_config)
        assert manager.list() == []

    def test_list_only_running(self, sample_config):
        manager = SubagentManager(sample_config)
        mock_proc = MagicMock(spec=SubagentProcess)
        mock_proc.is_running = True
        mock_proc2 = MagicMock(spec=SubagentProcess)
        mock_proc2.is_running = False
        manager._subagents = {"sub-1": mock_proc, "sub-2": mock_proc2}
        assert manager.list() == ["sub-1"]


class TestSubagentManagerGet:
    """Tests for SubagentManager.get."""

    def test_get_existing(self, sample_config):
        manager = SubagentManager(sample_config)
        mock_proc = MagicMock()
        manager._subagents = {"sub-1": mock_proc}
        assert manager.get("sub-1") is mock_proc

    def test_get_missing(self, sample_config):
        manager = SubagentManager(sample_config)
        assert manager.get("nonexistent") is None


class TestSubagentManagerStop:
    """Tests for SubagentManager.stop."""

    @pytest.mark.asyncio
    async def test_stop_existing(self, sample_config):
        manager = SubagentManager(sample_config)
        mock_proc = MagicMock()
        mock_proc.stop = AsyncMock()
        manager._subagents = {"sub-1": mock_proc}

        result = await manager.stop("sub-1")
        assert result is True
        mock_proc.stop.assert_called_once()
        assert "sub-1" not in manager._subagents

    @pytest.mark.asyncio
    async def test_stop_missing(self, sample_config):
        manager = SubagentManager(sample_config)
        result = await manager.stop("nonexistent")
        assert result is False


class TestSubagentManagerStopAll:
    """Tests for SubagentManager.stop_all."""

    @pytest.mark.asyncio
    async def test_stop_all_empty(self, sample_config):
        manager = SubagentManager(sample_config)
        await manager.stop_all()  # Should not raise

    @pytest.mark.asyncio
    async def test_stop_all_stops_everything(self, sample_config):
        manager = SubagentManager(sample_config)
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
    async def test_send_task_success(self, sample_config):
        manager = SubagentManager(sample_config)
        mock_proc = MagicMock()
        mock_proc.send_task = AsyncMock(return_value="task result")
        mock_proc.is_running = True
        manager._subagents = {"sub-1": mock_proc}

        result = await manager.send_task("sub-1", "do something")
        assert result == "task result"
        mock_proc.send_task.assert_called_once_with("do something", 120)

    @pytest.mark.asyncio
    async def test_send_task_custom_timeout(self, sample_config):
        manager = SubagentManager(sample_config)
        mock_proc = MagicMock()
        mock_proc.send_task = AsyncMock(return_value="done")
        mock_proc.is_running = True
        manager._subagents = {"sub-1": mock_proc}

        result = await manager.send_task("sub-1", "do", timeout=30)
        mock_proc.send_task.assert_called_once_with("do", 30)

    @pytest.mark.asyncio
    async def test_send_task_unknown_agent(self, sample_config):
        manager = SubagentManager(sample_config)
        with pytest.raises(ValueError, match="not found"):
            await manager.send_task("ghost", "task")


class TestSubagentManagerSendTaskAsync:
    """Tests for SubagentManager.send_task_async."""

    @pytest.mark.asyncio
    async def test_send_task_async_success(self, sample_config):
        manager = SubagentManager(sample_config)
        mock_proc = MagicMock()
        mock_proc.send_task_async = AsyncMock(return_value="abc123")
        mock_proc.is_running = True
        manager._subagents = {"sub-1": mock_proc}

        rpc_id = await manager.send_task_async("sub-1", "do async")
        assert rpc_id == "abc123"

    @pytest.mark.asyncio
    async def test_send_task_async_unknown_agent(self, sample_config):
        manager = SubagentManager(sample_config)
        with pytest.raises(ValueError, match="not found"):
            await manager.send_task_async("ghost", "task")


class TestSubagentManagerGetTaskResult:
    """Tests for SubagentManager.get_task_result."""

    def test_result_from_proc(self, sample_config):
        manager = SubagentManager(sample_config)
        mock_proc = MagicMock()
        mock_proc.get_task_result = MagicMock(return_value="result!")
        manager._subagents = {"sub-1": mock_proc}

        result = manager.get_task_result("sub-1", "task-1")
        assert result == "result!"

    def test_unknown_agent_returns_none(self, sample_config):
        manager = SubagentManager(sample_config)
        result = manager.get_task_result("ghost", "task-1")
        assert result is None
