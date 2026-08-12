"""Tests for scripts/migrate_memdb_completed_at.py — the one-off backfill."""

import importlib.util
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest; pytestmark = pytest.mark.unit


def _load_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "migrate_memdb_completed_at.py"
    spec = importlib.util.spec_from_file_location("migrate_memdb_completed_at", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _make_db(tmp_path, n=3, with_completed_at=False):
    """Old-style diary DB (no completed_at) with n rows."""
    db = tmp_path / "memory.db"
    con = sqlite3.connect(str(db))
    col = "completed_at TEXT," if with_completed_at else ""
    con.execute(f"""CREATE TABLE diary (
        user_message TEXT NOT NULL DEFAULT '',
        messages TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        {col}
        token_count INTEGER NOT NULL DEFAULT 0
    )""")
    for i in range(1, n + 1):
        con.execute(
            "INSERT INTO diary (user_message, created_at, token_count) VALUES (?, ?, ?)",
            (f"msg {i}", f"2026-08-12T14:3{i}:00+08:00", 100),
        )
    con.commit()
    con.close()
    return db


class TestMigrate:
    def test_adds_column_and_backfills(self, tmp_path):
        mod = _load_script()
        db = _make_db(tmp_path, n=2)
        before = {
            r[0]: r[1]
            for r in sqlite3.connect(str(db)).execute(
                "SELECT rowid, created_at FROM diary"
            )
        }

        changed = mod.migrate(db, max_minutes=5)

        assert changed == 2
        con = sqlite3.connect(str(db))
        cols = [r[1] for r in con.execute("PRAGMA table_info(diary)")]
        assert "completed_at" in cols
        for rowid, created_at, completed_at in con.execute(
            "SELECT rowid, created_at, completed_at FROM diary"
        ):
            # completed_at keeps the original save-time value
            assert completed_at == before[rowid]
            # created_at was pulled earlier by a random 0..5 minutes
            assert created_at <= completed_at
            gap = (
                datetime.fromisoformat(completed_at)
                - datetime.fromisoformat(created_at)
            )
            assert gap.total_seconds() == gap.seconds  # < 24h, same offset
            assert 0 <= gap.seconds <= 5 * 60

    def test_idempotent(self, tmp_path):
        mod = _load_script()
        db = _make_db(tmp_path, n=2)
        mod.migrate(db, max_minutes=5)
        snapshot = list(
            sqlite3.connect(str(db)).execute(
                "SELECT rowid, created_at, completed_at FROM diary"
            )
        )
        # Second run: nothing left to backfill → 0 rows changed
        changed = mod.migrate(db, max_minutes=5)
        assert changed == 0
        after = list(
            sqlite3.connect(str(db)).execute(
                "SELECT rowid, created_at, completed_at FROM diary"
            )
        )
        assert after == snapshot

    def test_existing_completed_at_column_untouched(self, tmp_path):
        """A DB that already has completed_at is not re-shifted."""
        mod = _load_script()
        db = _make_db(tmp_path, n=1, with_completed_at=True)
        con = sqlite3.connect(str(db))
        con.execute(
            "UPDATE diary SET completed_at = '2026-08-12T14:30:00+08:00'"
        )
        con.commit()
        con.close()

        changed = mod.migrate(db, max_minutes=5)
        assert changed == 0
