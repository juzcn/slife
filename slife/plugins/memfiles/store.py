"""MemfilesStore — notes / diary / files knowledge base with hybrid search.

Owns the memfiles SQLite index (``{agent}.files/.index.db``) and mirrors
note/diary content to human-readable markdown files under ``{agent}.files/``.

Three typed tables (each with its own FTS5 + vec0 index):
  - ``notes`` — keyed by ``subject``; content mirrored to ``notes/<slug>.md``
  - ``diary``  — keyed by ``date``;  content mirrored to ``diary/<date>.md``
  - ``files``  — saved attachments (binary stays on the filesystem); an
    LLM-written ``summary`` is embedded for semantic search

Implements the SemanticManager "document source" contract
(``count_unembedded`` / ``get_unembedded_docs`` / ``replace_embedding_chunks`` /
``reconfigure_for_embedding``) over a unified view of all three kinds, so the
shared ``SemanticManager`` (memdb.semantic) drives the memfiles drainer.
Code reuse is via memdb helpers: ``_chunk_text``, ``_split_chunks_to_token_limit``,
``_serialize_f32``, ``_to_fts5_query``, ``_contains_cjk``, ``_like_terms``,
``merge_hybrid``.
"""

import asyncio
import logging
import re
from pathlib import Path

import aiosqlite

from slife.plugins.memdb.search import merge_hybrid
from slife.plugins.memdb.store import (
    DEFAULT_EMBEDDING_DIM,
    VecStoreLifecycleMixin,
    _clamp_limit,
    _contains_cjk,
    _like_terms,
    _serialize_f32,
    _to_fts5_query,
    in_placeholders,
)
from slife.timeutil import normalize_time_bound, now_local_seconds

logger = logging.getLogger(__name__)

#: Local ISO-seconds timestamp — the shared helper under the store's name
#: (memdb aliases it the same way).
_now = now_local_seconds


def _slugify(text: str) -> str:
    """Turn arbitrary text into a safe filename slug."""
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug.strip("-")[:120]


def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """Return ``directory / stem{suffix}``, appending ``_N`` when taken."""
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    n = 1
    while True:
        candidate = directory / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _category_from_path(saved_path: str) -> str:
    """Derive a file's category from its stored path (``files/<cat>/<name>``)."""
    parts = Path(saved_path).parts
    if len(parts) >= 2 and parts[0] == "files":
        return parts[1]
    return ""


#: Per-kind specs — maps a kind to its tables/columns in the generic doc shape.
#:
#: ``time_col`` is the kind's own TIME AXIS: the single column every time filter
#: on that kind measures against — the list window AND the search window, and
#: the column the list is ordered by.  It lives here, once per kind, because a
#: bound that meant one thing arriving through ``cabinet_search(kind="diary")``
#: and another through ``diary_list`` is precisely how "last month" came to have
#: two answers.
#:
#: Which column that is follows from whether the CONTENT has a date:
#: - ``diary`` is date-KEYED — ``date`` is UNIQUE and names the file on disk
#:   (``diary/<date>.md``) — so its date is content, not bookkeeping;
#: - a ``note`` has no date of its own but is a living document, so its axis is
#:   ``updated_at``, which is also the order ``note_list`` shows;
#: - a ``file`` is written once and never touched, so ``created_at`` IS its date;
#: - a ``report`` likewise.  Its ``period_start``/``period_end`` say what the
#:   report COVERS — a different dimension from when it exists, and nullable —
#:   so the axis is ``created_at``.
#:
#: ``time_granularity`` follows the column: a date-only column compares in date
#: terms (a bare ``until`` already includes that whole day, no +1-day advance),
#: a timestamp column in datetime terms.
_KIND_SPECS = {
    "note": {
        "id_label": "note",
        "table": "notes",
        "fts": "notes_fts",
        "semantic": "notes_semantic",
        "key_col": "subject",
        "text_col": "content",
        "file_col": "file_path",
        "snippet_col": 1,        # notes_fts(subject, content, tags)
        "like_cols": ["subject", "content", "tags"],
        "time_col": "updated_at",
        "time_granularity": "datetime",
    },
    "diary": {
        "id_label": "diary",
        "table": "diary",
        "fts": "diary_fts",
        "semantic": "diary_semantic",
        "key_col": "date",
        "text_col": "content",
        "file_col": "file_path",
        "snippet_col": 0,        # diary_fts(content, tags)
        "like_cols": ["content", "tags"],
        "time_col": "date",
        "time_granularity": "date",
    },
    "file": {
        "id_label": "file",
        "table": "files",
        "fts": "files_fts",
        "semantic": "files_semantic",
        "key_col": "saved_path",
        "text_col": "summary",
        "file_col": "saved_path",
        "snippet_col": 3,        # files_fts(title, original_path, tags, summary)
        "like_cols": ["title", "original_path", "tags", "summary"],
        "time_col": "created_at",
        "time_granularity": "datetime",
    },
    "report": {
        "id_label": "report",
        "table": "reports",
        "fts": "reports_fts",
        "semantic": "reports_semantic",
        "key_col": "title",
        "text_col": "content",
        "file_col": "file_path",
        "snippet_col": 0,        # reports_fts(title, content, tags)
        "like_cols": ["title", "content", "tags"],
        "time_col": "created_at",
        "time_granularity": "datetime",
    },
}


def _kind_window(
    spec: dict, since: str | None, until: str | None,
) -> tuple[str | None, str | None]:
    """Normalize ``since``/``until`` for *spec*'s time axis.

    The ONE mapping from a kind to normalized bounds: the granularity is the
    kind's, so the same ``"last month"`` becomes a date for diary and a datetime
    for a note, and every caller (list or search) gets that for free instead of
    choosing a granularity of its own.
    """
    granularity = spec["time_granularity"]
    return (
        normalize_time_bound(since, role="since", granularity=granularity)
        if since else None,
        normalize_time_bound(until, role="until", granularity=granularity)
        if until else None,
    )


def _list_window(
    spec: dict, since: str | None, until: str | None,
) -> tuple[list[str], list[str]]:
    """``(clauses, params)`` for *spec*'s window, unaliased — the ``*_list`` shape.

    The list-shaped twin of :func:`_time_clause`: a ``*_list`` query names its
    columns bare, may merge the window with another predicate (a category, a
    task id), and may have no WHERE at all — so it hands back clauses to join
    rather than a suffix to append.  Both are built on :func:`_kind_window`, so
    the axis and the grammar are decided once.
    """
    since, until = _kind_window(spec, since, until)
    clauses: list[str] = []
    params: list[str] = []
    if since:
        clauses.append(f"{spec['time_col']} >= ?")
        params.append(since)
    if until:
        clauses.append(f"{spec['time_col']} <= ?")
        params.append(until)
    return clauses, params
_KIND_NAMES = ("note", "diary", "file", "report")


def _time_clause(
    since: str | None, until: str | None, column: str = "t.created_at",
) -> tuple[str, list[str]]:
    """The window a search runs inside, as ``(sql, params)``.

    *column* is the kind's time axis, qualified with the table alias these paths
    use — ``spec["time_col"]``, so a diary search windows ``date`` and a note
    search ``updated_at``.  The default keeps ``created_at`` for a caller with no
    kind in hand.  Built in ONE place for the three SQL paths (FTS5 / LIKE /
    regex): a window that meant different things in different modes would be the
    same class of bug as two LIKE clauses drifting apart.

    Returns a leading-``AND`` suffix, so a caller with no WHERE yet writes
    ``WHERE 1=1{sql}`` (memdb's ``_grep_scan`` does the same).  The ``*_list``
    methods do NOT use this — they have no table alias, may merge the window with
    another predicate, and may not have a WHERE at all, so they build their own
    clauses.  What they share with this is what matters: the axis
    (:data:`_KIND_SPECS`) and the grammar
    (:func:`~slife.timeutil.normalize_time_bound`).
    """
    clauses: list[str] = []
    params: list[str] = []
    if since:
        clauses.append(f"{column} >= ?")
        params.append(since)
    if until:
        clauses.append(f"{column} <= ?")
        params.append(until)
    return "".join(f" AND {c}" for c in clauses), params


class MemfilesStore(VecStoreLifecycleMixin):
    """The memfiles index: three typed document tables + hybrid search."""

    _semantic_tables: tuple[str, ...] = tuple(
        _KIND_SPECS[k]["semantic"] for k in _KIND_NAMES
    )
    _meta_table = "meta"
    _schema_dir = Path(__file__).parent
    _store_log_key = "memfiles"

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._mem_dir = db_path.parent          # .index.db lives in {agent}.files/
        self._conn: aiosqlite.Connection | None = None
        self._embedding_dim = DEFAULT_EMBEDDING_DIM
        self._embedding_model = ""
        self._vec_available = False             # sqlite-vec loaded? embeddings optional
        # Serializes multi-statement read-modify-write writes (upsert_note /
        # upsert_diary / upsert_report): aiosqlite lets coroutines interleave
        # between awaited statements, so two concurrent upserts of the same
        # subject/date/title could both see "no row" (UNIQUE IntegrityError)
        # or drop one writer's appended section.  Same reason memdb carries
        # one — subagents share this plugin over HTTP, so the lock covers the
        # main agent + subagent writers in this one process.
        self._write_lock = asyncio.Lock()

    # ── lifecycle ─────────────────────────────────────────────────
    # ``setup`` / ``reconfigure_for_embedding`` / ``_run_schema`` /
    # ``_maybe_migrate_vec_tables`` come from VecStoreLifecycleMixin.

    @property
    def _c(self):
        assert self._conn is not None
        return self._conn

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def mem_dir(self) -> Path:
        return self._mem_dir

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ── document writes (md mirrored) ──────────────────────────────

    async def upsert_note(self, subject: str, content: str, tags: str) -> dict:
        """Append a timestamped section to the subject's note (md + DB row).

        The whole read-modify-write (existing md + SELECT + UPDATE/INSERT +
        commit) runs under the store write lock: without it, two concurrent
        upserts (main agent + a subagent sharing this plugin) can both see
        "no row" and hit ``notes.subject UNIQUE``, or the md read-append-
        rewrite drops one writer's section.
        """
        if not subject.strip() or not content.strip():
            raise ValueError("subject and content are required")
        async with self._write_lock:
            slug = _slugify(subject) or "note"
            now = _now()
            body = content.strip()

            # The row's OWN file path is authoritative: re-appending to the
            # same subject reuses it.  A NEW subject whose slug collides with
            # an existing note (e.g. "API Design" vs "API-Design" → both
            # "api-design") must get a DISTINCT file — otherwise the two rows
            # would share one md, and updating either re-reads the merged
            # content (content bleeds across notes) (D2).
            cursor = await self._c.execute(
                "SELECT id, file_path FROM notes WHERE subject = ?", (subject,),
            )
            row = await cursor.fetchone()
            if row:
                doc_id = row["id"]
                rel = row["file_path"]
            else:
                notes_dir = self._mem_dir / "notes"
                rel = "notes/" + _unique_path(notes_dir, slug, ".md").name
                doc_id = None

            abs_path = self._mem_dir / rel
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            if abs_path.exists():
                existing = abs_path.read_text(encoding="utf-8").rstrip()
                new_md = f"{existing}\n\n## {now}\n\n{body}\n"
            else:
                new_md = f"# {subject}\n\n{body}\n"
            abs_path.write_text(new_md, encoding="utf-8")

            if doc_id is not None:
                await self._c.execute(
                    "UPDATE notes SET content=?, tags=?, updated_at=? "
                    "WHERE id=?",
                    (new_md, tags, now, doc_id),
                )
                await self._clear_kind_chunks("note", doc_id)
            else:
                cursor = await self._c.execute(
                    "INSERT INTO notes (subject, content, tags, file_path, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (subject, new_md, tags, rel, now, now),
                )
                doc_id = cursor.lastrowid
            await self._c.commit()
            return {"kind": "note", "doc_id": doc_id, "key": subject,
                    "file_path": rel, "content": new_md}

    async def upsert_diary(self, date: str, content: str, tags: str) -> dict:
        """Append a timestamped section to a day's diary (md + DB row).

        Serialized under the store write lock — see :meth:`upsert_note`.
        """
        if not content.strip():
            raise ValueError("content is required")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError(f"date must be YYYY-MM-DD, got {date!r}")
        async with self._write_lock:
            rel = f"diary/{date}.md"
            abs_path = self._mem_dir / rel
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            now = _now()
            body = content.strip()
            if abs_path.exists():
                existing = abs_path.read_text(encoding="utf-8").rstrip()
                new_md = f"{existing}\n\n## {now}\n\n{body}\n"
            else:
                new_md = f"# {date}\n\n{body}\n"
            abs_path.write_text(new_md, encoding="utf-8")

            cursor = await self._c.execute(
                "SELECT id FROM diary WHERE date = ?", (date,),
            )
            row = await cursor.fetchone()
            if row:
                await self._c.execute(
                    "UPDATE diary SET content=?, tags=?, file_path=?, updated_at=? "
                    "WHERE date=?",
                    (new_md, tags, rel, now, date),
                )
                await self._clear_kind_chunks("diary", row["id"])
                doc_id = row["id"]
            else:
                cursor = await self._c.execute(
                    "INSERT INTO diary (date, content, tags, file_path, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (date, new_md, tags, rel, now, now),
                )
                doc_id = cursor.lastrowid
            await self._c.commit()
            return {"kind": "diary", "doc_id": doc_id, "key": date,
                    "file_path": rel, "content": new_md}

    async def add_file(
        self, *, title: str, original_path: str, saved_path: str,
        mime: str, size: int, tags: str, summary: str,
    ) -> dict:
        """Record a saved file (bytes already copied by the caller)."""
        cursor = await self._c.execute(
            "INSERT INTO files (title, original_path, saved_path, mime, size, tags, summary, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, original_path, saved_path, mime, size, tags, summary, _now()),
        )
        await self._c.commit()
        return {"kind": "file", "doc_id": cursor.lastrowid,
                "key": saved_path, "file_path": saved_path}

    async def upsert_report(
        self, task_id: int | None, title: str, content: str, tags: str = "",
        period_start: str | None = None, period_end: str | None = None,
        due_at: str | None = None,
    ) -> dict:
        """Save a report (md + DB row) and, when bound to a task, confirm its run.

        Writes the report to ``reports/<slug>.md`` and the DB, then — only when
        *task_id* is not None — confirms a ``scheduled_runs`` row: with *due_at*
        the exact run (a backfill of a missed/failed run, whose row was flipped
        to ``pending`` at dispatch); without it the newest un-reported run (the
        cron-fire dispatch).  The confirmed run goes ``pending → ran`` — the one
        success writeback.  A standalone report (task_id None) never touches
        ``scheduled_runs``.
        """
        if not content.strip():
            raise ValueError("content is required")
        if not title.strip():
            raise ValueError("title is required")
        async with self._write_lock:
            slug = _slugify(title) or ("report" if task_id is None else f"report-{task_id}")
            now = _now()
            body = content.strip()

            # The report's OWN row is authoritative; re-saving the same
            # logical report (same task + same exact title) reuses its file,
            # appending sections.  Two DISTINCT reports whose titles
            # slug-collide ("API Design" vs "API-Design" → "api-design") must
            # NOT collapse into one row/file — identity is (task, exact title),
            # and a brand-new row claims its path via _unique_path (D2).
            if task_id is not None:
                cursor = await self._c.execute(
                    "SELECT id, file_path FROM reports "
                    "WHERE task_id = ? AND title = ? "
                    "ORDER BY id DESC LIMIT 1", (task_id, title),
                )
                row = await cursor.fetchone()
            else:
                cursor = await self._c.execute(
                    "SELECT id, file_path FROM reports "
                    "WHERE task_id IS NULL AND title = ? "
                    "ORDER BY id DESC LIMIT 1", (title,),
                )
                row = await cursor.fetchone()
            if row:
                doc_id = row["id"]
                rel = row["file_path"]
            else:
                reports_dir = self._mem_dir / "reports"
                rel = "reports/" + _unique_path(reports_dir, slug, ".md").name
                doc_id = None

            abs_path = self._mem_dir / rel
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            if abs_path.exists():
                # Same report → append a timestamped section (like diary).
                existing = abs_path.read_text(encoding="utf-8").rstrip()
                new_md = f"{existing}\n\n## {now}\n\n{body}\n"
            else:
                new_md = f"# {title}\n\n{body}\n"
            abs_path.write_text(new_md, encoding="utf-8")

            if doc_id is not None:
                await self._c.execute(
                    "UPDATE reports SET content=?, tags=?, "
                    "period_start=?, period_end=?, updated_at=? WHERE id=?",
                    (new_md, tags, period_start, period_end, now, doc_id),
                )
                await self._clear_kind_chunks("report", doc_id)
            else:
                cursor = await self._c.execute(
                    "INSERT INTO reports (task_id, title, content, tags, file_path, "
                    "period_start, period_end, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (task_id, title, new_md, tags, rel, period_start, period_end, now, now),
                )
                doc_id = cursor.lastrowid

            # Store-layer backfill: confirm the run this report is *for*, pending →
            # ran here (the ONLY 'ran' write).  Only a task-bound report does this;
            # a standalone report (task_id None) has no run to confirm.  A due_at
            # targets the exact run — the backfilled failed/missed run — so a newer
            # stale run is never grabbed; without one it links the newest un-linked
            # run (cron fire).
            # ``error`` is cleared with the status: the column records why a
            # run FAILED, and this write is the run being confirmed as done.
            # A report that lands after the worker's teardown already marked
            # the run failed ("worker finished without confirming") would
            # otherwise leave a row reading status='ran' next to a failure
            # reason — a row that contradicts itself, and one the backfill
            # reminder would keep surfacing.
            if task_id is not None:
                if due_at is not None:
                    await self._c.execute(
                        "UPDATE scheduled_runs SET report_id=?, status='ran', error='' "
                        "WHERE task_id=? AND due_at=? AND report_id IS NULL",
                        (doc_id, task_id, due_at),
                    )
                else:
                    await self._c.execute(
                        "UPDATE scheduled_runs SET report_id=?, status='ran', error='' "
                        "WHERE id = ("
                        "  SELECT id FROM scheduled_runs WHERE task_id=? AND report_id IS NULL "
                        "  ORDER BY due_at DESC LIMIT 1)",
                        (doc_id, task_id),
                    )
            await self._c.commit()
            return {"kind": "report", "doc_id": doc_id, "key": title,
                    "file_path": rel, "content": new_md}

    # ── scheduled-task registry ─────────────────────────────────────

    async def upsert_scheduled_task(
        self, name: str, description: str = "", schedule: str = "",
        timezone: str = "", enabled: bool = True,
    ) -> dict:
        """Create or update a scheduled task by name.  Returns its row."""
        now = _now()
        cursor = await self._c.execute(
            "SELECT id FROM scheduled_tasks WHERE name = ?", (name,),
        )
        row = await cursor.fetchone()
        if row:
            await self._c.execute(
                "UPDATE scheduled_tasks SET description=?, schedule=?, "
                "timezone=?, enabled=?, updated_at=? WHERE id=?",
                (description, schedule, timezone, 1 if enabled else 0, now, row["id"]),
            )
            task_id = row["id"]
        else:
            cursor = await self._c.execute(
                "INSERT INTO scheduled_tasks (name, description, schedule, timezone, "
                "enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, description, schedule, timezone, 1 if enabled else 0, now, now),
            )
            task_id = cursor.lastrowid
        await self._c.commit()
        return {"task_id": task_id, "name": name}

    async def get_scheduled_task(self, name: str) -> dict | None:
        cursor = await self._c.execute(
            "SELECT id, name, description, schedule, timezone, enabled, "
            "created_at, updated_at FROM scheduled_tasks WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def scheduled_tasks_list(self, enabled_only: bool = False) -> list[dict]:
        where = "WHERE enabled = 1" if enabled_only else ""
        cursor = await self._c.execute(
            f"SELECT id, name, description, schedule, timezone, enabled, "
            f"created_at, updated_at FROM scheduled_tasks {where} "
            f"ORDER BY name",
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def remove_scheduled_task(self, name: str) -> bool:
        """Delete a scheduled task and its run history.  Reports stay.

        Returns True if a task was removed.
        """
        cursor = await self._c.execute(
            "SELECT id FROM scheduled_tasks WHERE name = ?", (name,),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        task_id = row["id"]
        await self._c.execute(
            "DELETE FROM scheduled_runs WHERE task_id = ?", (task_id,),
        )
        await self._c.execute(
            "DELETE FROM scheduled_tasks WHERE id = ?", (task_id,),
        )
        await self._c.commit()
        return True

    async def record_scheduled_run(
        self, task_id: int, due_at: str, status: str = "pending",
    ) -> dict:
        """Insert a scheduled run (idempotent on (task_id, due_at)).

        Fires are recorded as ``pending`` — success is unconfirmed until a
        report lands (see :meth:`upsert_report`).  A run that already has a
        report (status ``ran``) is never downgraded by re-recording the same
        due time.
        """
        now = _now()
        cursor = await self._c.execute(
            "INSERT INTO scheduled_runs (task_id, due_at, status, ran_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(task_id, due_at) DO UPDATE SET status=excluded.status, "
            "ran_at=excluded.ran_at WHERE scheduled_runs.report_id IS NULL",
            (task_id, due_at, status, now),
        )
        await self._c.commit()
        return {"run_id": cursor.lastrowid, "task_id": task_id, "due_at": due_at}

    async def mark_run_missed(self, task_id: int, due_at: str) -> None:
        """Mark a due-but-not-dispatched run as missed (idempotent)."""
        await self._c.execute(
            "INSERT INTO scheduled_runs (task_id, due_at, status) VALUES (?, ?, 'missed') "
            "ON CONFLICT(task_id, due_at) DO UPDATE SET status='missed' "
            "WHERE scheduled_runs.status NOT IN "
            "('pending', 'ran', 'failed', 'skipped')",
            (task_id, due_at),
        )
        await self._c.commit()

    async def mark_run_failed(
        self, task_id: int, due_at: str, error: str = "",
    ) -> None:
        """Mark a dispatched-but-unconfirmed run as failed (best effort).

        Only a ``pending`` run is moved (a report may have already flipped it
        to ``ran``; a skipped run the user closed stays skipped).  ``error``
        is a detail string, not the state itself — the correctness invariant
        is "no report = failed", so a missing writeback here never matters.
        """
        await self._c.execute(
            "UPDATE scheduled_runs SET status='failed', error=? "
            "WHERE task_id=? AND due_at=? AND status='pending'",
            (error or "", task_id, due_at),
        )
        await self._c.commit()

    async def mark_run_skipped(self, task_id: int, due_at: str) -> bool:
        """Close a missed/failed run the user decided not to backfill.

        Returns whether a row actually changed.  Only ``missed`` and
        ``failed`` are skippable: a ``pending`` run is still in flight (the
        worker's report, or the startup sweep, settles it) and a ``ran`` one
        is already closed.  The caller reports this verdict instead of
        assuming the write landed.
        """
        cursor = await self._c.execute(
            "UPDATE scheduled_runs SET status='skipped' "
            "WHERE task_id=? AND due_at=? AND status IN ('missed', 'failed')",
            (task_id, due_at),
        )
        await self._c.commit()
        return cursor.rowcount > 0

    async def run_status(self, task_id: int, due_at: str) -> str | None:
        """The status of one run, or ``None`` when no such run is recorded."""
        cursor = await self._c.execute(
            "SELECT status FROM scheduled_runs WHERE task_id=? AND due_at=?",
            (task_id, due_at),
        )
        row = await cursor.fetchone()
        return row["status"] if row else None

    async def fail_unconfirmed_runs(self) -> list[dict]:
        """Startup sweep: dispatch-only runs from a previous process lifetime
        can never complete, so mark them failed.

        A run stays ``pending`` until the worker's report arrives; anything
        still pending at startup cannot complete.  A ``ran`` row always has
        its report and is never touched.  Returns the flipped runs (with task
        name) so the agent can surface them.
        """
        cursor = await self._c.execute(
            "SELECT r.task_id, t.name, r.due_at, r.status FROM scheduled_runs r "
            "JOIN scheduled_tasks t ON t.id = r.task_id "
            "WHERE r.status='pending' AND r.report_id IS NULL "
            "ORDER BY r.due_at DESC",
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        if rows:
            await self._c.execute(
                "UPDATE scheduled_runs SET status='failed', "
                "error=COALESCE(NULLIF(error,''), "
                "  'slife restarted before completion') "
                "WHERE status='pending' AND report_id IS NULL",
            )
            await self._c.commit()
        return rows

    async def last_run_due(self, task_id: int) -> str | None:
        """Return the newest ``due_at`` across all of a task's runs.

        Any status counts (ran/missed/confirmed) — the anchor for computing
        the next trigger must advance past missed runs too, or the loop would
        keep re-detecting the same overdue fire.
        """
        cursor = await self._c.execute(
            "SELECT MAX(due_at) FROM scheduled_runs WHERE task_id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        return row[0] if (row and row[0]) else None

    async def pending_run_task_ids(self) -> set[int]:
        """Return the set of task ids that currently have a ``pending`` run.

        A pending run means the worker may still be working (or its report has
        not landed yet) — the task's worker must NOT be recycled.
        """
        cursor = await self._c.execute(
            "SELECT DISTINCT task_id FROM scheduled_runs WHERE status = 'pending'",
        )
        return {row["task_id"] for row in await cursor.fetchall()}

    async def scheduled_runs_list(
        self, task_id: int | None = None, status: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List runs, newest-first; filter by task and/or status."""
        limit = _clamp_limit(limit)
        clauses: list[str] = []
        params: list[str] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(str(task_id))
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = await self._c.execute(
            f"SELECT id, task_id, due_at, status, ran_at, report_id, error "
            f"FROM scheduled_runs {where} ORDER BY due_at DESC LIMIT ? OFFSET 0",
            (*params, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    # ── browse / read ────────────────────────────────────────────────

    async def note_list(
        self, since: str | None = None, until: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> dict:
        """List notes, newest-updated first.  Lightweight — no content.

        ``since``/``until`` window the kind's time axis (:data:`_KIND_SPECS`) —
        for a note, ``updated_at``, which is also the column it orders by, so the
        window and the ordering describe one axis.  ``total`` counts the window,
        not the whole table.

        Returns ``{"entries": [...], "total": n}`` so the caller knows how
        many more remain beyond this page (``offset + len(entries) < total``).
        """
        limit = _clamp_limit(limit)
        offset = max(0, offset)
        clauses, params = _list_window(_KIND_SPECS["note"], since, until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = await self._c.execute(f"SELECT COUNT(*) FROM notes {where}", params)
        row = await cursor.fetchone()
        total = row[0] if row else 0
        cursor = await self._c.execute(
            f"SELECT id, subject, tags, file_path, created_at, updated_at "
            f"FROM notes {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        entries = [dict(row) for row in await cursor.fetchall()]
        return {"entries": entries, "total": total}

    async def diary_list(
        self, since: str | None = None, until: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> dict:
        """List diary entries, newest first, optionally within a date range.

        ``since``/``until`` window the date-only ``date`` column — the kind's
        time axis, and the same column ``cabinet_search(kind="diary")`` windows,
        so the two answer a range the same way instead of one of them reaching
        for ``created_at``.  Reduced to ``YYYY-MM-DD`` via
        :func:`~slife.timeutil.normalize_time_bound`.

        Returns ``{"entries": [...], "total": n}`` (total counts every row in
        the range, before ``limit``/``offset``).
        """
        limit = _clamp_limit(limit)
        offset = max(0, offset)
        clauses, params = _list_window(_KIND_SPECS["diary"], since, until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = await self._c.execute(
            f"SELECT COUNT(*) FROM diary {where}", params,
        )
        row = await cursor.fetchone()
        total = row[0] if row else 0
        cursor = await self._c.execute(
            f"SELECT id, date, tags, file_path, created_at, updated_at "
            f"FROM diary {where} ORDER BY date DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        entries = [dict(row) for row in await cursor.fetchall()]
        return {"entries": entries, "total": total}

    async def report_list(
        self, task_id: int | None = None, since: str | None = None,
        until: str | None = None, limit: int = 50, offset: int = 0,
    ) -> dict:
        """List reports, newest first, optionally filtered by task and/or window.

        ``since``/``until`` window the kind's time axis (:data:`_KIND_SPECS`) —
        for a report, ``created_at``.  Note what that is NOT: a report's
        ``period_start``/``period_end`` say what it COVERS, which is a different
        dimension (and nullable), so they are not the axis.

        ``created_at`` is second-precision — the ``id DESC`` tiebreaker makes
        same-second inserts order deterministically (newest insert first)
        instead of leaving the tie to the database.

        Returns ``{"entries": [...], "total": n}``.
        """
        limit = _clamp_limit(limit)
        offset = max(0, offset)
        clauses: list[str] = []
        params: list[str] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(str(task_id))
        w_clauses, w_params = _list_window(_KIND_SPECS["report"], since, until)
        clauses += w_clauses
        params += w_params
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = await self._c.execute(
            f"SELECT COUNT(*) FROM reports {where}", params,
        )
        row = await cursor.fetchone()
        total = row[0] if row else 0
        cursor = await self._c.execute(
            f"SELECT id, task_id, title, tags, file_path, period_start, "
            f"period_end, created_at, updated_at "
            f"FROM reports {where} "
            f"ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        entries = [dict(row) for row in await cursor.fetchall()]
        return {"entries": entries, "total": total}

    async def get_report(self, report_id: int) -> dict | None:
        """Return one report (with full content), or None."""
        cursor = await self._c.execute(
            "SELECT id, task_id, title, content, tags, file_path, period_start, "
            "period_end, created_at, updated_at FROM reports WHERE id = ?",
            (report_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def file_list(
        self, category: str = "", since: str | None = None,
        until: str | None = None, limit: int = 50, offset: int = 0,
    ) -> dict:
        """List saved files, newest first, optionally filtered by category.

        ``since``/``until`` window the kind's time axis (:data:`_KIND_SPECS`) —
        for a file, ``created_at`` (a file is written once, never "touched").  The
        category filter and the window merge into one WHERE, and ``total`` counts
        their intersection.

        Returns ``{"entries": [...], "total": n}``.  Each entry carries the
        file's metadata (title, saved_path, category, mime, size, tags,
        summary, created_at) — not the binary content.
        """
        limit = _clamp_limit(limit)
        offset = max(0, offset)
        clauses: list[str] = []
        params: list[str] = []
        if category.strip():
            clauses.append("saved_path LIKE ?")
            params.append(f"files/{_slugify(category)}/%")
        w_clauses, w_params = _list_window(_KIND_SPECS["file"], since, until)
        clauses += w_clauses
        params += w_params
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = await self._c.execute(
            f"SELECT COUNT(*) FROM files {where}", params,
        )
        row = await cursor.fetchone()
        total = row[0] if row else 0
        cursor = await self._c.execute(
            f"SELECT id, title, original_path, saved_path, mime, size, tags, "
            f"summary, created_at FROM files {where} "
            f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        entries = []
        for row in await cursor.fetchall():
            r = dict(row)
            r["category"] = _category_from_path(r["saved_path"])
            entries.append(r)
        return {"entries": entries, "total": total}

    async def get_note(self, subject: str) -> dict | None:
        """Return one note by subject (with full content), or None."""
        cursor = await self._c.execute(
            "SELECT id, subject, content, tags, file_path, created_at, updated_at "
            "FROM notes WHERE subject = ?",
            (subject,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_diary(self, date: str) -> dict | None:
        """Return one day's diary (with full content), or None."""
        cursor = await self._c.execute(
            "SELECT id, date, content, tags, file_path, created_at, updated_at "
            "FROM diary WHERE date = ?",
            (date,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def _clear_kind_chunks(self, kind: str, doc_id: int) -> None:
        """Delete a document's vector chunks (marks it for re-embedding).

        No-op when no embedding backend is configured — the vec0 tables were
        not created (dim 0), so there is nothing to clear; when embedding is
        enabled later, the drainer embeds every unembedded document anyway.
        """
        if self._embedding_dim <= 0:
            return
        await self._c.execute(
            f"DELETE FROM {_KIND_SPECS[kind]['semantic']} WHERE doc_id = ?",
            (doc_id,),
        )

    # ── SemanticManager contract (unified document view) ───────────

    async def count_unembedded(self) -> int:
        # No vec0 tables when embedding is disabled (dim 0) — nothing can
        # be embedded, so the count is 0 (matches _clear_kind_chunks' guard).
        if self._embedding_dim <= 0:
            return 0
        total = 0
        for kind in _KIND_NAMES:
            spec = _KIND_SPECS[kind]
            where = "AND t.summary != ''" if kind == "file" else ""
            cursor = await self._c.execute(
                f"SELECT COUNT(*) FROM {spec['table']} t "
                f"WHERE t.id NOT IN (SELECT DISTINCT doc_id FROM {spec['semantic']}) "
                f"{where}",
            )
            row = await cursor.fetchone()
            total += row[0] if row else 0
        return total

    async def get_unembedded_docs(self, limit: int = 100) -> list[dict]:
        if self._embedding_dim <= 0:
            return []
        docs: list[dict] = []
        for kind in _KIND_NAMES:
            spec = _KIND_SPECS[kind]
            where = "AND t.summary != ''" if kind == "file" else ""
            cursor = await self._c.execute(
                f"SELECT t.id AS doc_id, t.{spec['text_col']} AS text, "
                f"t.{spec['key_col']} AS summary, t.tags, t.created_at "
                f"FROM {spec['table']} t "
                f"WHERE t.id NOT IN (SELECT DISTINCT doc_id FROM {spec['semantic']}) "
                f"{where} ORDER BY t.id LIMIT ?",
                (limit,),
            )
            for row in await cursor.fetchall():
                d = dict(row)
                d["kind"] = kind
                docs.append(d)
            if len(docs) >= limit:
                break
        return docs[:limit]

    async def replace_embedding_chunks(
        self, doc: dict, embeddings: list[list[float]],
    ) -> None:
        """Atomically replace a document's vector chunks (routed by kind).

        No-op when embedding is disabled (dim 0) — the vec0 tables were not
        created, so there is nothing to write.
        """
        if self._embedding_dim <= 0:
            return
        spec = _KIND_SPECS[doc["kind"]]
        doc_id = doc["doc_id"]
        summary = doc.get("summary", "")
        tags = doc.get("tags", "")
        created_at = doc.get("created_at", "")
        vec_blobs = [_serialize_f32(emb) for emb in embeddings]
        try:
            await self._c.execute(
                f"DELETE FROM {spec['semantic']} WHERE doc_id = ?", (doc_id,),
            )
            for idx, blob in enumerate(vec_blobs):
                await self._c.execute(
                    f"INSERT INTO {spec['semantic']} "
                    "(doc_embedding, doc_id, chunk_index, summary, tags, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (blob, doc_id, idx, summary, tags, created_at),
                )
            await self._c.commit()
        except Exception:
            await self._c.rollback()
            raise

    # ── search ─────────────────────────────────────────────────────

    async def search(
        self, query: str, kind: str = "all", limit: int = 20,
        mode: str = "hybrid", embed_query: list[float] | None = None,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """Hybrid (FTS5 + vec0, RRF), keyword, or regex search across kinds.

        ``mode="grep"`` is a real grep: the pattern is a Python regex and the
        match runs here (SQLite has no regexp engine).  An unusable pattern
        raises ``re.error`` for the caller to report.

        ``since``/``until`` window EACH KIND on its own time axis — the
        ``time_col`` in :data:`_KIND_SPECS`, the same column ``*_list`` windows
        and orders by.  So ``kind="all"`` reads one bound four ways, each the way
        that kind means it: a diary by its ``date`` (content, since the date is
        the key), a note by ``updated_at``, a file or report by ``created_at``.
        That is what makes ``cabinet_search(kind="diary", ...)`` and
        ``diary_list`` agree about "last month" instead of answering from two
        different columns.  An unusable bound raises ``InvalidTimeBound``.

        Each result carries ``id`` (``"note:5"`` etc.), ``file_path``, the
        kind's key/text, ``snippet`` and the RRF annotations from
        ``merge_hybrid``.
        """
        limit = _clamp_limit(limit)
        kinds = {
            "note": ["note"], "diary": ["diary"], "file": ["file"],
            "report": ["report"],
            "all": ["note", "diary", "file", "report"],
        }[kind]
        use_semantic = mode == "hybrid" and bool(embed_query)
        rx = re.compile(query) if mode == "grep" else None
        out: list[dict] = []
        for k in kinds:
            # Normalized per kind rather than once up front: the GRANULARITY is
            # the kind's (diary compares dates, a note datetimes), so one bound
            # has to be read through each kind's own axis.  A bound in no known
            # grammar still aborts the whole search — it raises on the first
            # kind, before any result is returned.
            k_since, k_until = _kind_window(_KIND_SPECS[k], since, until)
            key_hits = (
                await self._regex_search_kind(k, rx, limit, k_since, k_until)
                if rx is not None
                else await self._keyword_search_kind(
                    k, query, limit, k_since, k_until,
                )
            )
            sem_hits: list[dict] = []
            if use_semantic:
                assert embed_query is not None  # guaranteed by use_semantic
                sem_hits = await self._semantic_search_kind(
                    k, embed_query, limit, k_since, k_until,
                )
            out.extend(merge_hybrid(key_hits, sem_hits, key_field="id"))
        out.sort(key=lambda r: r.get("rrf_score", 0.0), reverse=True)
        return out[:limit]

    async def _keyword_search_kind(
        self, kind: str, query: str, limit: int,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        spec = _KIND_SPECS[kind]
        if _contains_cjk(query):
            return await self._like_search_kind(kind, query, limit, since, until)
        q = _to_fts5_query(query)
        # FTS5 carries no created_at, so the JOIN is what supplies the window
        # — memdb joins diary for the same reason.
        time_sql, time_params = _time_clause(
            since, until, f"t.{spec['time_col']}",
        )
        cursor = await self._c.execute(
            f"SELECT t.id, t.{spec['key_col']} AS key, "
            f"t.{spec['text_col']} AS text, t.tags, t.created_at, "
            f"t.{spec['file_col']} AS file_path, "
            f"snippet({spec['fts']}, {spec['snippet_col']}, '…', '…', '…', 40) AS snippet, "
            f"{spec['fts']}.rank AS rank "
            f"FROM {spec['fts']} JOIN {spec['table']} t ON t.id = {spec['fts']}.rowid "
            f"WHERE {spec['fts']} MATCH ?{time_sql} ORDER BY rank LIMIT ?",
            (q, *time_params, limit),
        )
        hits = []
        for row in await cursor.fetchall():
            r = dict(row)
            r["id"] = f"{spec['id_label']}:{r['id']}"
            hits.append(r)
        return hits

    async def _like_search_kind(
        self, kind: str, query: str, limit: int,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """CJK substring search — FTS5's unicode61 can't segment Chinese.

        The predicate is :func:`_like_terms`, the very one memdb's
        ``_search_like`` uses: split on whitespace, every word must appear in
        some column, ANDed.  One pattern for the whole query instead needs the
        words adjacent and in order — so ``"子agent 委托"`` missed a note holding
        the two words in different columns, while ``turn_recall`` answered with
        it.  Same query, two stores, two answers.
        """
        spec = _KIND_SPECS[kind]
        words = [w for w in query.split() if w]
        if not words:
            return []
        where, params = _like_terms(
            words, tuple(f"t.{c}" for c in spec["like_cols"]),
        )
        time_sql, time_params = _time_clause(
            since, until, f"t.{spec['time_col']}",
        )
        cursor = await self._c.execute(
            f"SELECT t.id, t.{spec['key_col']} AS key, "
            f"t.{spec['text_col']} AS text, t.tags, t.created_at, "
            f"t.{spec['file_col']} AS file_path "
            f"FROM {spec['table']} t WHERE {where}{time_sql} "
            f"ORDER BY t.id DESC LIMIT ?",
            (*params, *time_params, limit),
        )
        hits = []
        for row in await cursor.fetchall():
            r = dict(row)
            r["id"] = f"{spec['id_label']}:{r['id']}"
            text = r.get("text", "")
            r["snippet"] = text[:80] + ("…" if len(text) > 80 else "")
            hits.append(r)
        return hits

    async def _regex_search_kind(
        self, kind: str, rx: "re.Pattern", limit: int,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """REGEX search over the kind's text columns — a real ``grep``.

        SQLite has no regexp engine, so the match runs here: the kind's own
        row count is what bounds the scan (a cabinet is small, and the
        alternative — SQL ``LIKE`` — is what made ``grep`` a misnomer).  The
        time window still narrows in SQL, so it bounds the scan too — only the
        text predicate is left to Python.
        """
        spec = _KIND_SPECS[kind]
        time_sql, time_params = _time_clause(
            since, until, f"t.{spec['time_col']}",
        )
        cursor = await self._c.execute(
            f"SELECT t.id, t.{spec['key_col']} AS key, "
            f"t.{spec['text_col']} AS text, t.tags, t.created_at, "
            f"t.{spec['file_col']} AS file_path "
            f"FROM {spec['table']} t WHERE 1=1{time_sql} "
            f"ORDER BY t.id DESC",
            time_params,
        )
        hits: list[dict] = []
        for row in await cursor.fetchall():
            r = dict(row)
            m = (rx.search(str(r.get("key") or ""))
                 or rx.search(str(r.get("text") or "")))
            if m is None:
                continue
            r["id"] = f"{spec['id_label']}:{r['id']}"
            text = str(r.get("text") or "")
            start = max(0, m.start() - 40)
            r["snippet"] = text[start:start + 80] + ("…" if len(text) > start + 80 else "")
            r["rank"] = 0
            hits.append(r)
            if len(hits) >= limit:
                break
        return hits

    async def _semantic_search_kind(
        self, kind: str, embedding: list[float], limit: int,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        # No vec0 tables when embedding is disabled (dim 0) — hybrid search
        # degrades to keyword-only (search() keeps the FTS5 half).
        if self._embedding_dim <= 0:
            return []
        spec = _KIND_SPECS[kind]
        vec_blob = _serialize_f32(embedding)
        # Fetch extra rows to dedup multi-chunk documents (vec0 KNN no GROUP BY).
        #
        # With a time window, fetch a WIDER pool: vec0 KNN is global
        # nearest-neighbour and sqlite-vec forbids an auxiliary-column
        # constraint inside it, so the window cannot narrow the KNN — it is
        # applied below, in Python.  Without a wider pool the in-window hits
        # could all sit outside the KNN's top `limit*2` and the windowed
        # search would come back empty while matches existed.  memdb's
        # `search_semantic` widens by the same factor for the same reason.
        fetch_limit = (limit * 8) if (since or until) else (limit * 2)
        cursor = await self._c.execute(
            f"SELECT rowid, doc_id, summary, tags, created_at, distance "
            f"FROM {spec['semantic']} WHERE doc_embedding MATCH ? AND k = ? "
            f"ORDER BY distance",
            (vec_blob, fetch_limit),
        )
        seen: set[int] = set()
        hits: list[dict] = []
        for row in await cursor.fetchall():
            r = dict(row)
            rid = r["doc_id"]
            if rid in seen:
                continue
            seen.add(rid)
            r["id"] = f"{spec['id_label']}:{rid}"
            hits.append(r)
        # The window is applied on the metadata fetched below, NOT on the vec0
        # row — the vec0 table carries only `created_at`, while the window is on
        # the kind's OWN axis (`date` for diary, `updated_at` for a note).
        # Filtering the aux column here is exactly what would make a hybrid
        # diary search answer from `created_at` while its own keyword legs, in
        # the same call, answered from `date`.
        if hits:
            ids = [h["doc_id"] for h in hits]
            ph = in_placeholders(len(ids))
            cur = await self._c.execute(
                f"SELECT id, {spec['file_col']} AS file_path, "
                f"{spec['time_col']} AS time_value "
                f"FROM {spec['table']} WHERE id IN ({ph})",
                ids,
            )
            meta = {r["id"]: r for r in await cur.fetchall()}
            kept: list[dict] = []
            for h in hits:
                m = meta.get(h["doc_id"])
                h["file_path"] = m["file_path"] if m is not None else ""
                value = (m["time_value"] if m is not None else "") or ""
                if since and value < since:
                    continue
                if until and value > until:
                    continue
                kept.append(h)
            hits = kept[:limit]
        return hits

    # ── read / path safety ─────────────────────────────────────────

    def resolve_safe_path(self, relative: str) -> Path:
        """Resolve a ``{agent}.files``-relative path; refuse traversal escapes."""
        base = self._mem_dir.resolve()
        target = (base / relative).resolve()
        if not target.is_relative_to(base):
            raise ValueError(f"path escapes the files directory: {relative}")
        return target
