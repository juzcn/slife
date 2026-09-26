"""MemfilesStore — notes / diary / files / reports knowledge base with hybrid search.

Owns the memfiles SQLite index (``{agent}.files/.index.db``) and mirrors
note/diary/report content to human-readable markdown files under
``{agent}.files/``.

Four typed document tables — the writers' truth, each with its own columns:
  - ``notes`` — keyed by ``subject``; content mirrored to ``notes/<slug>.md``
  - ``diary``  — keyed by ``date``;  content mirrored to ``diary/<date>.md``
  - ``files``  — saved attachments (binary stays on the filesystem); an
    LLM-written ``summary`` is embedded for semantic search
  - ``reports`` — scheduled-task reports

and **ONE search index for all four** (``cabinet_fts`` + ``cabinet_semantic``),
reached through the ``cabinet_docs`` view.  That is the shape the turns database
has and the reason for it is in the schema file: a fusion consumes ranks, and a
rank only means something inside the corpus that produced it.  Four indexes made
four corpora, so a query spanning kinds had to answer with an order nothing had
measured.

The three legs over that one corpus (keyword / semantic / regex) are what the
store exposes; the *composition* that runs them — the mode dispatch, the clamp,
the single embed, the gate, the fusion and the hint — belongs to
``slife.plugins.memdb.search.run_search``, shared with ``turn_search`` so the
two plugins answer identically by construction.  :meth:`search_legs` hands them
over in the shape that composition takes.

Implements the SemanticManager "document source" contract
(``count_unembedded`` / ``get_unembedded_docs`` / ``replace_embedding_chunks`` /
``reconfigure_for_embedding``) over the one index, so the shared
``SemanticManager`` (memdb.semantic) drives the memfiles drainer.
Other code reuse is via memdb helpers: ``_serialize_f32``, ``_to_fts5_query``,
``_contains_cjk``, ``_like_terms``, ``in_placeholders`` — and the chunking and
embedding itself, which arrive through the shared ``SemanticManager``.
"""

import asyncio
import logging
import re
from functools import partial
from pathlib import Path

import aiosqlite

from slife.plugins.memdb.search import (
    GREP_SCAN_LIMIT,
    SearchLegs,
    _clamp_limit,
)
from slife.plugins.memdb.store import (
    DEFAULT_EMBEDDING_DIM,
    VecStoreLifecycleMixin,
    _contains_cjk,
    _is_fts_parse_error,
    _like_escape,
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

#: Identity of the text contract the cabinet's vectors are built from — the
#: ``body`` column of ``cabinet_docs``, which is a note/diary/report's own text
#: and, for a file, the summary that stands in for the content it has none of.
#: Stored vectors describe the text that was embedded, so the contract is part
#: of what makes them comparable, exactly as it is for turns
#: (``memdb.store.INDEX_TEXT_VERSION``): **bump it when the builder changes
#: shape**, or vectors built from text the builder no longer produces stay
#: beside the new ones and nothing in the numbers says so.
INDEX_TEXT_VERSION = "1"


def _slugify(text: str) -> str:
    """Turn arbitrary text into a safe filename slug — a *stem*, so with no
    dots: every caller appends an extension afterwards."""
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug.strip("-")[:120]


def _slugify_filename(text: str) -> str:
    """Turn arbitrary text into a safe filename stem, keeping its dots.

    ``_slugify`` drops ``.`` because its callers pass a subject or a title
    whose extension is appended afterwards; a *name* is the other case, and
    there the dot is part of the name: saving ``memfiles_test_doc.txt`` under
    a title of the same text wrote ``memfiles_test_doctxt.txt`` (2026-09-26),
    ``notes.v2`` losing the same way.  Legal in a filename, so kept — but a
    stem still may not begin or end with one, a leading dot hiding the file
    and a trailing dot not being a legal Windows name.
    """
    slug = re.sub(r"[^\w\s.-]", "", text.lower())
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug.strip("-. ")[:120]


def _name_to_stem(name: str, suffix: str) -> str:
    """*name* as a stem: its own extension dropped, the rest slugified.

    Callers hold a name that may or may not already carry the extension —
    ``memfiles_test_doc.txt`` against a ``.txt`` source, ``notes.v2`` against
    anything — while the extension is appended separately from the
    authoritative source (``src.suffix``).  Dropping *suffix* when the name
    ends in it, and only then, keeps the dot inside ``notes.v2`` while never
    writing ``paper.pdf.pdf``.
    """
    if suffix and name.lower().endswith(suffix.lower()):
        name = name[: -len(suffix)]
    return _slugify_filename(name)


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


#: Per-kind specs — what a kind is called, where its rows live, and its own
#: TIME AXIS.  The kinds' *searchable* text is not here: that is normalized
#: once, in the ``cabinet_docs`` view and the ``cabinet_fts`` triggers, which is
#: what makes the four kinds ONE corpus (see the schema file).
#:
#: ``time_col`` is the single column every time filter on that kind measures
#: against — the list window AND the search window, and the column the list is
#: ordered by.  It lives here, once per kind, because a bound that meant one
#: thing arriving through ``cabinet_search(kind="diary")`` and another through
#: ``diary_list`` is precisely how "last month" came to have two answers.
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
#: a timestamp column in datetime terms.  It applies to the ``*_list`` tools,
#: which window the column they order by.  A *search* windows the corpus's own
#: ``ts`` instead, which the view reads at a uniform datetime precision — one
#: corpus, one axis, so one bound narrows every kind the same way.
_KIND_SPECS = {
    "note": {
        "table": "notes",
        "key_col": "subject",       # what the read tools take as the key
        "time_col": "updated_at",
        "time_granularity": "datetime",
    },
    "diary": {
        "table": "diary",
        "key_col": "date",
        "time_col": "date",
        "time_granularity": "date",
    },
    "file": {
        "table": "files",
        "key_col": "saved_path",
        "time_col": "created_at",
        "time_granularity": "datetime",
    },
    "report": {
        "table": "reports",
        "key_col": "title",
        "time_col": "created_at",
        "time_granularity": "datetime",
    },
}

_KIND_NAMES = ("note", "diary", "file", "report")

#: The corpus's searchable text columns, in one spelling for the LIKE (CJK)
#: fallback: the union of what the four kinds' per-kind indexes used to hold
#: (a note's subject, a file's original path, …), normalized by the view.
_LIKE_COLS = ("title", "body", "tags", "source", "summary")

#: What a caller is told to call instead when it searches the cabinet with an
#: empty query — ``SearchLegs.browse``, which the shared refusal names.  Four
#: tools rather than one: the cabinet is browsed per kind, and a single
#: "cabinet_list" does not exist.
CABINET_BROWSE_TOOLS = "note_list / diary_list / file_list / report_list"


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

    A ``*_list`` query names its columns bare, may merge the window with another
    predicate (a category, a task id), and may have no WHERE at all — so it hands
    back clauses to join rather than a suffix to append.
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


def _normalize_hits(hits: list[dict]) -> None:
    """Rename each hit into the shape a caller reads, dropping the joins' keys.

    The legs carry ``doc_id`` and the corpus's ``ts`` because that is how they
    join the view; neither means anything outside this module, and the vec0
    legs additionally carry sqlite-vec's own ``rowid``.  A search result is a
    tool result — the model reads it — so the internal keys go and the
    model-facing names arrive here, once, for all three legs and both plugins'
    worth of callers.

    ``time`` rather than ``created_at``: the value is the row's position on its
    kind's own time axis (a note's last update, a diary's date, a file's save
    time), and calling it ``created_at`` would be false for a note.
    """
    for h in hits:
        h.pop("doc_id", None)
        h.pop("rowid", None)
        h.pop("source", None)
        h["time"] = h.pop("ts", "")


def _valid_kind(kind: str) -> str:
    """*kind* if the corpus has it, else a refusal naming the ones it does.

    One function because two layers need the answer and must give it the same
    way: the tool, which owns the parameter (and so refuses BEFORE it reaches
    a store at all), and :meth:`MemfilesStore.search_legs`, which cannot build
    a corpus from a kind it does not have.
    """
    if kind not in ("all", *_KIND_NAMES):
        raise ValueError(
            f"kind must be one of all/{'/'.join(_KIND_NAMES)} — got {kind!r}"
        )
    return kind


def _doc_window(
    since: str | None, until: str | None, kind: str,
) -> tuple[str, list[str]]:
    """The window a SEARCH runs inside, as an ``(sql, params)`` AND-suffix.

    One bound, one axis: the corpus's ``ts``, which the view reads at a uniform
    datetime precision.  A search has one corpus and therefore one axis — a
    per-kind axis here would be four bounds pretending to be one, and the reason
    the four kinds were merged in the first place was to stop answering one
    question four ways.

    *kind* narrows the corpus to one kind when the caller asked for one; the
    bound itself is normalized once, not per kind.
    """
    since = normalize_time_bound(since, role="since") if since else None
    until = normalize_time_bound(until, role="until") if until else None
    clauses: list[str] = []
    params: list[str] = []
    if kind != "all":
        clauses.append("d.kind = ?")
        params.append(kind)
    if since:
        clauses.append("d.ts >= ?")
        params.append(since)
    if until:
        clauses.append("d.ts <= ?")
        params.append(until)
    return "".join(f" AND {c}" for c in clauses), params


class MemfilesStore(VecStoreLifecycleMixin):
    """The memfiles index: four typed document tables, ONE search corpus."""

    #: ONE vec0 table for all four kinds — the cabinet is one corpus (see the
    #: schema file), so a model change migrates one table, not four.
    _semantic_tables: tuple[str, ...] = ("cabinet_semantic",)
    _index_text_version = INDEX_TEXT_VERSION
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
        #: Why sqlite-vec is not usable ("" once it loads).  Initialised here,
        #: not only in ``_load_vec_extension``: the normal startup path skips
        #: that call, and ``__check`` reads this whenever ``_vec_available`` is
        #: false — an unset attribute there is an AttributeError in the probe.
        self._vec_reason = ""
        # Serializes multi-statement read-modify-write writes (upsert_note /
        # upsert_diary / upsert_report): aiosqlite lets coroutines interleave
        # between awaited statements, so two concurrent upserts of the same
        # subject/date/title could both see "no row" (UNIQUE IntegrityError)
        # or drop one writer's appended section.  Same reason memdb carries
        # one — subagents share this plugin over HTTP, so the lock covers the
        # main agent + subagent writers in this one process.
        self._write_lock = asyncio.Lock()

    # ── lifecycle ─────────────────────────────────────────────────
    # ``reconfigure_for_embedding`` / ``_run_schema`` / ``_maybe_migrate_vec_tables``
    # come from VecStoreLifecycleMixin; ``setup`` is extended below.

    async def setup(
        self,
        embedding_dim: int = DEFAULT_EMBEDDING_DIM,
        embedding_model: str = "",
    ) -> None:
        """Establish the store, refusing a cabinet from before the one corpus.

        The check runs BEFORE the schema because the schema is what fails: it
        creates a view over columns an old database does not have, and the
        error that surfaces is ``no such column: summary`` — which says nothing
        about what to do.  This says it instead.
        """
        await self._refuse_pre_one_corpus_db()
        await super().setup(embedding_dim, embedding_model)

    async def _refuse_pre_one_corpus_db(self) -> None:
        """Name a stale cabinet rather than let the schema fail cryptically.

        There is no migration layer (a deliberate project rule), so an old
        database is deleted and rebuilt.  That is NOT done for you here: the
        markdown mirrors and the saved files survive a rebuild, but the
        scheduled tasks and their run history live in this database and nowhere
        else — so the deletion is the user's, and this names it precisely,
        including what would be lost.
        """
        if not self._db_path.exists():
            return
        conn = await aiosqlite.connect(str(self._db_path))
        try:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='notes'",
            )
            if await cursor.fetchone() is None:
                return  # empty or brand-new — the schema will build it
            cursor = await conn.execute("PRAGMA table_info(notes)")
            columns = {row[1] for row in await cursor.fetchall()}
        finally:
            await conn.close()
        if "summary" in columns:
            return
        raise RuntimeError(
            f"this cabinet predates the one-corpus index (its notes have no "
            f"summary column) and there is no migration layer — delete "
            f"{self._db_path} and it rebuilds on the next start. The notes, "
            f"diary and reports are mirrored as markdown and the saved files "
            f"are on disk, so only the scheduled tasks and their run history "
            f"would be lost: copy those out first if you need them."
        )

    @property
    def _c(self):
        assert self._conn is not None
        return self._conn

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
                await self._clear_doc_chunks("note", doc_id)
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
                await self._clear_doc_chunks("diary", row["id"])
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
                await self._clear_doc_chunks("report", doc_id)
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
            f"FROM notes {where} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
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
            f"FROM diary {where} ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
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
            # ``_slugify`` keeps ``_`` (it is a word character), and ``_`` is a
            # LIKE wildcard that would match any single character — so
            # ``category="my_docs"`` also listed the files under ``my-docs``.
            # Escaped like every other LIKE predicate here.
            clauses.append("saved_path LIKE ? ESCAPE '\\'")
            params.append(f"files/{_like_escape(_slugify(category))}/%")
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
            f"ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
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

    async def _clear_doc_chunks(self, kind: str, doc_id: int) -> None:
        """Delete a document's vector chunks (marks it for re-embedding).

        The cabinet's one semantic table keys on (kind, doc_id) — a bare
        doc_id is not unique across kinds, every one of which counts from 1.

        No-op when no embedding backend is configured — the vec0 table was
        not created (dim 0), so there is nothing to clear; when embedding is
        enabled later, the drainer embeds every unembedded document anyway.
        """
        if self._embedding_dim <= 0:
            return
        await self._c.execute(
            "DELETE FROM cabinet_semantic WHERE kind = ? AND doc_id = ?",
            (kind, doc_id),
        )

    # ── writing an annotation onto a saved row ─────────────────────

    async def summarize(
        self, kind: str, key: str, summary: str | None = None,
        tags: str | None = None,
    ) -> dict:
        """Write a saved row's summary and/or tags, and re-queue it to be embedded.

        The counterpart of memdb's ``turn_summarize``: annotate a row that is
        already saved, so it becomes findable — by **keyword** search, which is
        what a summary is for on either side (a turn's vector is built from the
        conversation, not from its summary).

        One kind is the exception, and it is the exception memdb does not have:
        a file has no text of its own, so its summary IS the text its vector is
        built from.  Writing one therefore re-queues that row's embedding, and
        writing one for a file saved without a summary is how such a file
        enters semantic search at all.

        ``None`` means "leave alone" for both fields, so an empty string is a
        real value (it clears).  The kind's FTS row follows from the table's
        ``AFTER UPDATE`` trigger either way.

        The row's own timestamp is left alone: an annotation is not an edit of
        the document, and bumping ``updated_at`` would jump a note to the top
        of ``note_list`` for it.
        """
        if kind not in _KIND_NAMES:
            raise ValueError(
                f"kind must be one of {'/'.join(_KIND_NAMES)} — got {kind!r}"
            )
        if summary is None and tags is None:
            raise ValueError("nothing to write — pass summary and/or tags")
        row = await self._row_by_key(kind, key)
        if row is None:
            raise ValueError(f"{kind} not found — {key}")

        sets: list[str] = []
        params: list[object] = []
        if summary is not None:
            sets.append("summary = ?")
            params.append(summary)
        if tags is not None:
            sets.append("tags = ?")
            params.append(tags)
        params.append(row["id"])
        async with self._write_lock:
            await self._c.execute(
                f"UPDATE {_KIND_SPECS[kind]['table']} "
                f"SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            if summary is not None and kind == "file":
                # A file's vector is built from its body, which includes the
                # summary, so the new text makes the old vector stale —
                # dropping the chunks is what puts the row back in the
                # drainer's unembedded queue (the same path a re-save takes).
                # For every other kind the vector is built from the content,
                # which this did not touch.
                await self._clear_doc_chunks(kind, row["id"])
            await self._c.commit()
        return {
            "kind": kind, "id": row["id"], "key": key,
            "tags": row["tags"] if tags is None else tags,
            "summary": row.get("summary", "") if summary is None else summary,
            "reembedded": summary is not None and kind == "file",
        }

    async def _row_by_key(self, kind: str, key: str) -> dict | None:
        """The row a kind's key names, or None — the one key→row mapping.

        ``report`` keys on its integer id (what ``report_list`` shows and
        ``report_read`` takes); every other kind keys on its text key column.
        """
        spec = _KIND_SPECS[kind]
        if kind == "report":
            try:
                value: object = int(key)
            except (TypeError, ValueError):
                raise ValueError(
                    f"report key must be the numeric report_id — got {key!r}"
                ) from None
            where = "id = ?"
        else:
            value = key
            where = f"{spec['key_col']} = ?"
        cursor = await self._c.execute(
            f"SELECT id, * FROM {spec['table']} WHERE {where}", (value,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ── SemanticManager contract (unified document view) ───────────

    async def count_unembedded(self) -> int:
        # No vec0 table when embedding is disabled (dim 0) — nothing can
        # be embedded, so the count is 0 (matches _clear_doc_chunks' guard).
        if self._embedding_dim <= 0:
            return 0
        # The vector is built from ``body``: a note/diary/report's own text, and
        # for a file the text that stands in for the content it has none of —
        # its title, source path and saved path, plus the summary once one is
        # written (see the view in the schema file).  So a file is embeddable
        # from the moment it is saved, summary or not; writing one re-queues it
        # with the richer text.  The counting route and the reading route below
        # must share this rule or the gate opens on a row the drainer cannot
        # embed.
        cursor = await self._c.execute(
            "SELECT COUNT(*) FROM cabinet_docs d "
            "WHERE d.body != '' "
            "AND d.id NOT IN (SELECT kind || ':' || doc_id FROM cabinet_semantic)",
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_unembedded_docs(self, limit: int = 100) -> list[dict]:
        if self._embedding_dim <= 0:
            return []
        # ``text`` is what gets embedded: ``body``, the same column the rule
        # above counts on, so this route and the count cannot disagree.  The
        # aux columns are memdb's shape, carrying the row's own summary and
        # tags; a hit's key and display fields come from the view join the
        # semantic leg already does, so nothing has to be smuggled through here.
        cursor = await self._c.execute(
            "SELECT d.kind, d.doc_id, d.body AS text, d.summary, "
            "       d.tags, d.ts AS created_at "
            "FROM cabinet_docs d "
            "WHERE d.body != '' "
            "AND d.id NOT IN (SELECT kind || ':' || doc_id FROM cabinet_semantic) "
            "ORDER BY d.kind, d.doc_id LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def replace_embedding_chunks(
        self, doc: dict, embeddings: list[list[float]],
    ) -> None:
        """Atomically replace a document's vector chunks.

        No-op when embedding is disabled (dim 0) — the vec0 table was not
        created, so there is nothing to write.
        """
        if self._embedding_dim <= 0:
            return
        kind = doc["kind"]
        doc_id = doc["doc_id"]
        summary = doc.get("summary", "")
        tags = doc.get("tags", "")
        created_at = doc.get("created_at", "")
        vec_blobs = [_serialize_f32(emb) for emb in embeddings]
        # The store's write lock, as memdb's twin takes it: the DELETE and the
        # INSERTs below share ONE connection with every other writer here, so
        # an interleaved commit from a concurrent write could land between them
        # — leaving the document half-indexed exactly as the rollback below
        # exists to prevent.  The rollback stays INSIDE the lock: releasing it
        # first would let the next writer commit the very statements this path
        # is undoing.
        async with self._write_lock:
            try:
                await self._c.execute(
                    "DELETE FROM cabinet_semantic WHERE kind = ? AND doc_id = ?",
                    (kind, doc_id),
                )
                for idx, blob in enumerate(vec_blobs):
                    await self._c.execute(
                        "INSERT INTO cabinet_semantic (doc_embedding, kind, doc_id, "
                        "chunk_index, summary, tags, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (blob, kind, doc_id, idx, summary, tags, created_at),
                    )
                await self._c.commit()
            except Exception:
                await self._c.rollback()
                raise

    # ── search: the three legs over the one corpus ─────────────────

    def search_legs(self, kind: str = "all") -> SearchLegs:
        """The cabinet, in the shape the shared composition takes.

        One corpus, so ONE :class:`SearchLegs` — not one per kind.  *kind*
        narrows it to a single kind when a caller asked for one, which is a
        filter on the corpus and not a different corpus.
        """
        kind = _valid_kind(kind)
        return SearchLegs(
            keyword=partial(self.keyword_hits, kind=kind),
            semantic=partial(self.semantic_hits, kind=kind),
            regex=partial(self.regex_hits, kind=kind),
            key_field="id",
            normalize=_normalize_hits,
            noun="entries",
            browse=CABINET_BROWSE_TOOLS,
        )

    async def keyword_hits(
        self, *, query: str, limit: int, since: str | None = None,
        until: str | None = None, kind: str = "all",
    ) -> list[dict]:
        """Ranked keyword hits over the cabinet, best first.

        FTS5 over the one index; CJK routes to a substring scan because the
        standard tokenizer makes a contiguous CJK run one token, so a Chinese
        word is an exact-token lookup (memdb's store records the measurement).
        Both ways AND their terms and both read the same corpus, so a hit means
        the same thing in either.
        """
        limit = _clamp_limit(limit)
        where, params = _doc_window(since, until, kind)

        if _contains_cjk(query):
            # A substring has no relevance to rank by, so this leg is newest
            # first — the same constant-rank shape memdb's LIKE fallback has.
            words = [w for w in query.split() if w]
            if not words:
                return []
            clause, like_params = _like_terms(words, _LIKE_COLS)
            cursor = await self._c.execute(
                f"""SELECT d.kind, d.doc_id, d.id, d.key, d.title, d.body, d.summary, d.tags,
                           d.source, d.file_path, d.ts,
                           substr(d.body, 1, 80) AS snippet, 0 AS rank
                    FROM cabinet_docs d WHERE {clause}{where}
                    ORDER BY d.ts DESC LIMIT ?""",
                (*like_params, *params, limit),
            )
            return [dict(r) for r in await cursor.fetchall()]

        try:
            cursor = await self._c.execute(
                f"""SELECT d.kind, d.doc_id, d.id, d.key, d.title, d.body, d.summary, d.tags,
                           d.source, d.file_path, d.ts,
                           snippet(cabinet_fts, -1, '…', '…', '…', 40) AS snippet,
                           f.rank
                    FROM cabinet_fts f
                    JOIN cabinet_docs d
                      ON d.kind = f.kind AND d.doc_id = f.doc_id
                    WHERE cabinet_fts MATCH ?{where}
                    ORDER BY f.rank LIMIT ?""",
                (_to_fts5_query(query), *params, limit),
            )
            return [dict(r) for r in await cursor.fetchall()]
        except aiosqlite.OperationalError as e:
            # A MATCH the parser rejects is an empty result, not a failure: the
            # query reached us as text and nothing is wrong with the store.
            # Any other OperationalError is a store failure and propagates, so
            # this leg and memdb's cannot answer the same failure differently
            # (the shared discriminator; DESIGN.md Appendix A 4).
            if not _is_fts_parse_error(e):
                raise
            return []

    async def semantic_hits(
        self, *, embedding: list[float], limit: int,
        since: str | None = None, until: str | None = None, kind: str = "all",
    ) -> list[dict]:
        """Vector hits over the cabinet, nearest first.

        ``limit`` counts DOCUMENTS: a long text is several chunks, so the KNN
        is asked for a wider pool and deduped by (kind, doc_id), keeping each
        document's closest chunk.

        With a time window the pool is wider still, because vec0 KNN is global
        nearest-neighbour and sqlite-vec forbids an auxiliary-column constraint
        inside it — so the window cannot narrow the KNN and is applied below.
        Without the wider pool the in-window hits could all sit outside the
        KNN's top ``limit*2`` and a windowed search would come back empty while
        matches existed.  memdb's ``search_semantic`` widens by the same factor
        for the same reason.
        """
        if self._embedding_dim <= 0:
            return []
        limit = _clamp_limit(limit)
        fetch_limit = (limit * 8) if (since or until) else (limit * 2)
        cursor = await self._c.execute(
            "SELECT kind, doc_id, distance FROM cabinet_semantic "
            "WHERE doc_embedding MATCH ? AND k = ? ORDER BY distance",
            (_serialize_f32(embedding), fetch_limit),
        )
        best: dict[str, dict] = {}
        for row in await cursor.fetchall():
            r = dict(row)
            key = f"{r['kind']}:{r['doc_id']}"
            if key not in best:
                best[key] = r
        if not best:
            return []

        ids = list(best)
        where, params = _doc_window(since, until, kind)
        cur = await self._c.execute(
            f"""SELECT d.kind, d.doc_id, d.id, d.key, d.title, d.body, d.summary, d.tags,
                       d.source, d.file_path, d.ts, d.body AS snippet
                FROM cabinet_docs d
                WHERE d.id IN ({in_placeholders(len(ids))}){where}""",
            (*ids, *params),
        )
        hits: list[dict] = []
        for row in await cur.fetchall():
            r = dict(row)
            r["distance"] = best[r["id"]]["distance"]
            hits.append(r)
        hits.sort(key=lambda h: h["distance"])
        return hits[:limit]

    async def regex_hits(
        self, *, pattern: str, limit: int, since: str | None = None,
        until: str | None = None, kind: str = "all",
    ) -> list[dict]:
        """Regex hits over the cabinet, newest first.

        A real ``grep``: the pattern is a Python regex and the match runs here,
        because SQLite has no regexp engine.

        It reads a row's **title, its path and its text** — and not its tags
        (those are the keyword leg's) or its summary, which is the one column a
        model wrote rather than the document.  That is the cabinet's answer to
        the question memdb answers with "a turn's messages, not its summary and
        tags": a cabinet row has a name and a place on disk as well as a text,
        and a grep is how you find a row by either.

        An unusable pattern raises ``re.error`` for the caller to report.  Rows
        are examined newest-first up to :data:`GREP_SCAN_LIMIT`, so the worst
        case stays bounded on a long-lived cabinet.
        """
        limit = _clamp_limit(limit)
        rx = re.compile(pattern)
        where, params = _doc_window(since, until, kind)
        cursor = await self._c.execute(
            f"""SELECT d.kind, d.doc_id, d.id, d.key, d.title, d.body, d.summary, d.tags,
                       d.source, d.file_path, d.ts
                FROM cabinet_docs d WHERE 1=1{where}
                ORDER BY d.ts DESC LIMIT ?""",
            (*params, GREP_SCAN_LIMIT),
        )
        hits: list[dict] = []
        for row in await cursor.fetchall():
            r = dict(row)
            # The four the pattern reads, in the order a hit should be
            # explained by: the row's text first, then its name, then where it
            # lives.  The snippet comes from whichever one matched.
            for field in ("body", "title", "file_path", "source"):
                text = r.get(field) or ""
                match = rx.search(text)
                if match is None:
                    continue
                start = max(0, match.start() - 40)
                r["snippet"] = text[start:start + 160]
                break
            else:
                continue
            r["rank"] = 0
            hits.append(r)
            if len(hits) >= limit:
                break
        return hits

    # ── read / path safety ─────────────────────────────────────────

    def resolve_safe_path(self, relative: str) -> Path:
        """Resolve a ``{agent}.files``-relative path; refuse traversal escapes."""
        base = self._mem_dir.resolve()
        target = (base / relative).resolve()
        if not target.is_relative_to(base):
            raise ValueError(f"path escapes the files directory: {relative}")
        return target
