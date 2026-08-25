#!/usr/bin/env python3
"""One-time migration: add scheduled-task tables to an existing memfiles DB.

Adds ``reports`` (scheduled-task report docs), ``scheduled_tasks``
(task definitions) and ``scheduled_runs`` (per-run status + report index)
to an EXISTING ``{agent}.files/.index.db`` that predates them.

The app adds no migration code (no schema changes inside the plugin at
runtime beyond the CREATE IF NOT EXISTS in schema.sql for fresh DBs) —
run this once per DB, then restart slife.

Idempotent: a DB that already has ``scheduled_tasks`` is left untouched.

Usage:
    python scripts/migrate_memfiles_scheduled.py [DB_PATH]

DB resolution (no positional arg): $SLIFE_MEMFILES_DB, else
<data_dir>/<agent_name>.files/.index.db where agent_name =
$SLIFE_AGENT_NAME or "slife".
"""

import argparse
import sqlite3
import sys
from pathlib import Path


def resolve_db_path(arg: str | None) -> Path:
    """Pick the memfiles index DB: CLI arg > env > <data_dir>/<agent>.files/.index.db."""
    if arg:
        return Path(arg).expanduser()
    import os

    env = os.environ.get("SLIFE_MEMFILES_DB")
    if env:
        return Path(env)
    from slife.paths import get_memfiles_dir

    agent_name = os.environ.get("SLIFE_AGENT_NAME", "slife")
    return get_memfiles_dir(agent_name) / ".index.db"


def _schema_statements() -> list[str]:
    """Return the runtime schema.sql's non-vec0 statements.

    Runs the whole CREATE IF NOT EXISTS set (safe for a DB that already has
    notes/diary/files) except the ``vec0`` virtual tables: those need the
    sqlite-vec extension, which is only loaded at runtime by the store.  The
    store's ``_maybe_migrate_vec_dimension`` creates/migrates every vec0
    table (incl. ``reports_semantic``) on startup, so the migration leaves
    them to the runtime — no drift, no extension dependency.
    """
    from slife.plugins.memdb.store import _split_sql

    here = Path(__file__).resolve().parent.parent
    schema = here / "slife" / "plugins" / "memfiles" / "schema.sql"
    stmts: list[str] = []
    for raw in _split_sql(schema.read_text(encoding="utf-8")):
        stmt = raw.strip()
        if not stmt:
            continue
        if "CREATE VIRTUAL TABLE" in stmt and "vec0" in stmt:
            continue
        stmts.append(stmt)
    return stmts


def migrate(db_path: Path) -> bool:
    """Add the scheduled-task tables if missing.  Returns True if added."""
    if not db_path.is_file():
        sys.exit(f"DB not found: {db_path}")

    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        has_tasks = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_tasks'"
        ).fetchone()
        if has_tasks:
            print(f"already has scheduled_tasks -> {db_path}")
            return False

        # Non-vec0 schema statements (CREATE IF NOT EXISTS) — idempotent.
        for stmt in _schema_statements():
            con.execute(stmt)
        con.commit()
        print(
            "added scheduled_tasks / scheduled_runs / reports "
            "(reports_semantic is created at runtime by the store) "
            f"-> {db_path}"
        )
        return True
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db_path", nargs="?", help="Path to .index.db (optional)")
    args = parser.parse_args()
    migrate(resolve_db_path(args.db_path))


if __name__ == "__main__":
    main()
