#!/usr/bin/env python3
"""One-time migration: drop the now-unused ``images`` column from diary.

The diary ``images`` column is gone from the schema — image attachments
ride the ``include_image`` tool path (harness-invoked for ``@path``), the
image blocks are live-session-only, and restore is text-only.  The app
performs no migration (no ALTER inside the plugin) — run this once per DB
that predates the removal, then restart slife.

Existing DBs keep working even before this runs: the store INSERT/SELECT no
longer reference the column, so it just sits unused.  This script removes
the dead column for a clean schema.

Usage:
    python scripts/drop_memdb_images.py [DB_PATH]

DB resolution (no positional arg): $SLIFE_MEMDB_DB, else
<data_dir>/<agent_name>.db where agent_name = $SLIFE_AGENT_NAME or "slife".
Idempotent: a DB without an ``images`` column is left untouched.
"""

import argparse
import sqlite3
import sys
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


def migrate(db_path: Path) -> bool:
    """Drop the ``images`` column if present.  Returns True if it was dropped."""
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
        if "images" not in cols:
            print(f"no images column -> {db_path}")
            return False

        cur.execute("ALTER TABLE diary DROP COLUMN images")
        con.commit()
        print(f"dropped column images -> {db_path}")
        return True
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Drop diary.images from an existing memdb SQLite DB.",
    )
    parser.add_argument(
        "db", nargs="?",
        help="diary SQLite path (default: $SLIFE_MEMDB_DB or <data_dir>/<agent_name>.db)",
    )
    args = parser.parse_args()
    migrate(resolve_db_path(args.db))


if __name__ == "__main__":
    main()