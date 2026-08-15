#!/usr/bin/env python3
"""One-time migration: rebuild the memdb semantic index from full text.

The old ``memory_summarize`` (now ``memory_turn_summarize``) re-embedded a turn
from its summary — replacing the turn's full-text chunks in ``diary_semantic``
with a single summary
vector, which silently dropped the full text from semantic search.  The
plugin no longer does this (summary/tags are keyword-search recall clues
only), but an existing DB still carries the summary-only vectors.

This script clears ``diary_semantic`` so every embedded turn is unembedded
again; on the next slife restart the drainer re-embeds each turn from its
full text (user message + assistant/tool contents) — the same path that
built the index originally.  Semantic search is briefly unavailable (hybrid
degrades to FTS5) while it rebuilds, exactly as after a model change.

Usage:
    python scripts/migrate_memdb_embeddings.py [DB_PATH]

DB resolution (no positional arg): $SLIFE_MEMDB_DB, else
<data_dir>/<agent_name>.db where agent_name = $SLIFE_AGENT_NAME or "slife".
Idempotent: an empty (or absent) semantic index is left untouched.
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


def _load_vec(con: sqlite3.Connection) -> bool:
    """Load sqlite-vec on the connection so the vec0 table is queryable.

    ``diary_semantic`` is a vec0 virtual table — any statement touching it
    (COUNT/DELETE included) fails with ``no such module: vec0`` unless the
    extension is loaded.  Mirrors the plugin's ``SessionStore._load_vec_extension``
    (store.py), but against the sync ``sqlite3`` connection used here.
    """
    try:
        import sqlite_vec
    except ImportError:
        return False
    try:
        con.enable_load_extension(True)
        con.load_extension(sqlite_vec.loadable_path())
        con.enable_load_extension(False)
        return True
    except Exception:
        return False


def migrate(db_path: Path) -> int:
    """Clear the semantic index.  Returns the number of chunks deleted."""
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

        has_semantic = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='diary_semantic'"
        ).fetchone()
        if not has_semantic:
            print(f"no semantic index in {db_path} -> nothing to do")
            return 0

        if not _load_vec(con):
            print(
                f"sqlite-vec unavailable in this Python env -> semantic index in "
                f"{db_path} left untouched (the app also cannot serve semantic search here)"
            )
            return 0

        count = cur.execute("SELECT COUNT(*) FROM diary_semantic").fetchone()[0]
        if count == 0:
            print(f"semantic index already empty -> {db_path}")
            return 0

        cur.execute("DELETE FROM diary_semantic")
        con.commit()
        print(
            f"cleared {count} embedding chunks from {db_path} -> "
            "restart slife so the drainer rebuilds them from full text"
        )
        return count
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clear the memdb semantic index so it rebuilds from full text on restart."
    )
    parser.add_argument(
        "db", nargs="?",
        help="diary SQLite path (default: $SLIFE_MEMDB_DB or <data_dir>/<agent_name>.db)",
    )
    args = parser.parse_args()
    migrate(resolve_db_path(args.db))


if __name__ == "__main__":
    main()
