"""Tests for Slife.a2a.task_store — TaskRecord and TaskStore."""

import pytest; pytestmark = pytest.mark.unit

import pytest

from slife.a2a.task_store import TaskRecord, TaskStore, get_store


# ── TaskRecord ──────────────────────────────────────────────────────────


class TestTaskRecord:
    """Tests for TaskRecord dataclass."""

    def test_default_values(self):
        rec = TaskRecord(task_id="t1", agent_name="agent-1", status="pending")
        assert rec.task_id == "t1"
        assert rec.agent_name == "agent-1"
        assert rec.status == "pending"
        assert rec.created_at > 0


# ── TaskStore — writes ──────────────────────────────────────────────────


class TestTaskStoreWrites:
    """Tests for TaskStore write operations."""

    @pytest.fixture
    def store(self):
        return TaskStore()

    def test_record_send(self, store):
        rec = store.record_send("t1", "agent-1")
        assert rec.task_id == "t1"
        assert rec.agent_name == "agent-1"
        assert rec.status == "pending"

    def test_record_result(self, store):
        store.record_send("t1", "agent-1")
        rec = store.record_result("t1")
        assert rec.status == "completed"

    def test_record_result_unknown_task(self, store):
        assert store.record_result("nonexistent") is None

    def test_record_result_does_not_overwrite_failed(self, store):
        """Regression: a late result after a timeout marked the record failed
        must not flip it back to completed (the caller was already told it
        timed out)."""
        store.record_send("t1", "agent-1")
        store.record_error("t1")
        rec = store.record_result("t1")
        assert rec.status == "failed"

    def test_record_error(self, store):
        store.record_send("t1", "agent-1")
        assert store.record_error("t1").status == "failed"

    def test_record_error_unknown_task(self, store):
        assert store.record_error("nonexistent") is None

    def test_record_cancel(self, store):
        store.record_send("t1", "agent-1")
        assert store.record_cancel("t1").status == "cancelled"

    def test_record_cancel_unknown_task(self, store):
        assert store.record_cancel("nonexistent") is None


# ── TaskStore — reads ───────────────────────────────────────────────────


class TestTaskStoreReads:
    """Tests for TaskStore read operations."""

    @pytest.fixture
    def store(self):
        s = TaskStore()
        s.record_send("t1", "agent-1")
        s.record_send("t2", "agent-2")
        s.record_result("t1")
        s.record_error("t2")
        return s

    def test_get(self, store):
        rec = store.get("t1")
        assert rec.task_id == "t1"
        assert rec.status == "completed"

    def test_get_missing(self, store):
        assert store.get("nonexistent") is None


# ── TaskStore — maintenance ─────────────────────────────────────────────


class TestTaskStoreMaintenance:
    """Tests for TaskStore maintenance operations."""

    def test_prune_removes_terminal_entries(self):
        """When exceeding MAX_RECORDS, oldest terminal entries are pruned."""
        store = TaskStore()
        for i in range(store.MAX_RECORDS + 10):
            store.record_send(f"t{i}", "agent-1")
            store.record_result(f"t{i}")
        assert len(store._records) <= store.MAX_RECORDS

    def test_prune_keeps_pending(self):
        """Pending tasks are not pruned — only terminal ones."""
        store = TaskStore()
        for i in range(store.MAX_RECORDS + 5):
            store.record_send(f"t{i}", "agent-1")
            if i < store.MAX_RECORDS + 4:
                store.record_result(f"t{i}")
        pending_rec = store.get(f"t{store.MAX_RECORDS + 4}")
        assert pending_rec is not None
        assert pending_rec.status == "pending"


# ── Module-level singleton ──────────────────────────────────────────────


class TestStoreSingleton:
    """Tests for the get_store module-level singleton."""

    def test_get_store_returns_singleton(self):
        s1 = get_store()
        s2 = get_store()
        assert s1 is s2
