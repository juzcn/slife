"""Tests for Slife.a2a.task_store — TaskRecord and TaskStore."""

import pytest

from slife.a2a.task_store import TaskRecord, TaskStore, get_store, clear_store


# ── TaskRecord ──────────────────────────────────────────────────────────


class TestTaskRecord:
    """Tests for TaskRecord dataclass."""

    def test_default_values(self):
        rec = TaskRecord(
            task_id="t1",
            agent_id="agent-1",
            task_preview="do something",
            status="pending",
            transport="mqtt",
        )
        assert rec.task_id == "t1"
        assert rec.agent_id == "agent-1"
        assert rec.task_preview == "do something"
        assert rec.status == "pending"
        assert rec.transport == "mqtt"
        assert rec.created_at > 0
        assert rec.completed_at is None
        assert rec.result is None

    def test_with_result(self):
        rec = TaskRecord(
            task_id="t2",
            agent_id="a2",
            task_preview="run tests",
            status="completed",
            transport="subagent",
            completed_at=100.0,
            result="All tests passed",
        )
        assert rec.status == "completed"
        assert rec.completed_at == 100.0
        assert rec.result == "All tests passed"


# ── TaskStore — writes ──────────────────────────────────────────────────


class TestTaskStoreWrites:
    """Tests for TaskStore write operations."""

    @pytest.fixture
    def store(self):
        return TaskStore()

    def test_record_send(self, store):
        rec = store.record_send("t1", "agent-1", "do the thing", "mqtt")
        assert rec.task_id == "t1"
        assert rec.agent_id == "agent-1"
        assert rec.status == "pending"
        assert rec.transport == "mqtt"
        assert rec.task_preview == "do the thing"

    def test_record_send_truncates_preview(self, store):
        long_task = "x" * 300
        rec = store.record_send("t1", "agent-1", long_task, "subagent")
        assert len(rec.task_preview) == store.MAX_PREVIEW_LEN

    def test_record_result(self, store):
        store.record_send("t1", "agent-1", "task", "mqtt")
        rec = store.record_result("t1", "Here is the answer")
        assert rec.status == "completed"
        assert rec.completed_at is not None
        assert rec.result == "Here is the answer"

    def test_record_result_truncates_long_result(self, store):
        store.record_send("t1", "agent-1", "task", "mqtt")
        long_result = "y" * 3000
        rec = store.record_result("t1", long_result)
        assert len(rec.result) == store.MAX_RESULT_LEN

    def test_record_result_unknown_task(self, store):
        assert store.record_result("nonexistent", "x") is None

    def test_record_error(self, store):
        store.record_send("t1", "agent-1", "task", "mqtt")
        rec = store.record_error("t1", "Something broke")
        assert rec.status == "failed"
        assert "Error: Something broke" in rec.result

    def test_record_error_unknown_task(self, store):
        assert store.record_error("nonexistent", "err") is None

    def test_record_cancel(self, store):
        store.record_send("t1", "agent-1", "task", "mqtt")
        rec = store.record_cancel("t1")
        assert rec.status == "cancelled"
        assert rec.completed_at is not None

    def test_record_cancel_unknown_task(self, store):
        assert store.record_cancel("nonexistent") is None


# ── TaskStore — reads ───────────────────────────────────────────────────


class TestTaskStoreReads:
    """Tests for TaskStore read operations."""

    @pytest.fixture
    def store(self):
        s = TaskStore()
        s.record_send("t1", "agent-1", "task one", "mqtt")
        s.record_send("t2", "agent-2", "task two", "subagent")
        s.record_send("t3", "agent-1", "task three", "mqtt")
        s.record_result("t1", "done one")
        s.record_error("t2", "failed two")
        return s

    def test_get(self, store):
        rec = store.get("t1")
        assert rec.task_id == "t1"
        assert rec.status == "completed"

    def test_get_missing(self, store):
        assert store.get("nonexistent") is None

    def test_list_tasks_all(self, store):
        records = store.list_tasks()
        assert len(records) == 3
        # newest first
        assert records[0].created_at >= records[-1].created_at

    def test_list_tasks_filter_agent_id(self, store):
        records = store.list_tasks(agent_id="agent-1")
        assert len(records) == 2
        assert all(r.agent_id == "agent-1" for r in records)

    def test_list_tasks_filter_status(self, store):
        records = store.list_tasks(status="pending")
        assert len(records) == 1
        assert records[0].task_id == "t3"

    def test_list_tasks_filter_transport(self, store):
        records = store.list_tasks(transport="subagent")
        assert len(records) == 1
        assert records[0].task_id == "t2"

    def test_list_tasks_combined_filters(self, store):
        records = store.list_tasks(agent_id="agent-1", status="completed")
        assert len(records) == 1
        assert records[0].task_id == "t1"

    def test_list_tasks_limit(self, store):
        records = store.list_tasks(limit=1)
        assert len(records) == 1

    def test_count_by_status(self, store):
        counts = store.count_by_status()
        assert counts == {"completed": 1, "failed": 1, "pending": 1}


# ── TaskStore — maintenance ─────────────────────────────────────────────


class TestTaskStoreMaintenance:
    """Tests for TaskStore maintenance operations."""

    def test_clear(self):
        store = TaskStore()
        store.record_send("t1", "agent-1", "task", "mqtt")
        store.clear()
        assert store.list_tasks() == []

    def test_prune_removes_terminal_entries(self):
        """When exceeding MAX_RECORDS, oldest terminal entries are pruned."""
        store = TaskStore()
        # Fill with terminal entries
        for i in range(store.MAX_RECORDS + 10):
            store.record_send(f"t{i}", "agent-1", f"task {i}", "mqtt")
            store.record_result(f"t{i}", f"result {i}")
        # Should be at or under max
        assert len(store.list_tasks()) <= store.MAX_RECORDS

    def test_prune_keeps_pending(self):
        """Pending tasks are not pruned — only terminal ones."""
        store = TaskStore()
        # Fill mostly terminal entries, with one pending at the end
        for i in range(store.MAX_RECORDS + 5):
            store.record_send(f"t{i}", "agent-1", f"task {i}", "mqtt")
            if i < store.MAX_RECORDS + 4:
                store.record_result(f"t{i}", f"result {i}")
        # The last pending entry should survive
        pending_rec = store.get(f"t{store.MAX_RECORDS + 4}")
        assert pending_rec is not None
        assert pending_rec.status == "pending"


# ── Module-level singleton ──────────────────────────────────────────────


class TestStoreSingleton:
    """Tests for get_store / clear_store module-level singleton."""

    def teardown_method(self):
        clear_store()

    def test_get_store_returns_singleton(self):
        s1 = get_store()
        s2 = get_store()
        assert s1 is s2

    def test_get_store_creates_new_after_clear(self):
        s1 = get_store()
        s1.record_send("t1", "agent", "task", "mqtt")
        clear_store()
        s2 = get_store()
        assert s2.list_tasks() == []

    def test_clear_store_idempotent(self):
        """Calling clear_store with no store is safe."""
        clear_store()
        clear_store()  # Should not raise
