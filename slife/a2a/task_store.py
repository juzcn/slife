"""Task store — shared A2A task-lifecycle tracking.

Every A2A operation (send, result, cancel) records metadata here so that
``a2a_list_tasks`` and ``a2a_get_task_result`` can return structured
task state following A2A protocol semantics.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field

from slife.a2a.wire import Message, TaskState, iso_now

# ── Task record ─────────────────────────────────────────────────────────


@dataclass
class TaskRecord:
    """Full lifecycle metadata for one A2A task."""

    task_id: str
    """Unique correlation / rpc id."""

    agent_name: str
    """Target agent this task was sent to."""

    task_preview: str
    """First 200 characters of the task text."""

    status: str
    """One of ``"pending"``, ``"completed"``, ``"failed"``, ``"cancelled"``."""

    transport: str
    """Transport binding that produced the task (currently ``"mqtt"``)."""

    created_at: float = field(default_factory=_time.monotonic)
    completed_at: float | None = None
    result: str | None = None
    """Result text (first 2000 chars).  ``None`` while pending."""

    created_iso: str = field(default_factory=iso_now)
    """Wall-clock ISO-8601 creation time (for the official Task wire shape)."""

    def to_task(self) -> dict:
        """Serialize as an official A2A ``Task`` dict (mirrors ``a2a_pb2``).

        The internal status strings map onto :class:`TaskState` values; the
        result becomes the task's primary artifact.
        """
        state = {
            "pending": TaskState.SUBMITTED.value,
            "completed": TaskState.COMPLETED.value,
            "failed": TaskState.FAILED.value,
            "cancelled": TaskState.CANCELLED.value,
        }.get(self.status, TaskState.SUBMITTED.value)

        status = TaskStatus_dict(state, self.created_iso, self.result)
        artifacts: list[dict] = []
        if self.result is not None:
            artifacts.append({
                "name": "result",
                "parts": [{"type": "text", "text": self.result}],
            })
        return {
            "id": self.task_id,
            "status": status,
            "artifacts": artifacts,
            "history": [],
            "metadata": {
                "target": self.agent_name,
                "preview": self.task_preview,
                "transport": self.transport,
            },
        }


def TaskStatus_dict(state: str, timestamp: str, result: str | None) -> dict:
    """Build an official ``TaskStatus`` dict."""
    d: dict = {"state": state, "timestamp": timestamp}
    if result is not None:
        d["message"] = Message.text_message(result, role="agent").to_dict()
    return d


# ── Task store ──────────────────────────────────────────────────────────


class TaskStore:
    """Thread-safe task-lifecycle store shared by both transports.

    Module-level singleton — ``get_store()`` / ``clear_store()``.
    """

    MAX_RESULT_LEN = 2000
    MAX_PREVIEW_LEN = 200
    MAX_RECORDS = 500  # soft cap — oldest completed entries pruned first

    def __init__(self) -> None:
        self._records: dict[str, TaskRecord] = {}

    # ── Write ─────────────────────────────────────────────────────────

    def record_send(
        self, task_id: str, agent_name: str, task: str, transport: str,
    ) -> TaskRecord:
        """Record a newly-sent task (status = pending)."""
        rec = TaskRecord(
            task_id=task_id,
            agent_name=agent_name,
            task_preview=task[: self.MAX_PREVIEW_LEN],
            status="pending",
            transport=transport,
        )
        self._records[task_id] = rec
        self._maybe_prune()
        return rec

    def record_result(self, task_id: str, result: str) -> TaskRecord | None:
        """Mark a task as completed and store its result.

        A task that was already cancelled stays cancelled — a result arriving
        from a peer that ignored the CancelTask must not flip the record back
        to completed and contradict the cancel the caller was told about
        (REVIEW §1-12).
        """
        rec = self._records.get(task_id)
        if rec is None:
            return None
        if rec.status == "cancelled":
            return rec
        rec.status = "completed"
        rec.completed_at = _time.monotonic()
        rec.result = result[: self.MAX_RESULT_LEN]
        return rec

    def record_error(self, task_id: str, error: str) -> TaskRecord | None:
        """Mark a task as failed."""
        rec = self._records.get(task_id)
        if rec is None:
            return None
        rec.status = "failed"
        rec.completed_at = _time.monotonic()
        rec.result = f"Error: {error}"[: self.MAX_RESULT_LEN]
        return rec

    def record_cancel(self, task_id: str) -> TaskRecord | None:
        """Mark a task as cancelled."""
        rec = self._records.get(task_id)
        if rec is None:
            return None
        rec.status = "cancelled"
        rec.completed_at = _time.monotonic()
        return rec

    # ── Read ──────────────────────────────────────────────────────────

    def get(self, task_id: str) -> TaskRecord | None:
        """Return a task record by id, or ``None``."""
        return self._records.get(task_id)

    def list_tasks(
        self,
        agent_name: str | None = None,
        status: str | None = None,
        transport: str | None = None,
        limit: int = 50,
    ) -> list[TaskRecord]:
        """Return filtered task records, newest first."""
        result = list(self._records.values())

        if agent_name is not None:
            result = [r for r in result if r.agent_name == agent_name]
        if status is not None:
            result = [r for r in result if r.status == status]
        if transport is not None:
            result = [r for r in result if r.transport == transport]

        # Newest first
        result.sort(key=lambda r: r.created_at, reverse=True)
        return result[:limit]

    def count_by_status(self) -> dict[str, int]:
        """Return ``{status: count}`` summary."""
        counts: dict[str, int] = {}
        for r in self._records.values():
            counts[r.status] = counts.get(r.status, 0) + 1
        return counts

    # ── Maintenance ───────────────────────────────────────────────────

    def clear(self) -> None:
        """Remove all records."""
        self._records.clear()

    def _maybe_prune(self) -> None:
        """Drop oldest entries when over max — terminal status first, then the
        oldest pending so a burst of async sends to slow/hung peers can't grow
        the in-memory store past the cap (REVIEW §1-12)."""
        if len(self._records) <= self.MAX_RECORDS:
            return
        excess = len(self._records) - self.MAX_RECORDS + 50
        # Oldest first; terminal-status entries preferred for removal.
        ordered = sorted(
            self._records.values(),
            key=lambda r: (
                r.status not in ("completed", "cancelled", "failed"),
                r.created_at,
            ),
        )
        to_remove = ordered[:excess]
        for r in to_remove:
            self._records.pop(r.task_id, None)


# ── Module-level singleton ──────────────────────────────────────────────

_store: TaskStore | None = None


def get_store() -> TaskStore:
    """Return the module-level :class:`TaskStore` singleton.

    Created on first access if not already set by ``AgentService``.
    """
    global _store
    if _store is None:
        _store = TaskStore()
    return _store


def clear_store() -> None:
    """Remove all task records (called on A2A shutdown)."""
    global _store
    if _store is not None:
        _store.clear()
    _store = None
