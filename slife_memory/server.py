"""slife-memory server — FastMCP server for turn-based permanent memory.

Each turn (user message + assistant response) is an independent,
immutable row.  No sessions, no lifecycle — just turns.
Restore loads the most recent N turns by rowid.

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

_store: SessionStore | None = None
_embedder: EmbeddingClient | None = None
_DEFAULT_DB_PATH = Path.home() / ".slife" / "slife.db"


def _get_db_path() -> Path:
    env_path = os.environ.get("SLIFE_MEMORY_DB")
    if env_path:
        return Path(env_path)
    return _DEFAULT_DB_PATH


mcp = FastMCP(
    "slife-memory",
    instructions=(
        "slife-memory — turn-based long-term knowledge. "
        "Every turn (user question + your response) is one row. "
        "LLM-visible tools: memory_list_recent, memory_search (grep/fts5/hybrid/time), "
        "memory_open, memory_summarize, memory_check/set/remove_embedding. "
        "All tools take an author parameter for --user isolation."
    ),
)


# ═══════════════════════════════════════════════════════════════════════
# Harness tools (programmatic only — not exposed to LLM)
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(name="memory_save_turn", description="Save a turn. Harness-only.")
async def memory_save_turn(
    author: str = "default",
    user_message: str = "",
    messages: list[dict] | None = None,
    token_count: int = 0,
    who_helped: str = "",
    what_model: str = "",
) -> str:
    assert _store is not None
    try:
        rowid = await _store.save_turn(
            author=author, user_message=user_message, messages=messages,
            token_count=token_count, who_helped=who_helped, what_model=what_model,
            embedder=_embedder,
        )
        return json.dumps({"rowid": rowid, "status": "saved"}, ensure_ascii=False)
    except Exception as e:
        logger.exception("save_turn_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(name="memory_get_recent_turns", description="Load recent turns for restore. Harness-only.")
async def memory_get_recent_turns(
    author: str = "default", limit: int = 50,
) -> str:
    assert _store is not None
    try:
        turns = await _store.get_recent_turns(author=author, limit=limit)
        return json.dumps({"turns": turns}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("get_recent_turns_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════
# LLM-visible tools
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="memory_list_recent",
    description=(
        "Browse your recent knowledge, newest first. "
        "Each entry is one turn — a user question and your response. "
        "Returns rowid, user_message (truncated), summary, tags, created_at. "
        "Lightweight — use memory_open to load full content."
    ),
)
async def memory_list_recent(author: str = "default", limit: int = 20) -> str:
    assert _store is not None
    try:
        entries = await _store.list_recent(author=author, limit=limit)
        for e in entries:
            um = e.get("user_message", "")
            if len(um) > 200:
                e["user_message"] = um[:200] + "…"
        return json.dumps(entries, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("list_recent_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_count",
    description=(
        "Count your knowledge. Returns total turns and filtered count.\n"
        "- No params: total count for your author.\n"
        "- since/until: count in a time range (ISO datetime, e.g. '2026-07-01T00:00:00').\n"
        "  Use 'since' alone for 'since last month', 'until' for 'before date'.\n"
        "- query + mode: count turns matching a search (grep/fts5)."
    ),
)
async def memory_count(
    author: str = "default",
    since: str | None = None,
    until: str | None = None,
    query: str | None = None,
    mode: str = "fts5",
) -> str:
    assert _store is not None
    try:
        result = await _store.count_turns(
            author=author, since=since, until=until, query=query, mode=mode,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("count_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_open",
    description=(
        "Load a turn by rowid. Returns the full messages (OpenAI JSON) "
        "including thinking, tool calls, and tool results. "
        "Find rowids via memory_list_recent or memory_search."
    ),
)
async def memory_open(rowid: int, author: str = "default") -> str:
    assert _store is not None
    try:
        turn = await _store.get_turn(rowid=rowid, author=author)
        if turn is None:
            return json.dumps(
                {"error": f"未找到 turn rowid={rowid}"}, ensure_ascii=False,
            )
        return json.dumps(turn, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("open_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_search",
    description=(
        "Search your knowledge — everything you have ever read, written, "
        "or discussed. Each result is one turn.\n"
        "\n"
        "Four modes:\n"
        "  'grep' — exact substring match (error messages, file paths, code).\n"
        "  'fts5' — keyword ranking via BM25 (topic search).\n"
        "  'hybrid' — fts5 + semantic merged with RRF (default).\n"
        "  'time' — browse by date range, no query needed.\n"
        "\n"
        "All modes accept since/until (ISO datetime). "
        "Convert relative time to ISO: 'yesterday' → compute the date.\n"
        "\n"
        "Results are lightweight. Use memory_open to load full turns."
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
    assert _store is not None
    mode = mode.lower()
    if mode not in ("grep", "fts5", "hybrid", "time"):
        mode = "hybrid"

    if mode == "time":
        try:
            hits = await _store.search_time(author=author, limit=limit, since=since, until=until)
            return json.dumps({"mode": "time", "since": since, "until": until, "results": hits},
                              ensure_ascii=False, indent=2)
        except Exception as e:
            logger.exception("search_time_failed")
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    if not query.strip():
        return json.dumps({"error": "query 不能为空（time 模式不需要 query）"}, ensure_ascii=False)

    try:
        if mode == "grep":
            hits = await _store.search_grep(author=author, pattern=query, limit=limit,
                                             since=since, until=until)
            return json.dumps({"mode": "grep", "query": query, "results": hits,
                               "hint": "" if hits else f"未找到包含 '{query}' 的记忆"},
                              ensure_ascii=False, indent=2)

        if mode == "fts5":
            hits = await _store.search_keyword(author=author, query=query, limit=limit,
                                                since=since, until=until)
            return json.dumps({"mode": "fts5", "query": query, "results": hits,
                               "hint": "" if hits else f"未找到与 '{query}' 相关的记忆"},
                              ensure_ascii=False, indent=2)

        # hybrid
        keyword_hits = await _store.search_keyword(author=author, query=query, limit=limit * 2,
                                                     since=since, until=until)
        semantic_hits: list[dict] = []
        semantic_available = False
        if _embedder and _embedder.available:
            emb = await _embedder.embed_one(query)
            if emb:
                semantic_hits = await _store.search_semantic(author=author, embedding=emb,
                                                              limit=limit * 2,
                                                              since=since, until=until)
                semantic_available = True

        merged = merge_hybrid(keyword_hits, semantic_hits)
        return json.dumps({
            "mode": "hybrid" if semantic_available else "fts5",
            "query": query,
            "results": merged[:limit],
            "hint": "" if merged else "没有找到相关的记忆",
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("search_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_summarize",
    description=(
        "Write a summary and tags for a specific turn. "
        "Summary: 1-2 sentences about what this turn accomplished. "
        "Tags: comma-separated topics (e.g. 'debug,auth,oauth'). "
        "Both optional. This makes the turn findable via search."
    ),
)
async def memory_summarize(
    rowid: int, author: str = "default",
    summary: str | None = None, tags: str | None = None,
) -> str:
    assert _store is not None
    try:
        await _store.update_summary(rowid=rowid, author=author, summary=summary, tags=tags)

        if summary and _embedder and _embedder.available:
            try:
                emb = await _embedder.embed_one(summary)
                if emb:
                    assert _store._conn is not None
                    cursor = await _store._conn.execute(
                        "SELECT tags, created_at FROM diary WHERE rowid = ? AND author = ?",
                        (rowid, author),
                    )
                    row = await cursor.fetchone()
                    if row:
                        await _store.upsert_embedding(
                            rowid=rowid, author=author,
                            summary=summary, tags=tags or row["tags"] or "",
                            created_at=row["created_at"], turn_embedding=emb,
                        )
            except Exception as e:
                logger.debug("embedding_upsert_skipped err=%s", e)

        return json.dumps({"status": "已更新", "rowid": rowid}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("summarize_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════
# Embedding config tools (unchanged)
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(name="memory_check_embedding",
          description="Check the current embedding configuration status.")
async def memory_check_embedding() -> str:
    from slife_memory.embedding_config import make_check_report
    try:
        return json.dumps(make_check_report(), ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("check_embedding_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(name="memory_set_embedding",
          description="Configure the embedding backend: 'gguf' or 'api'.")
async def memory_set_embedding(
    backend: str = "", model: str = "bge-m3",
    gguf_path: str | None = None, dim: int = 0,
) -> str:
    from slife_memory.embedding_config import (
        write_embedding_config, validate_gguf_path,
        get_first_provider_api_key, reload_embedder,
    )
    backend = backend.lower().strip()
    if backend not in ("gguf", "api"):
        return json.dumps({"error": f"不支持的后端 '{backend}'。可选: 'gguf' 或 'api'"}, ensure_ascii=False, indent=2)
    cfg: dict = {"model": model}
    if backend == "gguf":
        if not gguf_path:
            return json.dumps({"error": "GGUF 后端需要 gguf_path 参数"}, ensure_ascii=False, indent=2)
        ok, msg = validate_gguf_path(gguf_path)
        if not ok:
            return json.dumps({"error": f"GGUF 文件校验失败: {msg}"}, ensure_ascii=False, indent=2)
        cfg["gguf_path"] = msg
        if dim > 0:
            cfg["dim"] = dim
    elif backend == "api":
        if not get_first_provider_api_key():
            return json.dumps({"error": "API 后端需要 api_key"}, ensure_ascii=False, indent=2)
        if dim > 0:
            cfg["dim"] = dim
    try:
        write_embedding_config(cfg)
        status = await reload_embedder()
        status["backend"] = backend
        status["model"] = model
        if gguf_path:
            status["gguf_path"] = gguf_path
        return json.dumps(status, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("set_embedding_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(name="memory_remove_embedding",
          description="Remove the embedding configuration.")
async def memory_remove_embedding() -> str:
    from slife_memory.embedding_config import remove_embedding_config, reload_embedder
    try:
        remove_embedding_config()
        status = await reload_embedder()
        status["message"] = "Embedding 配置已移除。语义搜索已禁用，关键词搜索仍可用。"
        return json.dumps(status, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("remove_embedding_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── Entry point ──────────────────────────────────────────────────────


def _read_db_path_from_config(config_path: str) -> Path | None:
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
    import argparse
    parser = argparse.ArgumentParser(description="slife-memory server")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    logger.info("log_path=%s", _log_path)

    db_path = Path(args.db).expanduser() if args.db else None
    if db_path is None:
        config_db = _read_db_path_from_config("slife.json5")
        db_path = config_db or _get_db_path()

    import asyncio

    async def _init():
        global _store, _embedder
        _embedder = EmbeddingClient.from_config()
        _store = SessionStore(db_path)
        await _store.setup(embedding_dim=_embedder.dimension)
        if _embedder.available:
            logger.info("embeddings_ready backend=%s model=%s dim=%d",
                        _embedder.backend, _embedder._model, _embedder.dimension)
        else:
            logger.info("embeddings_disabled")

    asyncio.run(_init())

    if not sys.stdin.isatty():
        logger.info("memory_start transport=stdio db=%s", db_path)
        mcp.run(transport="stdio")
        return

    config_path = "slife.json5"
    if not Path(config_path).exists():
        logger.error("slife.json5 not found.")
        sys.exit(1)

    cfg = read_host_port_from_config(config_path, config_key="memory", default_port=9877)
    if cfg is None:
        logger.error("Cannot determine host/port.")
        sys.exit(1)

    host = args.host if args.host is not None else cfg[0]
    port = args.port if args.port is not None else cfg[1]
    logger.info("memory_start transport=http host=%s port=%s db=%s", host, port, db_path)
    mcp.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    main()
