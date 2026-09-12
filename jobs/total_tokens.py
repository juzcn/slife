"""Total token spend across conversation history.

Pure-computation job: reads the memdb SQLite database and aggregates the
billed token usage per turn. No LLM call. Returns a formatted text report.
"""

import glob
import os
import sqlite3


def total_tokens(since: str = "", until: str = "", db_path: str = "") -> str:
    """Summarize the total token spend across conversation history.

    Args:
        since: Optional ISO datetime (lower bound, inclusive) to restrict turns.
        until: Optional ISO datetime (upper bound, inclusive) to restrict turns.
        db_path: Path to the memdb SQLite database. Empty = auto-detect
            (this agent's own <agent>.db first, then any memdb-shaped *.db).
    """
    path = _resolve_db(db_path)
    if path is None:
        return (
            "Error: memdb database not found "
            "(tried $SLIFE_MEMDB_DB, $SLIFE_AGENT_NAME.db, "
            "$SLIFE_DATA_DIR, cwd, ../)"
        )

    conn = sqlite3.connect(path)
    try:
        clauses, params = [], []
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            clauses.append("created_at <= ?")
            params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(token_count),0), "
            "COALESCE(SUM(context_tokens),0) "
            f"FROM diary {where}",
            params,
        ).fetchone()
    finally:
        conn.close()

    n, total, ctx = row
    avg = round(total / n, 1) if n else 0
    return (
        f"数据库: {path}\n"
        f"轮数(turns): {n}\n"
        f"总token花费(token_count): {total}\n"
        f"总context_tokens: {ctx}\n"
        f"平均每轮: {avg}\n"
        f"筛选: since={since or '(无)'} until={until or '(无)'}"
    )


def _data_dir() -> str:
    """The data root this process is running for (falls back to cwd)."""
    for var in ("SLIFE_DATA_DIR", "SLIFE_CONFIG_DIR"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return os.getcwd()


def _looks_like_memdb(path: str) -> bool:
    """True if the SQLite file exposes the memdb 'diary' table."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='diary'"
            ).fetchone()
        finally:
            conn.close()
        return row is not None
    except sqlite3.Error:
        return False


def _resolve_db(db_path: str = "") -> str | None:
    """Locate the memdb database belonging to THIS agent."""
    if db_path:
        if os.path.isabs(db_path):
            return db_path if os.path.exists(db_path) else None
        for base in (os.getcwd(), os.path.dirname(os.getcwd()), _data_dir()):
            cand = os.path.join(base, db_path)
            if os.path.exists(cand):
                return cand
        return None

    # Canonical override the main process / memdb server / agent service all
    # honor first.  If it's set, it IS the database — trust it.
    override = os.environ.get("SLIFE_MEMDB_DB", "").strip()
    if override:
        return override if os.path.exists(override) else None

    data_dir = _data_dir()
    agent = os.environ.get("SLIFE_AGENT_NAME", "").strip()

    # 1. this agent's own database, by name.
    preferred = []
    if agent:
        preferred.append(os.path.join(data_dir, f"{agent}.db"))
    preferred.append(os.path.join(data_dir, "slife.db"))
    for cand in preferred:
        if os.path.exists(cand) and _looks_like_memdb(cand):
            return cand

    # 2. any memdb-shaped *.db nearby — newest first.
    found = []
    for base in (data_dir, os.getcwd(), os.path.dirname(os.getcwd())):
        if not base:
            continue
        for cand in glob.glob(os.path.join(base, "*.db")):
            if _looks_like_memdb(cand):
                found.append(cand)
    found = sorted(set(found), key=os.path.getmtime, reverse=True)
    return found[0] if found else None
