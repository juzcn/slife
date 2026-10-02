#!/usr/bin/env python3
"""One-time migration: rename the memdb ``diary`` table to ``turn``.

The turns database's table was called ``diary``, which collided with the file
cabinet's *own* ``diary`` — the user's day journal (``diary_write``), a
completely different thing that happens to live in the same agent data
directory.  Everything else in memdb already says "turn": the tools are
``turn_search`` / ``turn_list`` / ``turn_read`` / ``turn_count`` /
``turn_summarize``, the sibling table is ``turn_channel``, and the columns list
is ``_TURN_COLUMNS``.  This script completes the rename for databases created
before it:

    uv run python scripts/migrate_memdb_diary_to_turn.py --all
    uv run python scripts/migrate_memdb_diary_to_turn.py --agent slife
    uv run python scripts/migrate_memdb_diary_to_turn.py --db ~/.slife/slife.db

Nothing migrates automatically.  The new code's ``CREATE TABLE IF NOT EXISTS``
makes an *empty* ``turn`` table beside the old ``diary`` one, so an unmigrated
database starts with its history ignored — the store logs a warning naming
this script.  Run it once per database to carry the turns and their embeddings
over; without it the old rows are simply not part of the live history.

What moves, in one transaction:

    diary            -> turn                 (rows AND rowids ride along)
    diary_fts        -> turn_fts             (rebuilt from the content table)
    diary_ai/ad/au   -> turn_ai/ad/au        (the FTS triggers)
    diary_semantic   -> turn_semantic        (+diary_rowid -> +turn_rowid,
                                              vectors copied byte for byte)
    diary_meta       -> turn_meta
    idx_diary_created-> idx_turn_created

The old triggers are dropped *before* the rename on purpose: a table rename
carries the triggers along and retargets them onto ``turn``, but their bodies
would still write ``diary_fts`` — the first insert after migration would fail
with "no such table: diary_fts".

Idempotent, and safe to re-run: a database with no ``diary`` table is left
alone.  If both tables hold rows the script refuses and reports both, because
merging two generations would mean renumbering rowids that ``turn_channel``,
the vector column and the live-context list all reference by number.

**Stop slife first.**  A running process holds the old table's name, so its
next save would fail once the rename lands.  The script does not try to detect
that — SQLite has no portable "another process has this open" probe, and the
``-wal``/``-shm`` files outlive an unclean exit, so their presence proves
nothing.
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path

# Allow running from anywhere: ``slife.paths`` must resolve regardless of the
# CWD (dev mode resolves the data dir against the repo root).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slife.paths import get_data_dir  # noqa: E402

MIGRATED = "migrated"
NORMALIZED = "normalized"        # the app's empty new tables were dropped first
ALREADY = "already"
NOT_MEMDB = "not_memdb"
CONFLICT = "conflict"
EMPTY_LEGACY = "empty_legacy"
VEC_MISSING = "vec_missing"

#: The pre-rename objects this script is responsible for retiring.
LEGACY_OBJECTS = (
    "diary", "diary_fts", "diary_semantic", "diary_meta", "idx_diary_created",
)

#: A backup is written here before anything is touched — the same ``*.bak``
#: name the repo already ignores (``slife.db.bak*``).
BACKUP_SUFFIX = ".bak"

VEC_COLUMNS = "rowid, turn_embedding, turn_rowid, chunk_index, summary, tags, created_at"
VEC_SOURCE_COLUMNS = "rowid, turn_embedding, diary_rowid, chunk_index, summary, tags, created_at"

TRIGGERS = """\
CREATE TRIGGER turn_ai AFTER INSERT ON turn BEGIN
    INSERT INTO turn_fts(rowid, user_message, messages, summary, tags, channel)
    VALUES (new.rowid, new.user_message, new.messages, new.summary, new.tags, new.channel);
END\
""", """\
CREATE TRIGGER turn_ad AFTER DELETE ON turn BEGIN
    INSERT INTO turn_fts(turn_fts, rowid, user_message, messages, summary, tags, channel)
    VALUES ('delete', old.rowid, old.user_message, old.messages, old.summary, old.tags, old.channel);
END\
""", """\
CREATE TRIGGER turn_au AFTER UPDATE ON turn BEGIN
    INSERT INTO turn_fts(turn_fts, rowid, user_message, messages, summary, tags, channel)
    VALUES ('delete', old.rowid, old.user_message, old.messages, old.summary, old.tags, old.channel);
    INSERT INTO turn_fts(rowid, user_message, messages, summary, tags, channel)
    VALUES (new.rowid, new.user_message, new.messages, new.summary, new.tags, new.channel);
END\
"""


class MigrationError(Exception):
    """A state this script refuses to guess its way through."""


def _has(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _span(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        f"SELECT MIN(created_at), MAX(created_at) FROM {table}",
    ).fetchone()
    return f"{row[0] or '?'} … {row[1] or '?'}"


def _load_vec(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec, so a vec0 table can be created or dropped at all."""
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        conn.load_extension(sqlite_vec.loadable_path())
        conn.enable_load_extension(False)
        conn.execute("SELECT vec_version()")
        return True
    except Exception:
        return False


def _vec_ddl(conn: sqlite3.Connection) -> str:
    """The ``turn_semantic`` DDL, at the OLD table's dimension and metric.

    The dimension is substituted into ``schema.sql`` at setup time from the
    configured embedding width, so it is never assumed — and the metric is
    carried over verbatim too: a table created before the schema declared
    ``distance_metric=cosine`` holds L2 vectors, and re-declaring them as
    cosine would silently misread every distance.  The app's own
    ``_maybe_migrate_vec_tables`` is what rebuilds on such a mismatch.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='diary_semantic'",
    ).fetchone()
    ddl = (row[0] if row else "") or ""
    dim = re.search(r"float\[(\d+)\]", ddl)
    if not dim:
        raise MigrationError(
            "cannot read the vector width from diary_semantic's DDL "
            f"(got {ddl[:80]!r}) — refusing to size the new table by guess",
        )
    metric = re.search(r"distance_metric\s*=\s*(\w+)", ddl, re.IGNORECASE)
    suffix = f" distance_metric={metric.group(1)}" if metric else ""
    return (
        "CREATE VIRTUAL TABLE turn_semantic USING vec0(\n"
        f"    turn_embedding float[{dim.group(1)}]{suffix},\n"
        "    +turn_rowid    INTEGER,\n"
        "    +chunk_index   INTEGER,\n"
        "    +summary       TEXT,\n"
        "    +tags          TEXT,\n"
        "    +created_at    TEXT\n"
        ")"
    )


def _statements(conn: sqlite3.Connection, *, has_new_tables: bool) -> list[str]:
    """The migration, in order.  Read-only helpers run before this."""
    has_old_vec = _has(conn, "diary_semantic")
    has_meta = _has(conn, "diary_meta")
    stmts: list[str] = []

    # 0. A database the new code already opened has an empty ``turn``
    #    generation (and its satellites) beside the populated old one.  Drop
    #    that generation before moving the real data in: it holds no turns,
    #    and its meta rows describe vectors that do not exist yet.
    if has_new_tables:
        stmts += [
            "DROP TABLE IF EXISTS turn_fts",
            "DROP TABLE IF EXISTS turn_semantic",
            "DROP TABLE IF EXISTS turn_meta",
            "DROP TABLE IF EXISTS turn",
        ]

    # 1. Before the rename — see the module docstring.
    stmts += [
        "DROP TRIGGER IF EXISTS diary_ai",
        "DROP TRIGGER IF EXISTS diary_ad",
        "DROP TRIGGER IF EXISTS diary_au",
    ]

    # 2. Vectors, at the old width, copied row for row (they are the one
    #    thing here that cannot be recomputed without the embedding model).
    if has_old_vec:
        stmts += [
            _vec_ddl(conn),
            "INSERT INTO turn_semantic "
            f"({VEC_COLUMNS}) SELECT {VEC_SOURCE_COLUMNS} FROM diary_semantic",
            "DROP TABLE diary_semantic",
        ]

    # 3. The old keyword index, before the rename (an index keeps its name
    #    through a table rename, so it would survive as a stray).
    stmts += [
        "DROP TABLE IF EXISTS diary_fts",
        "DROP INDEX IF EXISTS idx_diary_created",
    ]

    # 4. Rows and rowids ride along — no INSERT..SELECT, so every id that
    #    turn_channel, the vector column and context_turns refer to stays
    #    valid.
    stmts.append("ALTER TABLE diary RENAME TO turn")

    # 5. Meta: the embedding identity the copied vectors actually pair with.
    stmts.append(
        "CREATE TABLE IF NOT EXISTS turn_meta "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    )
    if has_meta:
        stmts += [
            "INSERT INTO turn_meta (key, value) SELECT key, value FROM diary_meta",
            "DROP TABLE diary_meta",
        ]

    # 6. The keyword index over the renamed table, rebuilt from its content.
    stmts += [
        "CREATE INDEX idx_turn_created ON turn(created_at)",
        "CREATE VIRTUAL TABLE turn_fts USING fts5(\n"
        "    user_message, messages, summary, tags, channel,\n"
        "    content='turn', content_rowid='rowid'\n"
        ")",
        "INSERT INTO turn_fts(turn_fts) VALUES('rebuild')",
    ]

    # 7. The triggers, under their new names.
    stmts += list(TRIGGERS)
    return stmts


def _verify(conn: sqlite3.Connection, expected_turns: int) -> None:
    """Fail the transaction rather than commit a half-migrated database."""
    got = _count(conn, "turn")
    if got != expected_turns:
        raise MigrationError(f"turn has {got} rows, expected {expected_turns}")
    if _has(conn, "diary_semantic"):
        raise MigrationError("diary_semantic survived the migration")
    # For an external-content table the content comparison only happens when
    # ``rank`` is 1 — the default form checks the index against itself.
    conn.execute("INSERT INTO turn_fts(turn_fts, rank) VALUES('integrity-check', 1)")
    leftover = conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'diary%' "
        "OR name = 'idx_diary_created' LIMIT 1",
    ).fetchone()
    if leftover:
        raise MigrationError(f"legacy object survived: {leftover[0]}")


def migrate_db(
    db_path: Path, *, backup: bool = True, fail_after: int | None = None,
) -> str:
    """Migrate one database.  Returns the resulting state.

    ``fail_after`` exists for the tests: it raises after that many statements
    inside the transaction, to prove the whole thing rolls back.
    """
    if not db_path.is_file():
        raise MigrationError(f"no such file: {db_path}")

    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = OFF")

        if not _has(conn, "diary"):
            return ALREADY if _has(conn, "turn") else NOT_MEMDB

        diary_turns = _count(conn, "diary")
        has_new = _has(conn, "turn")
        turn_turns = _count(conn, "turn") if has_new else 0

        if has_new and turn_turns and diary_turns:
            raise MigrationError(
                f"both generations hold turns — diary={diary_turns} "
                f"({_span(conn, 'diary')}), turn={turn_turns} "
                f"({_span(conn, 'turn')}).  Merging them would mean "
                "renumbering rowids that turn_channel, the vector column and "
                "the live-context list all reference by number, so this script "
                "will not guess which generation is authoritative.  Move one "
                "aside by hand and re-run.",
            )
        if has_new and turn_turns and not diary_turns:
            return EMPTY_LEGACY            # nothing to carry over

        needs_vec = _has(conn, "diary_semantic") or _has(conn, "turn_semantic")
        if needs_vec and not _load_vec(conn):
            return VEC_MISSING

        if backup:
            target = db_path.with_name(db_path.name + BACKUP_SUFFIX)
            if not target.exists():
                # VACUUM INTO also fails on a database a live writer holds,
                # which is exactly the moment not to proceed.
                conn.execute("VACUUM INTO ?", (str(target),))

        stmts = _statements(conn, has_new_tables=has_new)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for i, stmt in enumerate(stmts):
                conn.execute(stmt)
                if fail_after is not None and i + 1 == fail_after:
                    raise MigrationError("injected failure (fail_after)")
            _verify(conn, diary_turns)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return NORMALIZED if has_new else MIGRATED
    finally:
        conn.close()


def find_databases(data_dir: Path) -> list[Path]:
    """All ``*.db`` files in the data dir (one per agent)."""
    return sorted(data_dir.glob("*.db"))


def _has_sidecars(db_path: Path) -> bool:
    """Whether the WAL sidecars are present (advice only — see ``main``)."""
    return (
        db_path.with_name(db_path.name + "-wal").exists()
        or db_path.with_name(db_path.name + "-shm").exists()
    )


def resolve_targets(args: argparse.Namespace) -> list[Path]:
    if args.db:
        return [Path(args.db).expanduser()]
    if args.agent:
        return [get_data_dir() / f"{args.agent}.db"]
    return find_databases(get_data_dir())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rename the memdb diary table to turn (one-time).",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--db", metavar="PATH", help="migrate one database file")
    group.add_argument(
        "--agent", metavar="NAME",
        help="migrate one agent's database (data dir / <NAME>.db)",
    )
    group.add_argument(
        "--all", action="store_true", help="migrate every *.db in the data dir",
    )
    parser.add_argument(
        "--no-backup", action="store_true",
        help=f"skip the {BACKUP_SUFFIX} copy taken before migrating",
    )
    args = parser.parse_args(argv)

    if not (args.db or args.agent or args.all):
        data_dir = get_data_dir()
        print(f"No target given — nothing was changed.  Databases in {data_dir}:")
        for path in find_databases(data_dir):
            print(f"  {path}")
        print("Pass --db PATH, --agent NAME, or --all.")
        return 0

    counts: dict[str, int] = {}
    failed = False
    for path in resolve_targets(args):
        if _has_sidecars(path):
            # Not a gate: a -wal/-shm outlives an unclean exit, so their
            # presence says nothing about whether slife is running right now
            # (a database untouched for weeks can carry them).  SQLite has no
            # portable "another process has this open" probe, so this is
            # advice, and ``BEGIN IMMEDIATE`` below is what actually
            # serializes against a live writer.
            print(
                f"note        {path}\n"
                "            -wal/-shm present — fine if slife is stopped; if "
                "it is running, stop it first, because its next save would "
                "fail against the renamed table.",
            )
        try:
            status = migrate_db(path, backup=not args.no_backup)
        except (MigrationError, sqlite3.Error) as e:
            print(f"FAILED  {path}\n        {e}")
            counts["failed"] = counts.get("failed", 0) + 1
            failed = True
            continue
        counts[status] = counts.get(status, 0) + 1
        print(f"{status:<12} {path}")
        if status == VEC_MISSING:
            print(
                "             sqlite-vec is not loadable — install it and "
                "re-run, so the existing embeddings are carried over.",
            )

    print("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
