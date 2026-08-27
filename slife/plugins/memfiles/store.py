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
``_serialize_f32``, ``_to_fts5_query``, ``_contains_cjk``, ``merge_hybrid``.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

import aiosqlite

from slife.plugins.memdb.search import merge_hybrid
from slife.plugins.memdb.store import (
    _clamp_limit,
    _contains_cjk,
    _serialize_f32,
    _split_sql,
    _to_fts5_query,
)

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_DIM = 1536


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


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
    },
}
_KIND_NAMES = ("note", "diary", "file", "report")


class MemfilesStore:
    """The memfiles index: three typed document tables + hybrid search."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._mem_dir = db_path.parent          # .index.db lives in {agent}.files/
        self._conn: aiosqlite.Connection | None = None
        self._embedding_dim = DEFAULT_EMBEDDING_DIM
        self._embedding_model = ""
        self._vec_available = False             # sqlite-vec loaded? embeddings optional

    # ── lifecycle ─────────────────────────────────────────────────

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

    async def setup(
        self,
        embedding_dim: int = DEFAULT_EMBEDDING_DIM,
        embedding_model: str = "",
    ) -> None:
        self._embedding_dim = embedding_dim
        self._embedding_model = embedding_model
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._load_vec_extension()
        if not self._vec_available:
            self._embedding_dim = 0
        await self._run_schema()
        logger.info(
            "memfiles_store_ready path=%s wal=on vec_dim=%d model=%s",
            self._db_path, self._embedding_dim, embedding_model or "none",
        )

    async def reconfigure_for_embedding(
        self, embedding_dim: int, embedding_model: str = "",
    ) -> None:
        if self._conn is None:
            await self.setup(
                embedding_dim=embedding_dim, embedding_model=embedding_model,
            )
            return
        self._embedding_dim = embedding_dim
        self._embedding_model = embedding_model
        if not self._vec_available:
            await self._load_vec_extension()
            if not self._vec_available:
                self._embedding_dim = 0
        await self._run_schema()

    async def _load_vec_extension(self) -> None:
        try:
            import sqlite_vec
            await self._c.enable_load_extension(True)
            await self._c.load_extension(sqlite_vec.loadable_path())
            await self._c.enable_load_extension(False)
            await self._c.execute("SELECT vec_version()")
            self._vec_available = True
            logger.info("memfiles_vec_loaded")
        except Exception as e:
            self._vec_available = False
            logger.warning("memfiles_vec_unavailable err=%s", e)

    async def _run_schema(self) -> None:
        schema_path = Path(__file__).parent / "schema.sql"
        schema_sql = schema_path.read_text(encoding="utf-8")
        schema_sql = schema_sql.replace("float[1536]", f"float[{self._embedding_dim}]")
        for stmt in _split_sql(schema_sql):
            stmt = stmt.strip()
            if not stmt:
                continue
            # Skip only actual vec0 CREATE statements, not any fragment whose
            # leading comment merely mentions "vec0" (the header comment does).
            if (
                self._embedding_dim <= 0
                and "CREATE VIRTUAL TABLE" in stmt
                and "vec0" in stmt
            ):
                continue
            try:
                await self._c.execute(stmt)
            except Exception as e:
                logger.debug("memfiles_schema_stmt_error err=%s stmt=%.80s", e, stmt)
        await self._c.commit()
        await self._maybe_migrate_vec_dimension()

    async def _maybe_migrate_vec_dimension(self) -> None:
        """Drop + recreate the vec0 tables when dim/model changed (mirrors memdb).

        ``CREATE TABLE IF NOT EXISTS`` won't resize an existing vec0 table; a
        model/dimension change makes old vectors invalid (different vector
        space), so the tables are dropped and the drainer rebuilds them.
        """
        if self._embedding_dim <= 0:
            return
        cursor = await self._c.execute(
            "SELECT value FROM meta WHERE key = 'embedding_model'",
        )
        row = await cursor.fetchone()
        stored_model = row[0] if (row and isinstance(row[0], str)) else ""
        model_identity = self._embedding_model or ""

        migrated = False
        for kind in _KIND_NAMES:
            sem = _KIND_SPECS[kind]["semantic"]
            cursor = await self._c.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (sem,),
            )
            row = await cursor.fetchone()
            create_sql = row[0] if (row and row[0] and isinstance(row[0], str)) else ""
            existing_dim = 0
            if create_sql:
                m = re.search(r"float\[(\d+)\]", create_sql)
                if m:
                    existing_dim = int(m.group(1))
            dim_changed = existing_dim and existing_dim != self._embedding_dim
            model_changed = (
                model_identity and stored_model and stored_model != model_identity
            )
            if dim_changed or model_changed or not create_sql:
                logger.info("memfiles_vec_migrate table=%s dim=%s→%s model=%s→%s",
                            sem, existing_dim, self._embedding_dim,
                            stored_model, model_identity)
                await self._c.execute(f"DROP TABLE IF EXISTS {sem}")
                migrated = True
        if migrated:
            await self._c.commit()
            await self._run_schema_recreate_vec()

        if model_identity and model_identity != stored_model:
            await self._c.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('embedding_model', ?)",
                (model_identity,),
            )
            await self._c.commit()

    async def _run_schema_recreate_vec(self) -> None:
        """Re-create only the vec0 tables after a dimension migration."""
        schema_path = Path(__file__).parent / "schema.sql"
        schema_sql = schema_path.read_text(encoding="utf-8")
        schema_sql = schema_sql.replace("float[1536]", f"float[{self._embedding_dim}]")
        for stmt in _split_sql(schema_sql):
            stmt = stmt.strip()
            if not stmt or "CREATE VIRTUAL TABLE" not in stmt or "vec0" not in stmt:
                continue
            try:
                await self._c.execute(stmt)
            except Exception as e:
                logger.debug("memfiles_vec_recreate_error err=%s stmt=%.80s", e, stmt)
        await self._c.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ── document writes (md mirrored) ──────────────────────────────

    async def upsert_note(self, subject: str, content: str, tags: str) -> dict:
        """Append a timestamped section to the subject's note (md + DB row)."""
        if not subject.strip() or not content.strip():
            raise ValueError("subject and content are required")
        slug = _slugify(subject) or "note"
        rel = f"notes/{slug}.md"
        abs_path = self._mem_dir / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        now = _now()
        body = content.strip()
        if abs_path.exists():
            existing = abs_path.read_text(encoding="utf-8").rstrip()
            new_md = f"{existing}\n\n## {now}\n\n{body}\n"
        else:
            new_md = f"# {subject}\n\n{body}\n"
        abs_path.write_text(new_md, encoding="utf-8")

        cursor = await self._c.execute(
            "SELECT id FROM notes WHERE subject = ?", (subject,),
        )
        row = await cursor.fetchone()
        if row:
            await self._c.execute(
                "UPDATE notes SET content=?, tags=?, file_path=?, updated_at=? "
                "WHERE subject=?",
                (new_md, tags, rel, now, subject),
            )
            await self._clear_kind_chunks("note", row["id"])
            doc_id = row["id"]
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
        """Append a timestamped section to a day's diary (md + DB row)."""
        if not content.strip():
            raise ValueError("content is required")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError(f"date must be YYYY-MM-DD, got {date!r}")
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
        self, task_id: int, title: str, content: str, tags: str = "",
        period_start: str | None = None, period_end: str | None = None,
        due_at: str | None = None,
    ) -> dict:
        """Save a scheduled-task report (md + DB row + run backfill).

        Writes the report to ``reports/<slug>.md`` and the DB, then confirms a
        ``scheduled_runs`` row: with *due_at* the exact run (a backfill of a
        missed/failed run, whose row was flipped to ``pending`` at dispatch);
        without it the newest un-reported run (the cron-fire dispatch).  The
        confirmed run goes ``pending → ran`` — the one success writeback.
        """
        if not content.strip():
            raise ValueError("content is required")
        if not title.strip():
            raise ValueError("title is required")
        slug = _slugify(title) or f"report-{task_id}"
        rel = f"reports/{slug}.md"
        abs_path = self._mem_dir / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        now = _now()
        body = content.strip()
        if abs_path.exists():
            # Same title → append a timestamped section (like diary).
            existing = abs_path.read_text(encoding="utf-8").rstrip()
            new_md = f"{existing}\n\n## {now}\n\n{body}\n"
        else:
            new_md = f"# {title}\n\n{body}\n"
        abs_path.write_text(new_md, encoding="utf-8")

        cursor = await self._c.execute(
            "SELECT id FROM reports WHERE file_path = ?", (rel,),
        )
        row = await cursor.fetchone()
        if row:
            await self._c.execute(
                "UPDATE reports SET content=?, tags=?, file_path=?, "
                "period_start=?, period_end=?, updated_at=? WHERE id=?",
                (new_md, tags, rel, period_start, period_end, now, row["id"]),
            )
            await self._clear_kind_chunks("report", row["id"])
            doc_id = row["id"]
        else:
            cursor = await self._c.execute(
                "INSERT INTO reports (task_id, title, content, tags, file_path, "
                "period_start, period_end, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, title, new_md, tags, rel, period_start, period_end, now, now),
            )
            doc_id = cursor.lastrowid

        # Store-layer backfill: confirm the run this report is *for*, pending →
        # ran here (the ONLY 'ran' write).  A due_at targets the exact run —
        # the backfilled failed/missed run — so a newer stale run is never
        # grabbed; without one it links the newest un-linked run (cron fire).
        if due_at is not None:
            await self._c.execute(
                "UPDATE scheduled_runs SET report_id=?, status='ran' "
                "WHERE task_id=? AND due_at=? AND report_id IS NULL",
                (doc_id, task_id, due_at),
            )
        else:
            await self._c.execute(
                "UPDATE scheduled_runs SET report_id=?, status='ran' WHERE id = ("
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

    async def list_scheduled_tasks(self, enabled_only: bool = False) -> list[dict]:
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

    async def mark_run_skipped(self, task_id: int, due_at: str) -> None:
        """Close a missed/failed run the user decided not to backfill."""
        await self._c.execute(
            "UPDATE scheduled_runs SET status='skipped' "
            "WHERE task_id=? AND due_at=? AND status IN ('missed', 'failed')",
            (task_id, due_at),
        )
        await self._c.commit()

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

    async def list_scheduled_runs(
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

    async def list_notes(self, limit: int = 50, offset: int = 0) -> dict:
        """List notes, newest-updated first.  Lightweight — no content.

        Returns ``{"entries": [...], "total": n}`` so the caller knows how
        many more remain beyond this page (``offset + len(entries) < total``).
        """
        limit = _clamp_limit(limit)
        offset = max(0, offset)
        cursor = await self._c.execute("SELECT COUNT(*) FROM notes")
        row = await cursor.fetchone()
        total = row[0] if row else 0
        cursor = await self._c.execute(
            "SELECT id, subject, tags, file_path, created_at, updated_at "
            "FROM notes ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        entries = [dict(row) for row in await cursor.fetchall()]
        return {"entries": entries, "total": total}

    async def list_diary(
        self, since: str | None = None, until: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> dict:
        """List diary entries, newest first, optionally within a date range.

        Returns ``{"entries": [...], "total": n}`` (total counts every row in
        the range, before ``limit``/``offset``).
        """
        limit = _clamp_limit(limit)
        offset = max(0, offset)
        clauses: list[str] = []
        params: list[str] = []
        if since:
            clauses.append("date >= ?")
            params.append(since)
        if until:
            clauses.append("date <= ?")
            params.append(until)
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

    async def list_reports(
        self, task_id: int | None = None, limit: int = 50, offset: int = 0,
    ) -> dict:
        """List reports, newest first, optionally filtered by task.

        Returns ``{"entries": [...], "total": n}``.
        """
        limit = _clamp_limit(limit)
        offset = max(0, offset)
        where = ""
        params: list[str] = []
        if task_id is not None:
            where = "WHERE task_id = ?"
            params.append(str(task_id))
        cursor = await self._c.execute(
            f"SELECT COUNT(*) FROM reports {where}", params,
        )
        row = await cursor.fetchone()
        total = row[0] if row else 0
        cursor = await self._c.execute(
            f"SELECT id, task_id, title, tags, file_path, period_start, "
            f"period_end, created_at, updated_at "
            f"FROM reports {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
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

    async def list_files(
        self, category: str = "", limit: int = 50, offset: int = 0,
    ) -> dict:
        """List saved files, newest first, optionally filtered by category.

        Returns ``{"entries": [...], "total": n}``.  Each entry carries the
        file's metadata (title, saved_path, category, mime, size, tags,
        summary, created_at) — not the binary content.
        """
        limit = _clamp_limit(limit)
        offset = max(0, offset)
        where = ""
        params: list[str] = []
        if category.strip():
            where = "WHERE saved_path LIKE ?"
            params.append(f"files/{_slugify(category)}/%")
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
    ) -> list[dict]:
        """Hybrid (FTS5 + vec0, RRF) or keyword search across selected kinds.

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
        out: list[dict] = []
        for k in kinds:
            key_hits = await self._keyword_search_kind(k, query, limit)
            sem_hits: list[dict] = []
            if use_semantic:
                assert embed_query is not None  # guaranteed by use_semantic
                sem_hits = await self._semantic_search_kind(k, embed_query, limit)
            out.extend(merge_hybrid(key_hits, sem_hits, key_field="id"))
        out.sort(key=lambda r: r.get("rrf_score", 0.0), reverse=True)
        return out[:limit]

    async def _keyword_search_kind(
        self, kind: str, query: str, limit: int,
    ) -> list[dict]:
        spec = _KIND_SPECS[kind]
        if _contains_cjk(query):
            return await self._like_search_kind(kind, query, limit)
        q = _to_fts5_query(query)
        cursor = await self._c.execute(
            f"SELECT t.id, t.{spec['key_col']} AS key, "
            f"t.{spec['text_col']} AS text, t.tags, t.created_at, "
            f"t.{spec['file_col']} AS file_path, "
            f"snippet({spec['fts']}, {spec['snippet_col']}, '…', '…', '…', 40) AS snippet, "
            f"{spec['fts']}.rank AS rank "
            f"FROM {spec['fts']} JOIN {spec['table']} t ON t.id = {spec['fts']}.rowid "
            f"WHERE {spec['fts']} MATCH ? ORDER BY rank LIMIT ?",
            (q, limit),
        )
        hits = []
        for row in await cursor.fetchall():
            r = dict(row)
            r["id"] = f"{spec['id_label']}:{r['id']}"
            hits.append(r)
        return hits

    async def _like_search_kind(
        self, kind: str, query: str, limit: int,
    ) -> list[dict]:
        """CJK substring search — FTS5's unicode61 can't segment Chinese."""
        spec = _KIND_SPECS[kind]
        safe = (
            query.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
        )
        pat = f"%{safe}%"
        like_clauses = " OR ".join(f"t.{c} LIKE ? ESCAPE '\\'" for c in spec["like_cols"])
        params: list[str] = [pat] * len(spec["like_cols"])
        cursor = await self._c.execute(
            f"SELECT t.id, t.{spec['key_col']} AS key, "
            f"t.{spec['text_col']} AS text, t.tags, t.created_at, "
            f"t.{spec['file_col']} AS file_path "
            f"FROM {spec['table']} t WHERE {like_clauses} "
            f"ORDER BY t.id DESC LIMIT ?",
            (*params, limit),
        )
        hits = []
        for row in await cursor.fetchall():
            r = dict(row)
            r["id"] = f"{spec['id_label']}:{r['id']}"
            text = r.get("text", "")
            r["snippet"] = text[:80] + ("…" if len(text) > 80 else "")
            hits.append(r)
        return hits

    async def _semantic_search_kind(
        self, kind: str, embedding: list[float], limit: int,
    ) -> list[dict]:
        # No vec0 tables when embedding is disabled (dim 0) — hybrid search
        # degrades to keyword-only (search() keeps the FTS5 half).
        if self._embedding_dim <= 0:
            return []
        spec = _KIND_SPECS[kind]
        vec_blob = _serialize_f32(embedding)
        # Fetch extra rows to dedup multi-chunk documents (vec0 KNN no GROUP BY).
        cursor = await self._c.execute(
            f"SELECT rowid, doc_id, summary, tags, created_at, distance "
            f"FROM {spec['semantic']} WHERE doc_embedding MATCH ? AND k = ? "
            f"ORDER BY distance",
            (vec_blob, limit * 2),
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
            if len(hits) >= limit:
                break
        if hits:
            ids = [h["doc_id"] for h in hits]
            ph = ",".join("?" * len(ids))
            cur = await self._c.execute(
                f"SELECT id, {spec['file_col']} AS file_path "
                f"FROM {spec['table']} WHERE id IN ({ph})",
                ids,
            )
            paths = {r["id"]: r["file_path"] for r in await cur.fetchall()}
            for h in hits:
                h["file_path"] = paths.get(h["doc_id"], "")
        return hits

    # ── read / path safety ─────────────────────────────────────────

    def resolve_safe_path(self, relative: str) -> Path:
        """Resolve a ``{agent}.files``-relative path; refuse traversal escapes."""
        base = self._mem_dir.resolve()
        target = (base / relative).resolve()
        if not target.is_relative_to(base):
            raise ValueError(f"path escapes the files directory: {relative}")
        return target
