"""Total token spend across conversation history.

Pure-computation job: reads the memdb SQLite database and aggregates the
billed token usage per turn. No LLM call. Returns a formatted text report.
"""

import os
import sqlite3


def total_tokens(since: str = "", until: str = "", db_path: str = "") -> str:
    """Summarize the total token spend across conversation history.

    Args:
        since: Optional ISO datetime (lower bound, inclusive) to restrict turns.
        until: Optional ISO datetime (upper bound, inclusive) to restrict turns.
        db_path: Path to the memdb SQLite database. Empty = auto-detect.
    """
    path = _resolve_db(db_path)
    if path is None:
        return "Error: memdb database not found (tried cwd, ../, data dir)"

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
            "COALESCE(SUM(prompt_tokens),0) "
            f"FROM diary {where}",
            params,
        ).fetchone()
    finally:
        conn.close()

    n, total, prompt = row
    avg = round(total / n, 1) if n else 0
    return (
        f"数据库: {path}\n"
        f"轮数(turns): {n}\n"
        f"总token花费(token_count): {total}\n"
        f"总prompt_tokens: {prompt}\n"
        f"平均每轮: {avg}\n"
        f"筛选: since={since or '(无)'} until={until or '(无)'}"
    )


def _resolve_db(db_path: str = "") -> str | None:
    """Locate the memdb database (slife.db) with a few fallback paths."""
    if db_path:
        if os.path.isabs(db_path):
            return db_path if os.path.exists(db_path) else None
        for base in (os.getcwd(), os.path.dirname(os.getcwd())):
            cand = os.path.join(base, db_path)
            if os.path.exists(cand):
                return cand
        return None
    candidates = [
        os.path.join(os.getcwd(), "slife.db"),
        os.path.join(os.path.dirname(os.getcwd()), "slife.db"),
        os.path.expanduser("~/.slife/slife.db"),
    ]
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return None
