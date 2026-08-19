#!/usr/bin/env python3
"""One-time migration: add ``prompt_tokens`` to an existing diary DB.

The diary gains ``prompt_tokens`` (the last LLM call's prompt_tokens — the
exact context size at turn end) alongside the existing ``token_count`` (the
turn's cumulative total_tokens for billing).  Restore uses the latest
restored turn's ``prompt_tokens`` to prime the context footer / _sys_note
with the real exit-time context size instead of an estimate.

This script migrates an EXISTING database that predates the column.  The
app adds no migration code (no ALTER inside the plugin) — run this once per
DB, then restart slife.

Existing rows keep ``prompt_tokens = 0`` (the column default); restore
falls back to the token estimate for those legacy rows.  New rows written
after the upgrade carry their real value.

Usage:
    python scripts/migrate_memdb_prompt_tokens.py [DB_PATH]

DB resolution (no positional arg): $SLIFE_MEMDB_DB, else
<data_dir>/<agent_name>.db where agent_name = $SLIFE_AGENT_NAME or "slife".
Idempotent: a DB that already has the column is left untouched.
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
    """Add the ``prompt_tokens`` column if missing.  Returns True if added."""
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
        if "prompt_tokens" in cols:
            print(f"already has prompt_tokens -> {db_path}")
            return False

        cur.execute(
            "ALTER TABLE diary ADD COLUMN prompt_tokens INTEGER NOT NULL DEFAULT 0"
        )
        con.commit()
        print(f"added column prompt_tokens -> {db_path}")
        return True
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add diary.prompt_tokens to an existing memdb SQLite DB."
    )
    parser.add_argument(
        "db", nargs="?",
        help="diary SQLite path (default: $SLIFE_MEMDB_DB or <data_dir>/<agent_name>.db)",
    )
    args = parser.parse_args()
    migrate(resolve_db_path(args.db))


if __name__ == "__main__":
    main()
