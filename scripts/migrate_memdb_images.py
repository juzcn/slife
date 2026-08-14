#!/usr/bin/env python3
"""One-time migration: add ``images`` to an existing diary DB.

The diary gains ``images`` (user image attachments — a JSON array of local
paths / https URLs) alongside the existing turn columns.  This script
migrates an EXISTING database that predates the column.  The app adds no
migration code (no ALTER inside the plugin) — run this once per DB, then
restart slife.

Existing rows keep ``images = ''`` (the column default); new rows written
after the upgrade carry their real attachment list.

Usage:
    python scripts/migrate_memdb_images.py [DB_PATH]

DB resolution (no positional arg): $SLIFE_MEMDB_DB, else
<data_dir>/<agent_id>.db where agent_id = $SLIFE_AGENT_ID or "slife".
Idempotent: a DB that already has the column is left untouched.
"""

import argparse
import sqlite3
import sys
from pathlib import Path


def resolve_db_path(arg: str | None) -> Path:
    """Pick the diary DB: CLI arg > $SLIFE_MEMDB_DB > <data_dir>/<agent_id>.db."""
    if arg:
        return Path(arg).expanduser()
    import os

    env = os.environ.get("SLIFE_MEMDB_DB")
    if env:
        return Path(env)
    from slife.paths import get_data_dir

    agent_id = os.environ.get("SLIFE_AGENT_ID", "slife")
    return get_data_dir() / f"{agent_id}.db"


def migrate(db_path: Path) -> bool:
    """Add the ``images`` column if missing.  Returns True if it was added."""
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
        if "images" in cols:
            print(f"already has images -> {db_path}")
            return False

        cur.execute(
            "ALTER TABLE diary ADD COLUMN images TEXT NOT NULL DEFAULT ''"
        )
        con.commit()
        print(f"added column images -> {db_path}")
        return True
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add diary.images to an existing memdb SQLite DB."
    )
    parser.add_argument(
        "db", nargs="?",
        help="diary SQLite path (default: $SLIFE_MEMDB_DB or <data_dir>/<agent_id>.db)",
    )
    args = parser.parse_args()
    migrate(resolve_db_path(args.db))


if __name__ == "__main__":
    main()
