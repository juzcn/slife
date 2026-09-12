"""Tests for the total_tokens job (repo ``jobs/`` dir).

The job is a pure-computation memdb reader: it locates the agent's SQLite
database (``$SLIFE_AGENT_NAME.db`` first, then any memdb-shaped ``*.db``)
and aggregates ``token_count`` / ``context_tokens`` per turn.  These tests
exercise both the DB discovery and the aggregation against a real temp DB
shaped like the current diary schema.
"""

import os
import sqlite3

import pytest; pytestmark = pytest.mark.unit

JOB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "jobs",
    "total_tokens.py",
)


@pytest.fixture(scope="module")
def job():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_total_tokens_utm", JOB_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_db(path, rows):
    """Create a diary-table SQLite DB with the current memdb columns."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE diary ("
            " user_message TEXT NOT NULL DEFAULT '',"
            " messages TEXT NOT NULL DEFAULT '[]',"
            " created_at TEXT NOT NULL,"
            " token_count INTEGER NOT NULL DEFAULT 0,"
            " context_tokens INTEGER NOT NULL DEFAULT 0)"
        )
        conn.executemany(
            "INSERT INTO diary (user_message, created_at, token_count,"
            " context_tokens) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return str(path)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate every SLIFE_* var the job reads so the surrounding test
    environment (repo root is the dev data dir) can't leak in."""
    for var in (
        "SLIFE_MEMDB_DB",
        "SLIFE_AGENT_NAME",
        "SLIFE_DATA_DIR",
        "SLIFE_CONFIG_DIR",
    ):
        monkeypatch.delenv(var, raising=False)


# ── DB discovery ────────────────────────────────────────────────────────


def test_resolve_finds_agent_db_by_name(tmp_path, monkeypatch, job):
    """The real-world case from the bug report: the agent's DB is named
    sophie.db — resolved via $SLIFE_AGENT_NAME, not the default slife.db."""
    db = _make_db(tmp_path / "sophie.db", [("hi", "2026-09-12T09:00:00", 100, 90)])
    monkeypatch.setenv("SLIFE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SLIFE_AGENT_NAME", "sophie")

    assert job._resolve_db() == db


def test_resolve_honors_memdb_override(tmp_path, monkeypatch, job):
    """$SLIFE_MEMDB_DB — the canonical override honored by the memdb server
    and the agent service — wins over agent-name / *.db discovery."""
    _make_db(tmp_path / "sophie.db", [("a", "2026-09-12T09:00:00", 10, 9)])
    (tmp_path / "custom").mkdir()
    custom = _make_db(
        tmp_path / "custom" / "mem.db", [("b", "2026-09-12T09:05:00", 20, 15)]
    )
    monkeypatch.setenv("SLIFE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SLIFE_AGENT_NAME", "sophie")
    monkeypatch.setenv("SLIFE_MEMDB_DB", custom)

    assert job._resolve_db() == custom


def test_resolve_falls_back_to_any_memdb_shape(tmp_path, monkeypatch, job):
    """A named agent DB that isn't memdb-shaped is skipped; the next
    memdb-shaped *.db nearby wins."""
    plain = sqlite3.connect(str(tmp_path / "sophie.db"))
    plain.execute("CREATE TABLE other (x)").connection.commit()
    plain.close()
    other = _make_db(tmp_path / "slife.db", [("b", "2026-09-12T09:05:00", 2, 2)])
    monkeypatch.setenv("SLIFE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SLIFE_AGENT_NAME", "sophie")

    assert job._resolve_db() == other


def test_resolve_relative_db_path(tmp_path, monkeypatch, job):
    """A relative db_path argument resolves against cwd, parent, then data
    dir — matching the documented semantics."""
    _make_db(tmp_path / "rel.db", [("a", "2026-09-12T09:00:00", 1, 1)])
    monkeypatch.setenv("SLIFE_DATA_DIR", str(tmp_path))

    assert job._resolve_db("rel.db") == str(tmp_path / "rel.db")


def test_resolve_not_found(tmp_path, monkeypatch, job):
    """No DB anywhere → None → the job reports a clear error (and names the
    override it checked)."""
    monkeypatch.setenv("SLIFE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(job.os, "getcwd", lambda: str(tmp_path / "cwd"))

    out = job.total_tokens()

    assert "memdb database not found" in out
    assert "$SLIFE_MEMDB_DB" in out


# ── Aggregation against the current schema ─────────────────────────────┐


def test_total_tokens_sums_current_columns(tmp_path, job):
    """The query reads token_count + context_tokens (the current columns) —
    NOT the legacy prompt_tokens, which would raise ``no such column``."""
    rows = [
        ("hi", "2026-09-12T09:00:00", 100, 90),
        ("there", "2026-09-12T10:00:00", 250, 200),
        ("!", "2026-09-12T11:00:00", 60, 55),
    ]
    db = _make_db(tmp_path / "diary.db", rows)
    out = job.total_tokens(db_path=db)

    assert "轮数(turns): 3" in out
    assert "总token花费(token_count): 410" in out  # 100+250+60
    assert "总context_tokens: 345" in out          # 90+200+55
    assert "平均每轮: 136.7" in out


def test_total_tokens_since_until_filter(tmp_path, job):
    rows = [
        ("early", "2026-09-12T09:00:00", 100, 90),
        ("middle", "2026-09-12T10:00:00", 250, 200),
        ("late", "2026-09-12T11:00:00", 60, 55),
    ]
    db = _make_db(tmp_path / "diary.db", rows)

    out = job.total_tokens(db_path=db, since="2026-09-12T09:30:00")
    assert "轮数(turns): 2" in out

    out = job.total_tokens(
        db_path=db,
        since="2026-09-12T09:30:00",
        until="2026-09-12T10:30:00",
    )
    assert "轮数(turns): 1" in out
    assert "总token花费(token_count): 250" in out