"""slife-memory server — FastMCP server for permanent memory with hybrid search.

This is the entry point for the slife-memory service. It:
  1. Starts a FastMCP server on stdio or HTTP transport
  2. Manages a SQLite database (diary-style conversation records)
  3. Provides keyword (FTS5) and semantic (sqlite-vec) search
  4. Detects interrupted sessions for crash recovery

Usage:
    uv run python -m slife_memory.server               # auto-detect transport
    uv run python -m slife_memory.server --port 9877   # HTTP mode
"""

import json
import logging
import os
import sys
from pathlib import Path

from fastmcp import FastMCP

from slife_memory.store import SessionStore
from slife_memory.embeddings import EmbeddingClient
from slife_memory.search import merge_hybrid
from slife.server_utils import setup_server_logging, read_host_port_from_config

logger = logging.getLogger("slife_memory")

_log_path = setup_server_logging("slife_memory")

# ── Global state ─────────────────────────────────────────────────────

_store: SessionStore | None = None
_embedder: EmbeddingClient | None = None

# Default DB path — can be overridden from config
_DEFAULT_DB_PATH = Path.home() / ".slife" / "slife.db"


def _get_db_path() -> Path:
    """Resolve DB path from environment or config, falling back to default."""
    env_path = os.environ.get("SLIFE_MEMORY_DB")
    if env_path:
        return Path(env_path)
    return _DEFAULT_DB_PATH


# ── FastMCP server ──────────────────────────────────────────────────

mcp = FastMCP(
    "slife-memory",
    instructions=(
        "slife-memory — permanent conversation diary with hybrid search. "
        "Every conversation is recorded as one diary row. "
        "LLM-visible tools: memory_list_recent, memory_search (grep/fts5/hybrid/time), "
        "memory_open, memory_summarize. "
        "All tools take an author parameter for --user isolation."
    ),
)


# ═══════════════════════════════════════════════════════════════════════
# Memory tools
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="memory_open_diary",
    description=(
        "Start a new conversation diary or detect an interrupted one. "
        "Always call this first when a conversation begins. "
        "If the author has an interrupted diary (status '进行中'), it returns "
        "the interrupted session so the user can choose to restore or discard it. "
        "If no interruption, creates a fresh diary entry and returns its rowid."
    ),
)
async def memory_open_diary(
    author: str = "default",
    who_helped: str = "",
    what_model: str = "",
    system_prompt: str = "",
) -> str:
    """Open a diary — start new or detect interrupted."""
    assert _store is not None
    try:
        result = await _store.open_diary(
            author=author,
            who_helped=who_helped,
            what_model=what_model,
            system_prompt=system_prompt,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("open_diary_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_close_diary",
    description=(
        "Close a conversation diary — mark it as cleanly finished. "
        "Optionally provide a title, summary, tags, and key moments "
        "to make the diary searchable and recallable later. "
        "Call this when the user exits or says goodbye."
    ),
)
async def memory_close_diary(
    rowid: int,
    author: str = "default",
    title: str | None = None,
    summary: str | None = None,
    tags: str | None = None,
    key_moments: str | None = None,
) -> str:
    """Close a diary — mark as completed, optionally add summary."""
    assert _store is not None
    try:
        # Write summary if provided
        if any(x is not None for x in (title, summary, tags, key_moments)):
            await _store.update_summary(
                rowid=rowid, author=author,
                title=title, summary=summary,
                tags=tags, key_moments=key_moments,
            )

        # Generate embedding for summary if available
        if summary and _embedder and _embedder.available:
            try:
                emb = await _embedder.embed_one(summary)
                if emb:
                    await _store.upsert_embedding(
                        rowid=rowid, author=author,
                        summary=summary, summary_embedding=emb,
                    )
            except Exception as e:
                logger.debug("embedding_upsert_skipped err=%s", e)

        await _store.close_diary(rowid=rowid, author=author)
        return json.dumps(
            {"status": "已完成", "rowid": rowid, "author": author},
            ensure_ascii=False, indent=2,
        )
    except Exception as e:
        logger.exception("close_diary_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_update_diary",
    description=(
        "Save the current conversation messages to the diary. "
        "Call this after each completed turn (user message + assistant response). "
        "Pass the full OpenAI-format messages list and updated turn/token counts. "
        "This overwrites the messages for this diary entry — it's a save, not an append. "
        "trim_count records how many messages have been trimmed from the front "
        "(cumulative, used to restore the exact working context on restart)."
    ),
)
async def memory_update_diary(
    rowid: int,
    author: str = "default",
    messages: list[dict] | None = None,
    turn_count: int = 0,
    token_count: int = 0,
    trim_count: int = 0,
) -> str:
    """Save conversation progress after each turn."""
    assert _store is not None
    try:
        await _store.update_diary(
            rowid=rowid, author=author,
            messages=messages,
            turn_count=turn_count,
            token_count=token_count,
            trim_count=trim_count,
        )
        return json.dumps(
            {"status": "保存完成", "rowid": rowid, "turn_count": turn_count},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.exception("update_diary_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_list_recent",
    description=(
        "List recent conversation diaries, newest first. "
        "Returns rowid, title, summary, tags, created_at, turn/token counts. "
        "Lightweight — no full messages. Use memory_open to read a full entry."
    ),
)
async def memory_list_recent(
    author: str = "default",
    limit: int = 20,
) -> str:
    """List recent diary entries for an author."""
    assert _store is not None
    try:
        entries = await _store.list_recent(author=author, limit=limit)
        return json.dumps(entries, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("list_recent_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_open",
    description=(
        "Read a full conversation by rowid. "
        "Returns the complete messages list (OpenAI JSON) plus title, summary, "
        "tags, key_moments, and trim_count. "
        "Find rowids via memory_list_recent or memory_search first."
    ),
)
async def memory_open(rowid: int, author: str = "default") -> str:
    """Read a full diary entry by rowid."""
    assert _store is not None
    try:
        entry = await _store.get_diary(rowid=rowid, author=author)
        if entry is None:
            return json.dumps(
                {"error": f"未找到日记 rowid={rowid} author={author}"},
                ensure_ascii=False,
            )
        return json.dumps(entry, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("open_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_search",
    description=(
        "Search past conversations (excludes the active one). "
        "Four modes: "
        "'grep' — exact substring in full message text (error messages, code, "
        "file paths). "
        "'fts5' — keyword ranking via BM25 full-text index (topic search). "
        "'hybrid' — fts5 + semantic vector search merged with RRF (default; "
        "best for fuzzy recall when you don't remember exact words). "
        "'time' — browse by time range, no text query needed. "
        "All modes accept since/until (ISO datetime, e.g. 2026-07-14T00:00:00) "
        "to filter by created_at. YOU must convert relative time to ISO: "
        "'yesterday' → compute yesterday's date; 'last week' → 7 days ago; "
        "'this month' → first day of current month. Combine since+until for "
        "a range. Omit both to search all time. "
        "Use memory_open to read a full match."
        "Use memory_open to read a full match."
    ),
)
async def memory_search(
    author: str = "default",
    query: str = "",
    mode: str = "hybrid",
    limit: int = 10,
    since: str | None = None,
    until: str | None = None,
) -> str:
    """Search past conversations — grep / fts5 / hybrid / time, with optional time range."""
    assert _store is not None

    mode = mode.lower()
    if mode not in ("grep", "fts5", "hybrid", "time"):
        mode = "hybrid"

    # time mode: no query needed
    if mode == "time":
        try:
            hits = await _store.search_time(
                author=author, limit=limit, since=since, until=until,
            )
            return json.dumps(
                {"mode": "time", "since": since, "until": until, "results": hits},
                ensure_ascii=False, indent=2,
            )
        except Exception as e:
            logger.exception("search_time_failed")
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    if not query.strip():
        return json.dumps(
            {"error": "query 不能为空（time 模式不需要 query）"}, ensure_ascii=False,
        )

    try:
        # ── grep: exact substring scan ──────────────────────────
        if mode == "grep":
            hits = await _store.search_grep(
                author=author, pattern=query, limit=limit,
                since=since, until=until,
            )
            if not hits:
                return json.dumps(
                    {"mode": "grep", "query": query, "results": [],
                     "hint": f"未找到包含 '{query}' 的记忆"},
                    ensure_ascii=False, indent=2,
                )
            return json.dumps(
                {"mode": "grep", "query": query, "results": hits},
                ensure_ascii=False, indent=2,
            )

        # ── fts5: keyword index search ─────────────────────────
        if mode == "fts5":
            hits = await _store.search_keyword(
                author=author, query=query, limit=limit,
                since=since, until=until,
            )
            if not hits:
                return json.dumps(
                    {"mode": "fts5", "query": query, "results": [],
                     "hint": f"未找到与 '{query}' 相关的记忆"},
                    ensure_ascii=False, indent=2,
                )
            return json.dumps(
                {"mode": "fts5", "query": query, "results": hits},
                ensure_ascii=False, indent=2,
            )

        # ── hybrid: fts5 + semantic → RRF ──────────────────────
        keyword_hits = await _store.search_keyword(
            author=author, query=query, limit=limit * 2,
            since=since, until=until,
        )

        semantic_hits: list[dict] = []
        semantic_available = False
        if _embedder and _embedder.available:
            emb = await _embedder.embed_one(query)
            if emb:
                semantic_hits = await _store.search_semantic(
                    author=author, embedding=emb, limit=limit * 2,
                    since=since, until=until,
                )
                semantic_available = True

        merged = merge_hybrid(keyword_hits, semantic_hits)

        if not merged:
            return json.dumps(
                {"mode": "hybrid" if semantic_available else "fts5",
                 "query": query, "results": [],
                 "hint": "没有找到相关的记忆"},
                ensure_ascii=False, indent=2,
            )

        return json.dumps(
            {
                "mode": "hybrid" if semantic_available else "fts5",
                "query": query,
                "results": merged[:limit],
            },
            ensure_ascii=False, indent=2,
        )
    except Exception as e:
        logger.exception("search_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_summarize",
    description=(
        "Annotate a diary entry with title, summary, tags, and key moments. "
        "Call when a conversation wraps up so it's findable later: "
        "summary enables semantic search, tags enable topic search, "
        "key_moments record important decisions or insights. "
        "All fields optional — only pass what you have. "
        "Does NOT modify the conversation messages."
    ),
)
async def memory_summarize(
    rowid: int,
    author: str = "default",
    title: str | None = None,
    summary: str | None = None,
    tags: str | None = None,
    key_moments: str | None = None,
) -> str:
    """Add summary/metadata to a diary entry."""
    assert _store is not None
    try:
        await _store.update_summary(
            rowid=rowid, author=author,
            title=title, summary=summary,
            tags=tags, key_moments=key_moments,
        )

        # Generate embedding for the summary
        if summary and _embedder and _embedder.available:
            try:
                emb = await _embedder.embed_one(summary)
                if emb:
                    await _store.upsert_embedding(
                        rowid=rowid, author=author,
                        summary=summary, summary_embedding=emb,
                    )
            except Exception as e:
                logger.debug("embedding_upsert_skipped err=%s", e)

        return json.dumps(
            {"status": "已更新", "rowid": rowid},
            ensure_ascii=False, indent=2,
        )
    except Exception as e:
        logger.exception("summarize_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── Entry point ──────────────────────────────────────────────────────


def _read_db_path_from_config(config_path: str) -> Path | None:
    """Read memory.db_path from slife.json5."""
    try:
        import json5
        raw = json5.loads(Path(config_path).read_text(encoding="utf-8"))
    except Exception:
        return None

    memory = raw.get("memory", {})
    if isinstance(memory, dict) and "db_path" in memory:
        return Path(memory["db_path"]).expanduser()
    return None


def main():
    """Run the slife-memory server.

    Auto-detects transport mode:
      - Piped stdin (slife child process) → stdio mode
      - Terminal → reads slife.json5 → HTTP mode

    Examples:
      python -m slife_memory.server                          # auto-detect
      python -m slife_memory.server --port 9877              # HTTP, override port
      python -m slife_memory.server --db ~/.slife/slife.db  # custom DB path
    """
    import argparse

    parser = argparse.ArgumentParser(description="slife-memory server")
    parser.add_argument(
        "--port", type=int, default=None, help="HTTP port (overrides config)",
    )
    parser.add_argument(
        "--host", default=None, help="HTTP host (overrides config)",
    )
    parser.add_argument(
        "--db", default=None, help="Path to the SQLite database file",
    )
    args = parser.parse_args()

    logger.info("log_path=%s", _log_path)

    # Resolve DB path
    db_path = Path(args.db).expanduser() if args.db else None
    if db_path is None:
        config_db = _read_db_path_from_config("slife.json5")
        db_path = config_db or _get_db_path()

    # Initialize store and embedder asynchronously
    import asyncio

    async def _init():
        global _store, _embedder

        _embedder = EmbeddingClient.from_config()

        _store = SessionStore(db_path)
        await _store.setup(embedding_dim=_embedder.dimension)

        if _embedder.available:
            logger.info(
                "embeddings_ready backend=%s model=%s dim=%d",
                _embedder.backend, _embedder._model, _embedder.dimension,
            )
        else:
            logger.info("embeddings_disabled — semantic search unavailable")

    asyncio.run(_init())

    # Auto-detect transport
    if not sys.stdin.isatty():
        logger.info("memory_start transport=stdio db=%s", db_path)
        mcp.run(transport="stdio")
        return

    # Terminal mode — read host/port from slife.json5
    config_path = "slife.json5"
    if not Path(config_path).exists():
        logger.error(
            "slife.json5 not found. Either:\n"
            "  - Create slife.json5 with memory.url, or\n"
            "  - Use --host/--port to specify the HTTP endpoint."
        )
        sys.exit(1)

    cfg = read_host_port_from_config(config_path, config_key="memory", default_port=9877)
    if cfg is None:
        logger.error(
            "Cannot determine host/port. "
            "Set memory.url in slife.json5 or use --host/--port."
        )
        sys.exit(1)

    host = args.host if args.host is not None else cfg[0]
    port = args.port if args.port is not None else cfg[1]

    logger.info(
        "memory_start transport=http host=%s port=%s db=%s",
        host, port, db_path,
    )
    mcp.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    main()
