#!/usr/bin/env python3
"""One-time backfill: add ``completed_at`` to an existing diary DB.

The diary gains ``completed_at`` (assistant completion time) alongside the
existing ``created_at`` (user input time).  This script migrates an EXISTING
database that predates the column.  The app adds no migration code (no ALTER
inside the plugin) — run this once per DB, then restart slife.

Semantics for pre-existing rows: their ``created_at`` was written at save
time (the assistant's completion moment), so it becomes ``completed_at``;
``created_at`` is then moved earlier by a random 0..N minutes to represent
the user's input time, which precedes the reply.

Usage:
    python scripts/migrate_memdb_completed_at.py [DB_PATH] [--max-minutes 5]

DB resolution (no positional arg): $SLIFE_MEMDB_DB, else
<data_dir>/<agent_name>.db where agent_name = $SLIFE_AGENT_NAME or "slife".
Idempotent: rows whose ``completed_at`` is already set are left untouched.
"""

import argparse
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path


def resolve_db_path(arg: str | None) -> Path:
    """Pick the diary DB: CLI arg > $SLIFE_MEMDB_DB > <data_dir>/<agent_name>.db."""
    if arg:
        return Path(arg).expanduser()
    import os

    env = os.environ.get("SLIFE_MEMDB_DB")
    if env:
        return Path(env)
    from slife.paths import get_data_dir

    agent_name = os.environ.get("SLIFE_AGENT_NAME", "slife")
    return get_data_dir() / f"{agent_name}.db"


def _shift_earlier(iso: str, max_minutes: int) -> tuple[str, str]:
    """Return ``(completed_at, created_at)`` for one row.

    ``completed_at`` keeps the original save-time value; ``created_at`` is
    pulled earlier by a random 0..max_minutes minutes.  Unparseable values
    keep ``created_at`` untouched (still backfill ``completed_at``).
    """
    completed_at = iso
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return completed_at, iso
    back = random.randint(0, max_minutes)
    created = (dt - timedelta(minutes=back)).isoformat(timespec="seconds")
    return completed_at, created


def migrate(db_path: Path, max_minutes: int) -> int:
    """Add ``completed_at`` and backfill.  Returns the row count changed."""
    if not db_path.is_file():
        sys.exit(f"DB not found: {db_path}")

    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        has_diary = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='diary'"
        ).fetchone()
        if not has_diary:
            sys.exit(f"no diary table in {db_path}")

        cols = [r[1] for r in cur.execute("PRAGMA table_info(diary)")]
        if "completed_at" not in cols:
            cur.execute("ALTER TABLE diary ADD COLUMN completed_at TEXT")
            print(f"added column completed_at -> {db_path}")

        rows = cur.execute(
            "SELECT rowid, created_at FROM diary WHERE completed_at IS NULL"
        ).fetchall()
        for rowid, created_at in rows:
            completed_at, created = _shift_earlier(created_at, max_minutes)
            cur.execute(
                "UPDATE diary SET completed_at = ?, created_at = ? WHERE rowid = ?",
                (completed_at, created, rowid),
            )
        con.commit()
        print(
            f"backfilled {len(rows)} rows in {db_path} "
            f"(completed_at = created_at, created_at - random(0..{max_minutes}) min)"
        )
        return len(rows)
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill diary.completed_at on an existing memdb SQLite DB."
    )
    parser.add_argument(
        "db", nargs="?",
        help="diary SQLite path (default: $SLIFE_MEMDB_DB or <data_dir>/<agent_name>.db)",
    )
    parser.add_argument(
        "--max-minutes", type=int, default=5,
        help="pull created_at earlier by a random 0..N minutes (default 5)",
    )
    args = parser.parse_args()
    migrate(resolve_db_path(args.db), args.max_minutes)


if __name__ == "__main__":
    main()
