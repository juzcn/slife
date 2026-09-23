"""Task store — shared A2A task-lifecycle tracking.

Every A2A operation (send, result, cancel) records metadata here so the
mesh can attribute results to their send and answer cancel/status lookups.
Internal bookkeeping only — the wire is the official ``a2a-over-mqtt`` SDK.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _iso_now() -> str:
    """Current UTC time as an ISO-8601 string (official A2A timestamps)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

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

    created_iso: str = field(default_factory=_iso_now)
    """Wall-clock ISO-8601 creation time."""


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

        A task that is already in a terminal state (cancelled or failed) stays
        there — a result arriving from a peer after the caller was told about a
        cancel must not flip the record back to completed and contradict what
        the caller saw.  A *wait* timeout is no longer terminal: the sender
        auto-degrades to async, so a pending record flips to completed here and
        the late result still delivers.
        """
        rec = self._records.get(task_id)
        if rec is None:
            return None
        if rec.status in ("cancelled", "failed"):
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
        """Return filtered task records, newest first.

        The store's bulk-read surface (the LLM-facing ``a2a_list_tasks`` tool
        was retired with the push-model rework — see DESIGN.md §8 — but
        the records stay queryable here).
        """
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

    # ── Maintenance ───────────────────────────────────────────────────

    def clear(self) -> None:
        """Remove all records."""
        self._records.clear()

    def _maybe_prune(self) -> None:
        """Drop oldest entries when over max — terminal status first, then the
        oldest pending so a burst of async sends to slow/hung peers can't grow
        the in-memory store past the cap."""
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
