#!/usr/bin/env python3
"""One-time migration: rename the diary column ``prompt_tokens`` → ``context_tokens``.

The old code persisted only the last API call's ``prompt_tokens`` as the
context-size figure; the new code persists the last call's prompt + completion
tokens under the clearer name ``context_tokens`` (see ``schema.sql`` and the
``context_tokens`` storage path in the loop / inbox / memdb store).  Databases
created before the rename keep the legacy column, so run this once per agent
database after upgrading:

    python scripts/migrate_context_tokens.py            # all agent DBs
    python scripts/migrate_context_tokens.py --db X.db  # one database

The rename preserves data: pre-rename rows still hold the prompt-only figure,
which under-reports the exit-time context by the last completion — that
self-corrects after the first new turn.  Idempotent: databases already
carrying ``context_tokens`` are reported as already migrated and left alone.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

# Allow running from anywhere: ``slife.paths`` must resolve regardless of the
# CWD (dev mode resolves the data dir against the repo root).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slife.paths import get_data_dir  # noqa: E402

RENAMED = "renamed"
ALREADY = "already"
NO_DIARY = "no_diary_table"
NO_LEGACY = "no_legacy_column"


def diary_columns(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA table_info(diary)").fetchall()
    return [row[1] for row in rows]


def migrate_db(db_path: Path) -> str:
    """Rename ``prompt_tokens`` → ``context_tokens`` on a single database.

    Returns the resulting state:
      ``RENAMED``  — legacy column present, renamed
      ``ALREADY``  — ``context_tokens`` already present (nothing to do)
      ``NO_DIARY`` — file has no ``diary`` table (not a memdb)
      ``NO_LEGACY``— neither column present (unexpected schema)
    """
    conn = sqlite3.connect(str(db_path))
    try:
        has_diary = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='diary'"
        ).fetchone()
        if not has_diary:
            return NO_DIARY
        cols = diary_columns(conn)
        if "context_tokens" in cols:
            return ALREADY
        if "prompt_tokens" not in cols:
            return NO_LEGACY
        conn.execute(
            "ALTER TABLE diary RENAME COLUMN prompt_tokens TO context_tokens"
        )
        conn.commit()
        return RENAMED
    finally:
        conn.close()


def find_databases(data_dir: Path) -> list[Path]:
    """All ``*.db`` files in the data dir (one per agent)."""
    return sorted(data_dir.glob("*.db"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rename the diary prompt_tokens column to context_tokens.",
    )
    parser.add_argument(
        "--db", metavar="PATH", default=None,
        help="Migrate a single database file; otherwise all *.db in the data dir.",
    )
    args = parser.parse_args(argv)

    data_dir = get_data_dir()
    if args.db:
        targets = [Path(args.db)]
    else:
        targets = find_databases(data_dir)
        if not targets:
            print(f"没有找到数据库: {data_dir} 下无 *.db 文件")
            return 0

    counts = {RENAMED: 0, ALREADY: 0, NO_DIARY: 0, NO_LEGACY: 0}
    for path in targets:
        status = migrate_db(path)
        counts[status] += 1
        if status == RENAMED:
            print(f"已迁移: {path}")
        elif status == ALREADY:
            print(f"跳过(已迁移): {path}")
        elif status == NO_DIARY:
            print(f"跳过(无 diary 表): {path}")

    summary = (
        f"完成: 迁移 {counts[RENAMED]}, 已迁移 {counts[ALREADY]}, "
        f"跳过 {counts[NO_DIARY]} (无 diary 表)"
    )
    if counts[NO_LEGACY]:
        summary += f", 无 {NO_LEGACY} {counts[NO_LEGACY]}"
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())