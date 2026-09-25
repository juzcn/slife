"""Tests for Slife.subagent.process — SubagentManager and SubagentProcess."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from slife.subagent.process import (
    SubagentProcess,
    SubagentManager,
    TaskTimeout,
    get_manager,
    set_manager,
    clear_manager,
)
import slife.timeouts as _timeouts


# ── Helpers ────────────────────────────────────────────────────────────────


def _mock_config(**overrides):
    """Build a minimal mock Config for SubagentProcess / SubagentManager tests."""
    cfg = Mock()
    cfg.subagent_config = {"max_subagents": 5}
    cfg._path = None
    cfg.to_dict = Mock(return_value={
        "models": [], "active_model_ref": "", "tools": [],
        "max_iterations": 30, "agent_name": "slife",
        "mcp_config": None, "memdb_config": None,
        "wechat_config": None, "a2a_config": None,
        "subagent_config": {"max_subagents": 5},
    })
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _running_proc(name: str = "test") -> SubagentProcess:
    """A SubagentProcess that looks spawned, ready and writable.

    ``is_running`` needs a real ``returncode is None``, so a bare Mock process
    is not enough — the send paths check it before every write.
    """
    proc = SubagentProcess(name, _mock_config())
    proc._running = True
    proc._ready.set()
    proc._process = Mock()
    proc._process.returncode = None
    proc._process.stdin = Mock()
    proc._process.stdin.drain = AsyncMock()
    return proc


async def _spin_until(predicate, spins: int = 50) -> None:
    """Yield to the loop until *predicate* holds (bounded — never hang a test)."""
    for _ in range(spins):
        if predicate():
            return
        await asyncio.sleep(0)


async def _wait_for(predicate, timeout: float | None = None) -> bool:  # noqa-timeout
    """Wait real time for *predicate* — for conditions behind a timer."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (2.0 if timeout is None else timeout)  # noqa-timeout
    while True:
        if predicate():
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.005)  # noqa-timeout


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
        assert parsed["agent_name"] == "slife"
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
        proc._awaiting.add("rpc-1")
        proc._async_results["rpc-1"] = "old"

        assert await proc.cancel_task("rpc-1") is True
        assert proc._task_records["rpc-1"]["status"] == "cancelled"
        assert proc._awaiting == set()
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
        proc._awaiting.add("rpc-1")

        proc._dispatch_message({"id": "rpc-1", "result": "the answer"})

        assert fut.done()
        assert fut.result() == "the answer"
        assert proc._awaiting == set()
        assert proc._task_records["rpc-1"]["status"] == "completed"
        assert "rpc-1" not in proc._pending

    @pytest.mark.asyncio
    async def test_dispatch_stores_async_result(self):
        proc = self._proc()
        proc._record_send("rpc-1", "do X", mode="async")
        proc._awaiting.add("rpc-1")

        proc._dispatch_message({"id": "rpc-1", "result": "done"})

        assert proc._async_results["rpc-1"] == "done"
        assert proc._task_records["rpc-1"]["status"] == "completed"
        assert proc._awaiting == set()

    @pytest.mark.asyncio
    async def test_dispatch_auto_mode_notifies_manager(self):
        """mode='async' (auto-push) tasks notify the manager on completion."""
        proc = self._proc()
        manager = Mock(spec=SubagentManager)
        manager.on_task_complete = AsyncMock()
        set_manager(manager)
        try:
            proc._record_send("rpc-1", "do X", mode="async")
            proc._dispatch_message({"id": "rpc-1", "result": "done"})
            await asyncio.sleep(0)  # let the scheduled task run
            manager.on_task_complete.assert_awaited_once_with("test", "rpc-1", "done")
        finally:
            clear_manager()

    @pytest.mark.asyncio
    async def test_dispatch_auto_mode_keeps_result_pollable(self):
        """mode='auto' pushes AND keeps the result retrievable — a caller may
        poll a pushed result in the same turn (auto is not single-use)."""
        proc = self._proc()
        manager = Mock(spec=SubagentManager)
        manager.on_task_complete = AsyncMock()
        set_manager(manager)
        try:
            proc._record_send("rpc-1", "do X", mode="async")
            proc._dispatch_message({"id": "rpc-1", "result": "done"})
            await asyncio.sleep(0)
            manager.on_task_complete.assert_awaited_once_with("test", "rpc-1", "done")
            assert proc.get_task_result("rpc-1") == ("completed", "done")
        finally:
            clear_manager()

    @pytest.mark.asyncio
    async def test_dispatch_auto_push_survives_early_poll(self):
        """The auto-push carries the stored text even if a poll consumed the
        store entry first — it must not re-read _async_results (which pops)."""
        proc = self._proc()
        manager = Mock(spec=SubagentManager)
        manager.on_task_complete = AsyncMock()
        set_manager(manager)
        try:
            proc._record_send("rpc-1", "do X", mode="async")
            proc._dispatch_message({"id": "rpc-1", "result": "done"})
            proc.get_task_result("rpc-1")  # poll consumes the stored entry
            await asyncio.sleep(0)
            manager.on_task_complete.assert_awaited_once_with("test", "rpc-1", "done")
        finally:
            clear_manager()

    @pytest.mark.asyncio
    async def test_dispatch_poll_mode_skips_manager_notify(self):
        """mode='poll' tasks don't auto-push — the result stays retrievable
        via get_task_result instead."""
        proc = self._proc()
        manager = Mock(spec=SubagentManager)
        manager.on_task_complete = AsyncMock()
        set_manager(manager)
        try:
            proc._record_send("rpc-1", "do X", mode="async-poll")
            proc._dispatch_message({"id": "rpc-1", "result": "done"})
            await asyncio.sleep(0)
            manager.on_task_complete.assert_not_awaited()
            assert proc._async_results["rpc-1"] == "done"
            assert proc.get_task_result("rpc-1") == ("completed", "done")
        finally:
            clear_manager()

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

    @pytest.mark.asyncio
    async def test_read_stdout_survives_overlong_line(self):
        """A2 regression: one over-long stdout line must be discarded, not
        fatal.  Before the fix the ValueError (LimitOverrunError) fell into the
        generic handler, killed the reader, and every later task on this worker
        hung until send_task's own timeout."""
        proc = self._proc()
        proc._running = True
        proc._record_send("rpc-1", "do X", mode="sync")
        fut = asyncio.get_event_loop().create_future()
        proc._pending["rpc-1"] = fut
        proc._awaiting.add("rpc-1")
        proc._process = MagicMock()

        # readline sequence: [overlong-line head -> ValueError, its tail, a
        # valid JSON-RPC response, EOF].
        seq = [
            ValueError("LimitOverrunError"),
            b"T" * 200 + b"\n",
            b'{"jsonrpc":"2.0","id":"rpc-1","result":"after-overlong"}\n',
            b"",
        ]

        async def _readline():
            item = seq.pop(0) if seq else b""
            if isinstance(item, Exception):
                raise item
            return item

        proc._process.stdout = MagicMock()
        proc._process.stdout._limit = 1024
        proc._process.stdout.readline = _readline

        await proc._read_stdout()

        assert fut.done()
        assert fut.result() == "after-overlong"
        assert proc._pending == {}
        assert proc._task_records["rpc-1"]["status"] == "completed"


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
        cfg = _mock_config(subagent_config={"max_subagents": 3})
        manager = SubagentManager(cfg)
        assert manager._max == 3
        assert not hasattr(manager, "_timeout")

    def test_defaults_from_config(self):
        cfg = _mock_config()
        manager = SubagentManager(cfg)
        assert manager._max == 5
        assert not hasattr(manager, "_timeout")


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
        # None passes through — the worker resolves the registry default
        # (work.task_budget) at call time.
        mock_proc.send_task.assert_called_once_with("do something", None)

    @pytest.mark.asyncio
    async def test_send_task_custom_timeout(self):
        manager = SubagentManager(_mock_config())
        mock_proc = Mock()
        mock_proc.send_task = AsyncMock(return_value="ok")
        mock_proc.is_running = True
        manager._subagents = {"sub-1": mock_proc}

        await manager.send_task("sub-1", "task", timeout=60)  # noqa-timeout
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
        mock_proc.get_task_result = Mock(return_value=("completed", "done"))
        mock_proc.is_running = True
        manager._subagents = {"sub-1": mock_proc}

        assert manager.get_task_result("sub-1", "rpc-1") == ("completed", "done")

    def test_unknown_agent_is_unknown_not_pending(self):
        """An unknown worker cannot be answered — and that is not "not ready"."""
        manager = SubagentManager(_mock_config())
        assert manager.get_task_result("ghost", "rpc-1") == ("unknown", None)


# ── Task lifecycle: bounds, preemption, and honest bookkeeping ────────────


class TestSendRequestLifecycle:
    """One place sends a task, so one place unwinds it when the write fails."""

    @pytest.mark.asyncio
    async def test_a_failed_write_leaves_no_ghost_task(self):
        """A closed pipe must not leave a pending record and a busy worker.

        The record would read "pending" forever and the await-slot would keep
        ``is_busy`` true with nothing in flight — so every later send would
        auto-queue as async against an idle child.
        """
        proc = _running_proc()
        proc._process.stdin.write = Mock(side_effect=OSError("pipe closed"))

        with pytest.raises(OSError):
            await proc.send_task("do X")

        assert proc._awaiting == set()
        assert proc._pending == {}
        assert proc.is_busy is False
        rec = next(iter(proc._task_records.values()))
        assert rec["status"] == "failed"

    @pytest.mark.asyncio
    async def test_a_sync_timeout_names_the_task_it_gave_up_on(self, monkeypatch):
        """The raised timeout carries the id, so the late result is reachable."""
        monkeypatch.setattr(_timeouts.timeouts.work, "task_budget", 0.01)
        proc = _running_proc()
        written: list[dict] = []
        proc._process.stdin.write = lambda b: written.append(json.loads(b))

        with pytest.raises(TaskTimeout) as err:
            await proc.send_task("do X")

        task_id = err.value.task_id
        assert task_id in proc._task_records
        assert proc.get_task_result(task_id)[0] == "failed"
        assert proc._awaiting == set()
        # The abandoned task was preempted in the child (a serial worker must
        # not be wedged by it).
        assert [m["method"] for m in written] == ["worker/send", "worker/cancel"]

    @pytest.mark.asyncio
    async def test_a_cancelled_wait_preempts_the_child_too(self):
        """An Esc under a sync send is the same situation as a timeout."""
        proc = _running_proc()
        written: list[dict] = []
        proc._process.stdin.write = lambda b: written.append(json.loads(b))

        task = asyncio.create_task(proc.send_task("do X"))
        await _spin_until(lambda: bool(proc._pending))
        assert proc._pending
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert "worker/cancel" in [m["method"] for m in written]
        assert proc._awaiting == set()
        assert proc.is_busy is False


class TestAsyncTaskExpiry:
    """An async task has no waiting caller — the watchdog is its only bound."""

    @pytest.mark.asyncio
    async def test_a_task_that_never_answers_expires_and_says_so(self, monkeypatch):
        monkeypatch.setattr(_timeouts.timeouts.work, "task_lifetime", 0.01)
        proc = _running_proc()
        written: list[dict] = []
        proc._process.stdin.write = lambda b: written.append(json.loads(b))
        manager = Mock(spec=SubagentManager)
        manager.on_task_complete = AsyncMock()
        set_manager(manager)
        try:
            rpc_id = await proc.send_task_async("do X")
            assert await _wait_for(lambda: proc._watchdogs == {})
            await asyncio.sleep(0)
            state, result = proc.get_task_result(rpc_id)
            assert state == "failed"
            assert "timed out" in (result or "")
            assert proc._awaiting == set()
            # Nobody else would ever tell the caller: the auto-push is the only
            # channel an async task has.
            manager.on_task_complete.assert_awaited_once()
            assert manager.on_task_complete.await_args.args[1] == rpc_id
            assert "worker/cancel" in [m["method"] for m in written]
        finally:
            clear_manager()

    @pytest.mark.asyncio
    async def test_a_poll_mode_task_expires_without_a_push(self, monkeypatch):
        monkeypatch.setattr(_timeouts.timeouts.work, "task_lifetime", 0.01)
        proc = _running_proc()
        manager = Mock(spec=SubagentManager)
        manager.on_task_complete = AsyncMock()
        set_manager(manager)
        try:
            rpc_id = await proc.send_task_async("do X", mode="poll")
            assert await _wait_for(lambda: proc.get_task_result(rpc_id)[0] != "pending")
            assert proc.get_task_result(rpc_id)[0] == "failed"
            manager.on_task_complete.assert_not_awaited()
        finally:
            clear_manager()

    @pytest.mark.asyncio
    async def test_a_watched_task_that_answers_stops_its_watchdog(self, monkeypatch):
        """A completed task must not expire later and rewrite its own record."""
        monkeypatch.setattr(_timeouts.timeouts.work, "task_lifetime", 0.05)
        proc = _running_proc()
        proc._process.stdin.write = Mock()
        rpc_id = await proc.send_task_async("do X")
        proc._dispatch_message({"id": rpc_id, "result": "the answer"})

        assert proc._watchdogs == {}
        assert proc.get_task_result(rpc_id) == ("completed", "the answer")
        await asyncio.sleep(0.08)  # past the (patched) lifetime
        assert proc.get_task_result(rpc_id) == ("completed", "the answer")


class TestGetTaskResultStates:
    """Every state is reported as itself — "pending" is not a catch-all."""

    def _proc(self) -> SubagentProcess:
        return SubagentProcess("test", _mock_config())

    def test_unknown_id(self):
        assert self._proc().get_task_result("nope") == ("unknown", None)

    def test_pending(self):
        proc = self._proc()
        proc._record_send("rpc-1", "do X", mode="async")
        assert proc.get_task_result("rpc-1") == ("pending", None)

    def test_cancelled_reports_its_own_state(self):
        proc = self._proc()
        proc._record_send("rpc-1", "do X", mode="async")
        proc._record_update("rpc-1", "cancelled", "Cancelled by parent")
        assert proc.get_task_result("rpc-1") == ("cancelled", "Cancelled by parent")

    def test_reading_does_not_consume_the_result(self):
        """An auto-pushed result stays pollable — the tool promises it."""
        proc = self._proc()
        proc._record_send("rpc-1", "do X", mode="async")
        proc._dispatch_message({"id": "rpc-1", "result": "done"})
        assert proc.get_task_result("rpc-1") == ("completed", "done")
        assert proc.get_task_result("rpc-1") == ("completed", "done")

    def test_a_record_outliving_its_stored_result_still_answers(self):
        proc = self._proc()
        proc._record_send("rpc-1", "do X", mode="async")
        proc._record_update("rpc-1", "completed", "the text")
        proc._async_results.clear()  # e.g. evicted by newer results
        assert proc.get_task_result("rpc-1") == ("completed", "the text")

    def test_a_result_outliving_its_evicted_record_still_answers(self):
        proc = self._proc()
        proc._async_results["rpc-1"] = "the text"
        assert proc.get_task_result("rpc-1") == ("completed", "the text")


class TestAbandonedTasks:
    """A worker that is gone must not leave work pending in the parent."""

    @pytest.mark.asyncio
    async def test_the_reader_closing_announces_the_tasks_it_cannot_answer(self):
        proc = _running_proc()
        manager = Mock(spec=SubagentManager)
        manager.on_task_complete = AsyncMock()
        set_manager(manager)
        try:
            proc._record_send("rpc-1", "do X", mode="async")
            proc._awaiting.add("rpc-1")

            async def _eof():
                return b""

            proc._process.stdout = Mock()
            proc._process.stdout.readline = _eof
            await proc._read_stdout()

            rec = proc._task_records["rpc-1"]
            assert rec["status"] == "failed"
            assert "exited" in rec["result"]
            assert proc._awaiting == set()
            assert proc.is_running is False
            await asyncio.sleep(0)
            manager.on_task_complete.assert_awaited_once()
        finally:
            clear_manager()

    @pytest.mark.asyncio
    async def test_a_dead_child_takes_its_config_file_with_it(self, tmp_path):
        """The 0600 file carries the parent's plaintext api keys."""
        secret = tmp_path / "slife_subagent_x.json"
        secret.write_text('{"api_key": "sk-live"}', encoding="utf-8")
        proc = _running_proc()
        proc._config_file = str(secret)

        async def _eof():
            return b""

        proc._process.stdout = Mock()
        proc._process.stdout.readline = _eof
        await proc._read_stdout()

        assert not secret.exists()
        assert proc._config_file is None

    @pytest.mark.asyncio
    async def test_abandoning_twice_is_a_no_op(self):
        """The reader's finally can race an explicit stop — one notice each."""
        proc = _running_proc()
        manager = Mock(spec=SubagentManager)
        manager.on_task_complete = AsyncMock()
        set_manager(manager)
        try:
            proc._record_send("rpc-1", "do X", mode="async")
            proc._awaiting.add("rpc-1")
            proc._abandon_pending("worker stopped")
            proc._abandon_pending("worker exited before replying")
            await asyncio.sleep(0)
            assert manager.on_task_complete.await_count == 1
            assert proc._task_records["rpc-1"]["result"] == "Error: worker stopped"
        finally:
            clear_manager()


class TestLateResultReconcilesTheRecord:
    """One task, one story: a late reply un-does the timeout verdict."""

    def test_a_late_result_flips_the_record_to_completed(self):
        proc = SubagentProcess("test", _mock_config())
        proc._record_send("rpc-1", "do X", mode="sync")
        proc._record_update("rpc-1", "failed", "Error: timed out")
        proc._late_results.add("rpc-1")

        proc._dispatch_message({"id": "rpc-1", "result": "the answer"})

        assert proc._task_records["rpc-1"]["status"] == "completed"
        assert proc.get_task_result("rpc-1") == ("completed", "the answer")


class TestSpawnRegistry:
    """The registry is the lifetime — and only one spawn may own a name."""

    @pytest.mark.asyncio
    async def test_concurrent_spawn_of_one_name_starts_one_worker(self):
        """Two spawn calls in ONE tool batch: the loop runs them concurrently.

        Without the registry lock both pass the reuse check and the cap check,
        both start a child, and the second overwrites the first's entry — a
        live process nothing can list, send to, or stop.
        """
        manager = SubagentManager(_mock_config())
        started: list[str] = []

        async def fake_start(self):  # a patched method
            started.append(self.name)
            self._running = True
            self._process = Mock()
            self._process.returncode = None
            self._ready.set()
            await asyncio.sleep(0.01)  # a real boot takes time

        with patch.object(SubagentProcess, "start", fake_start):
            names = await asyncio.gather(manager.spawn("w"), manager.spawn("w"))

        assert names == ["w", "w"]
        assert started == ["w"]
        assert list(manager._subagents) == ["w"]

    def test_a_worker_that_died_on_its_own_is_swept(self):
        """Nothing else deletes it: stop() is for live workers, by name."""
        manager = SubagentManager(_mock_config())
        dead = Mock()
        dead.is_running = False
        live = Mock()
        live.is_running = True
        manager._subagents = {"dead": dead, "live": live}

        assert manager._prune_dead() == ["dead"]
        assert list(manager._subagents) == ["live"]

    @pytest.mark.asyncio
    async def test_spawn_sweeps_the_dead_before_it_registers(self):
        manager = SubagentManager(_mock_config())
        dead = Mock()
        dead.is_running = False
        manager._subagents = {"dead": dead}

        async def fake_start(self):  # a patched method
            self._running = True
            self._process = Mock()
            self._process.returncode = None
            self._ready.set()

        with patch.object(SubagentProcess, "start", fake_start):
            await manager.spawn("fresh")

        assert list(manager._subagents) == ["fresh"]
