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
}
_KIND_NAMES = ("note", "diary", "file")


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
            "all": ["note", "diary", "file"],
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
