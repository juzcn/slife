"""Tests for scripts/migrate_context_tokens.py — the one-time column rename."""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "migrate_context_tokens.py"


def _load_migrator():
    spec = importlib.util.spec_from_file_location(
        "migrate_context_tokens_under_test", SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def migrator():
    return _load_migrator()


def _make_db(path: Path, *, with_legacy: bool) -> None:
    """Create a diary table with (or without) the legacy column + one row."""
    conn = sqlite3.connect(str(path))
    try:
        cols = "context_tokens INT" if not with_legacy else "prompt_tokens INT"
        conn.execute(
            "CREATE TABLE diary ("
            "user_message TEXT, messages TEXT, summary TEXT, tags TEXT, "
            "created_at TEXT, completed_at TEXT, channel TEXT, "
            "who_helped TEXT, what_model TEXT, token_count INT, "
            f"{cols})"
        )
        col = "context_tokens" if not with_legacy else "prompt_tokens"
        conn.execute(
            f"INSERT INTO diary (user_message, token_count, {col}) "
            "VALUES ('hi', 100, 200)"
        )
        conn.commit()
    finally:
        conn.close()


class TestMigrateDb:
    def test_renames_and_preserves_data(self, migrator, tmp_path):
        db = tmp_path / "agent.db"
        _make_db(db, with_legacy=True)

        assert migrator.migrate_db(db) == migrator.RENAMED

        conn = sqlite3.connect(str(db))
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(diary)")]
            assert "context_tokens" in cols
            assert "prompt_tokens" not in cols
            row = conn.execute(
                "SELECT token_count, context_tokens FROM diary"
            ).fetchone()
            assert row == (100, 200)
        finally:
            conn.close()

    def test_idempotent_after_migration(self, migrator, tmp_path):
        db = tmp_path / "agent.db"
        _make_db(db, with_legacy=True)

        migrator.migrate_db(db)
        assert migrator.migrate_db(db) == migrator.ALREADY

    def test_already_migrated_db_is_untouched(self, migrator, tmp_path):
        db = tmp_path / "agent.db"
        _make_db(db, with_legacy=False)

        assert migrator.migrate_db(db) == migrator.ALREADY

        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT token_count, context_tokens FROM diary"
            ).fetchone()
            assert row == (100, 200)
        finally:
            conn.close()

    def test_non_diary_db_skipped(self, migrator, tmp_path):
        db = tmp_path / "other.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE unrelated (x INT)")
        conn.commit()
        conn.close()

        assert migrator.migrate_db(db) == migrator.NO_DIARY

    def test_neither_column_reported(self, migrator, tmp_path):
        db = tmp_path / "odd.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE diary (user_message TEXT)")
        conn.commit()
        conn.close()

        assert migrator.migrate_db(db) == migrator.NO_LEGACY

    def test_find_databases_scans_dot_db_only(self, migrator, tmp_path):
        (tmp_path / "a.db").touch()
        (tmp_path / "b.db").touch()
        (tmp_path / "c.db-shm").touch()  # WAL companion — must not be matched

        found = migrator.find_databases(tmp_path)
        assert [p.name for p in found] == ["a.db", "b.db"]