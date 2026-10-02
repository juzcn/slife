"""The one-shot ``diary`` -> ``turn`` migration (scripts/migrate_memdb_diary_to_turn.py).

The script is loaded by path rather than imported: it is an operator tool that
lives outside the package, and loading it here is what keeps it testable.
"""

import importlib.util
import json
import sqlite3
import struct
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO / "scripts" / "migrate_memdb_diary_to_turn.py"

#: The pre-rename schema, verbatim — the fixture is an old database, so it must
#: not be built from the current schema.sql.
OLD_SCHEMA = [
    """CREATE TABLE diary (
        user_message    TEXT NOT NULL DEFAULT '',
        messages        TEXT NOT NULL DEFAULT '[]',
        summary         TEXT DEFAULT '',
        tags            TEXT DEFAULT '',
        created_at      TEXT NOT NULL,
        completed_at    TEXT,
        channel         TEXT DEFAULT '',
        who_helped      TEXT DEFAULT '',
        what_model      TEXT DEFAULT '',
        token_count     INTEGER NOT NULL DEFAULT 0,
        context_tokens  INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE VIRTUAL TABLE diary_fts USING fts5(
        user_message, messages, summary, tags, channel,
        content='diary', content_rowid='rowid'
    )""",
    """CREATE TRIGGER diary_ai AFTER INSERT ON diary BEGIN
        INSERT INTO diary_fts(rowid, user_message, messages, summary, tags, channel)
        VALUES (new.rowid, new.user_message, new.messages, new.summary, new.tags, new.channel);
    END""",
    """CREATE TRIGGER diary_ad AFTER DELETE ON diary BEGIN
        INSERT INTO diary_fts(diary_fts, rowid, user_message, messages, summary, tags, channel)
        VALUES ('delete', old.rowid, old.user_message, old.messages, old.summary, old.tags, old.channel);
    END""",
    """CREATE TRIGGER diary_au AFTER UPDATE ON diary BEGIN
        INSERT INTO diary_fts(diary_fts, rowid, user_message, messages, summary, tags, channel)
        VALUES ('delete', old.rowid, old.user_message, old.messages, old.summary, old.tags, old.channel);
        INSERT INTO diary_fts(rowid, user_message, messages, summary, tags, channel)
        VALUES (new.rowid, new.user_message, new.messages, new.summary, new.tags, new.channel);
    END""",
    "CREATE TABLE diary_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE INDEX idx_diary_created ON diary(created_at)",
    """CREATE TABLE turn_channel (
        turn_id  INTEGER PRIMARY KEY,
        data     TEXT NOT NULL DEFAULT '{}'
    )""",
]

#: Deliberately NOT 1536 — ``run_schema`` substitutes the configured width, so
#: the script must read it from the live DDL rather than assume.
OLD_VEC_DIM = 4

OLD_VEC_SCHEMA = """CREATE VIRTUAL TABLE diary_semantic USING vec0(
    turn_embedding float[%d] distance_metric=cosine,
    +diary_rowid   INTEGER,
    +chunk_index   INTEGER,
    +summary       TEXT,
    +tags          TEXT,
    +created_at    TEXT
)""" % OLD_VEC_DIM

#: Rowids with a gap, so a silent renumbering cannot pass.
ROWS = [
    (1, "alpha uniqueone", "first summary", "2026-01-01T10:00:00+08:00"),
    (2, "bravo uniquetwo", "second summary", "2026-01-02T10:00:00+08:00"),
    (5, "alpha charlie", "third summary", "2026-01-05T10:00:00+08:00"),
]

#: (vec rowid, the turn it belongs to, chunk index, vector) — the vectors point
#: in different directions so the KNN's order is a real answer, not a tie.
VEC_ROWS = [
    (10, 1, 0, (1.0, 0.0, 0.0, 0.0)),
    (11, 1, 1, (0.9, 0.1, 0.0, 0.0)),
    (12, 5, 0, (0.0, 0.0, 1.0, 0.0)),
]

PROBES = ("alpha", "uniqueone", "bravo", "updatedthird")


def _load_script():
    spec = importlib.util.spec_from_file_location("migrate_diary_to_turn", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mig = _load_script()


def _vec_blob(*values: float) -> bytes:
    return struct.pack(f"<{OLD_VEC_DIM}f", *values)


def _load_vec_extension(conn: sqlite3.Connection) -> bool:
    if not hasattr(conn, "enable_load_extension"):
        return False
    try:
        import sqlite_vec
    except ImportError:
        return False
    try:
        conn.enable_load_extension(True)
        conn.load_extension(sqlite_vec.loadable_path())
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def _has(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def _build_old_db(path: Path, *, vec: bool) -> dict:
    """Build a populated pre-rename database and snapshot what matters."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        for stmt in OLD_SCHEMA:
            conn.execute(stmt)
        if vec:
            assert _load_vec_extension(conn), "sqlite-vec unavailable"
            conn.execute(OLD_VEC_SCHEMA)

        for rowid, message, summary, created in ROWS:
            conn.execute(
                "INSERT INTO diary (rowid, user_message, messages, summary, tags, "
                "channel, created_at, completed_at, who_helped, what_model, "
                "token_count, context_tokens) "
                "VALUES (?, ?, '[]', ?, '', 'human', ?, NULL, '', 'm', 10, 20)",
                (rowid, message, summary, created),
            )
        # Through diary_au, so the index must hold the UPDATED summary and not
        # merely what the inserts wrote.
        conn.execute("UPDATE diary SET summary='updatedthird' WHERE rowid=5")

        if vec:
            for vec_rowid, turn_rowid, chunk, vector in VEC_ROWS:
                conn.execute(
                    "INSERT INTO diary_semantic (rowid, turn_embedding, diary_rowid, "
                    "chunk_index, summary, tags, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        vec_rowid,
                        _vec_blob(*vector),
                        turn_rowid,
                        chunk,
                        f"chunk {chunk}",
                        "t",
                        "2026-01-01T10:00:00+08:00",
                    ),
                )

        for rowid, _, _, _ in ROWS:
            conn.execute(
                "INSERT INTO turn_channel (turn_id, data) VALUES (?, ?)",
                (rowid, json.dumps({"peer": f"p{rowid}"})),
            )
        for key, value in (
            ("embedding_model", "old-model"),
            ("embedding_dim", str(OLD_VEC_DIM)),
            ("embedding_text_version", "v1"),
            ("context_turns", json.dumps([1, 2, 5])),
        ):
            conn.execute(
                "INSERT INTO diary_meta (key, value) VALUES (?, ?)", (key, value),
            )

        snapshot = {
            "turns": [tuple(r) for r in conn.execute(
                "SELECT rowid, user_message, messages, summary, tags, channel, "
                "created_at, completed_at, who_helped, what_model, token_count, "
                "context_tokens FROM diary ORDER BY rowid",
            ).fetchall()],
            "fts": {
                probe: [r[0] for r in conn.execute(
                    "SELECT rowid FROM diary_fts WHERE diary_fts MATCH ? ORDER BY rowid",
                    (probe,),
                ).fetchall()]
                for probe in PROBES
            },
            "vec": [tuple(r) for r in conn.execute(
                "SELECT rowid, turn_embedding, diary_rowid, chunk_index, summary, "
                "tags, created_at FROM diary_semantic ORDER BY rowid",
            ).fetchall()] if vec else [],
            "meta": [tuple(r) for r in conn.execute(
                "SELECT key, value FROM diary_meta ORDER BY key",
            ).fetchall()],
            "channel": [tuple(r) for r in conn.execute(
                "SELECT turn_id, data FROM turn_channel ORDER BY turn_id",
            ).fetchall()],
        }
        conn.commit()
        return snapshot
    finally:
        conn.close()


def _dump(conn) -> dict:
    """A comparable picture of the NEW schema's contents."""
    return {
        "turns": [tuple(r) for r in conn.execute(
            "SELECT rowid, user_message, messages, summary, tags, channel, "
            "created_at, completed_at, who_helped, what_model, token_count, "
            "context_tokens FROM turn ORDER BY rowid",
        ).fetchall()],
        "fts": {
            probe: [r[0] for r in conn.execute(
                "SELECT rowid FROM turn_fts WHERE turn_fts MATCH ? ORDER BY rowid",
                (probe,),
            ).fetchall()]
            for probe in PROBES
        },
        "meta": [tuple(r) for r in conn.execute(
            "SELECT key, value FROM turn_meta ORDER BY key",
        ).fetchall()],
        "channel": [tuple(r) for r in conn.execute(
            "SELECT turn_id, data FROM turn_channel ORDER BY turn_id",
        ).fetchall()],
    }


@pytest.fixture
def old_db(tmp_path):
    path = tmp_path / "slife.db"
    snapshot = _build_old_db(path, vec=False)
    return path, snapshot


@pytest.fixture
def old_vec_db(tmp_path):
    path = tmp_path / "slife.db"
    snapshot = _build_old_db(path, vec=True)
    return path, snapshot


def test_migration_preserves_rows_rowids_and_the_index(old_db):
    path, snap = old_db
    assert mig.migrate_db(path, backup=False) == mig.MIGRATED

    conn = sqlite3.connect(path)
    try:
        got = _dump(conn)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        legacy = conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'diary%' "
            "OR name = 'idx_diary_created'",
        ).fetchall()
    finally:
        conn.close()

    assert got["turns"] == snap["turns"]
    # The gap is the point: rowids are referenced by number from turn_channel,
    # the vector column and the live-context list.
    assert [r[0] for r in got["turns"]] == [1, 2, 5]
    assert got["fts"] == snap["fts"]
    # …including the row whose summary diary_au rewrote before the migration.
    assert got["fts"]["updatedthird"] == [5]
    assert got["meta"] == snap["meta"]
    assert got["channel"] == snap["channel"]
    assert integrity == "ok"
    assert legacy == []


def test_snippets_work_after_the_rebuild(old_db):
    """A stale ``content=`` is invisible to MATCH but breaks ``snippet()``."""
    path, _ = old_db
    mig.migrate_db(path, backup=False)

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT rowid, snippet(turn_fts, 0, '[', ']', '…', 20) FROM turn_fts "
            "WHERE turn_fts MATCH 'uniqueone'",
        ).fetchall()
    finally:
        conn.close()

    assert [r[0] for r in rows] == [1]
    assert "[uniqueone]" in rows[0][1]


def test_new_triggers_feed_the_new_index(old_db):
    path, _ = old_db
    mig.migrate_db(path, backup=False)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'turn_%'",
        ).fetchall() != []
        conn.execute("UPDATE turn SET summary='freshlywritten' WHERE rowid=2")
        hits = conn.execute(
            "SELECT rowid FROM turn_fts WHERE turn_fts MATCH 'freshlywritten'",
        ).fetchall()
    finally:
        conn.close()

    assert [r[0] for r in hits] == [2]


def test_next_rowid_continues_after_the_gap(old_db):
    path, _ = old_db
    mig.migrate_db(path, backup=False)

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO turn (user_message, messages, created_at) VALUES ('x', '[]', 'now')",
        )
        rowid = conn.execute("SELECT MAX(rowid) FROM turn").fetchone()[0]
    finally:
        conn.close()

    assert rowid == 6


def test_meta_context_turns_survives_as_a_parseable_list(old_db):
    path, snap = old_db
    mig.migrate_db(path, backup=False)

    conn = sqlite3.connect(path)
    try:
        value = conn.execute(
            "SELECT value FROM turn_meta WHERE key='context_turns'",
        ).fetchone()[0]
    finally:
        conn.close()

    assert json.loads(value) == [1, 2, 5]


def test_vectors_are_copied_byte_for_byte(old_vec_db):
    path, snap = old_vec_db
    assert mig.migrate_db(path, backup=False) == mig.MIGRATED

    conn = sqlite3.connect(path)
    assert _load_vec_extension(conn), "sqlite-vec unavailable"
    try:
        rows = [tuple(r) for r in conn.execute(
            "SELECT rowid, turn_embedding, turn_rowid, chunk_index, summary, tags, "
            "created_at FROM turn_semantic ORDER BY rowid",
        ).fetchall()]
        # The width is read from the old DDL, never assumed to be 1536.
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='turn_semantic'",
        ).fetchone()[0]
        nearest = conn.execute(
            "SELECT turn_rowid FROM turn_semantic WHERE turn_embedding MATCH ? "
            "AND k = 3 ORDER BY distance",
            (_vec_blob(1.0, 0.0, 0.0, 0.0),),
        ).fetchall()
    finally:
        conn.close()

    expected = [
        (vec_rowid, _vec_blob(*vector), turn_rowid, chunk, f"chunk {chunk}", "t",
         "2026-01-01T10:00:00+08:00")
        for vec_rowid, turn_rowid, chunk, vector in VEC_ROWS
    ]
    assert rows == expected
    # The vector bytes themselves, not just the metadata around them.
    assert [r[1] for r in rows] == [r[1] for r in snap["vec"]]
    assert f"float[{OLD_VEC_DIM}]" in ddl
    assert "turn_rowid" in ddl and "diary_rowid" not in ddl
    # The KNN still resolves each chunk back to the turn it belongs to.
    assert [r[0] for r in nearest] == [1, 1, 5]


def test_rerun_is_a_no_op(old_db):
    path, _ = old_db
    assert mig.migrate_db(path, backup=False) == mig.MIGRATED
    conn = sqlite3.connect(path)
    try:
        before = _dump(conn)
    finally:
        conn.close()

    assert mig.migrate_db(path, backup=False) == mig.ALREADY

    conn = sqlite3.connect(path)
    try:
        assert _dump(conn) == before
    finally:
        conn.close()


def test_backup_is_written_beside_the_db(old_db):
    path, _ = old_db
    mig.migrate_db(path, backup=True)
    backup = path.with_name(path.name + mig.BACKUP_SUFFIX)
    assert backup.is_file()
    # The backup is the pre-migration database, so it still has the old table.
    conn = sqlite3.connect(backup)
    try:
        assert _has(conn, "diary") and not _has(conn, "turn")
    finally:
        conn.close()


def test_case_b_drops_the_app_created_empty_generation(old_db):
    """The new code ran first, leaving empty turn tables beside the old rows."""
    path, snap = old_db
    conn = sqlite3.connect(path)
    try:
        for stmt in (
            "CREATE TABLE turn (user_message TEXT, messages TEXT, summary TEXT, "
            "tags TEXT, channel TEXT, created_at TEXT, completed_at TEXT, "
            "who_helped TEXT, what_model TEXT, token_count INTEGER, "
            "context_tokens INTEGER)",
            "CREATE TABLE turn_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "CREATE INDEX idx_turn_created ON turn(created_at)",
        ):
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO turn_meta (key, value) VALUES ('embedding_model', 'app-written')",
        )
        conn.commit()
    finally:
        conn.close()

    assert mig.migrate_db(path, backup=False) == mig.NORMALIZED

    conn = sqlite3.connect(path)
    try:
        got = _dump(conn)
    finally:
        conn.close()

    assert got["turns"] == snap["turns"]
    assert got["fts"] == snap["fts"]
    # The app's empty generation never described these turns: diary_meta wins.
    assert got["meta"] == snap["meta"]
    assert ("embedding_model", "app-written") not in got["meta"]


def test_case_c_refuses_and_changes_nothing(old_db):
    path, snap = old_db
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE turn (user_message TEXT, messages TEXT, summary TEXT, "
            "tags TEXT, channel TEXT, created_at TEXT, completed_at TEXT, "
            "who_helped TEXT, what_model TEXT, token_count INTEGER, "
            "context_tokens INTEGER)",
        )
        conn.execute(
            "INSERT INTO turn (user_message, messages, created_at) VALUES ('new', '[]', 'now')",
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(mig.MigrationError, match="both generations"):
        mig.migrate_db(path, backup=False)

    conn = sqlite3.connect(path)
    try:
        assert _has(conn, "diary") and _has(conn, "turn")
        assert [tuple(r) for r in conn.execute(
            "SELECT rowid FROM diary ORDER BY rowid")] == [(1,), (2,), (5,)]
        assert conn.execute("SELECT COUNT(*) FROM turn").fetchone()[0] == 1
    finally:
        conn.close()


def test_empty_legacy_table_is_left_alone(tmp_path):
    path = tmp_path / "slife.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE diary (user_message TEXT, created_at TEXT)")
        conn.execute(
            "CREATE TABLE turn (user_message TEXT, created_at TEXT)",
        )
        conn.execute("INSERT INTO turn (user_message, created_at) VALUES ('live', 'now')")
        conn.commit()
    finally:
        conn.close()

    assert mig.migrate_db(path, backup=False) == mig.EMPTY_LEGACY

    conn = sqlite3.connect(path)
    try:
        assert _has(conn, "diary") and _has(conn, "turn")
    finally:
        conn.close()


def test_injected_failure_rolls_the_whole_transaction_back(old_vec_db):
    path, snap = old_vec_db
    with pytest.raises(mig.MigrationError, match="injected"):
        mig.migrate_db(path, backup=False, fail_after=4)

    conn = sqlite3.connect(path)
    try:
        # Every old object survives, whole — nothing half-moved.
        assert _has(conn, "diary")
        assert _has(conn, "diary_semantic")
        assert _has(conn, "diary_meta")
        assert not _has(conn, "turn")
        assert [r[0] for r in conn.execute(
            "SELECT rowid FROM diary ORDER BY rowid")] == [1, 2, 5]
    finally:
        conn.close()

    # …and the re-run then succeeds.
    assert mig.migrate_db(path, backup=False) == mig.MIGRATED
    conn = sqlite3.connect(path)
    try:
        assert _dump(conn)["turns"] == snap["turns"]
    finally:
        conn.close()


def test_sidecars_do_not_block_the_migration(old_db):
    """A -wal/-shm outlives an unclean exit, so it is advice, not a gate."""
    path, snap = old_db
    sidecar = path.with_name(path.name + "-wal")
    sidecar.write_bytes(b"")
    try:
        assert mig.migrate_db(path, backup=False) == mig.MIGRATED
        assert mig._has_sidecars(path) is True
    finally:
        sidecar.unlink()

    conn = sqlite3.connect(path)
    try:
        assert _dump(conn)["turns"] == snap["turns"]
    finally:
        conn.close()


def test_non_memdb_and_fresh_databases(tmp_path):
    other = tmp_path / "other.db"
    sqlite3.connect(other).close()
    assert mig.migrate_db(other, backup=False) == mig.NOT_MEMDB

    fresh = tmp_path / "fresh.db"
    conn = sqlite3.connect(fresh)
    try:
        conn.execute("CREATE TABLE turn (user_message TEXT, created_at TEXT)")
        conn.commit()
    finally:
        conn.close()
    assert mig.migrate_db(fresh, backup=False) == mig.ALREADY


def test_find_databases_ignores_wal_sidecars(tmp_path):
    (tmp_path / "slife.db").touch()
    (tmp_path / "sophie.db").touch()
    (tmp_path / "slife.db-wal").touch()
    (tmp_path / "slife.db-shm").touch()

    assert [p.name for p in mig.find_databases(tmp_path)] == [
        "slife.db", "sophie.db",
    ]
