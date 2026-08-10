"""slife-memdb server — FastMCP server for turn-based permanent memory.

Each turn (user message + assistant response) is an independent,
immutable row.  No sessions, no lifecycle — just turns.
Restore loads the most recent N turns by rowid.

Usage:
    uv run python -m slife.plugins.memdb.server       # auto-assigned port (Streamable HTTP)
    uv run python -m slife.plugins.memdb.server --port 9877   # fixed port
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from slife.paths import get_data_dir
from slife.plugins.memdb.store import SessionStore
from slife.plugins.memdb.embeddings import EmbeddingClient
from slife.plugins.memdb.search import merge_hybrid
from slife.server_utils import create_plugin_server

mcp, _log_path, logger = create_plugin_server(
    "slife-memdb",
    instructions=(
        "slife-memdb — turn-based long-term knowledge. "
        "Every turn (user question + your response) is one row. "
        "LLM-visible tools: memory_list_recent, memory_search (grep/fts5/hybrid/time), "
        "memory_open, memory_summarize, memory_check/set/remove_embedding. "
        "All data is automatically scoped to the current agent."
    ),
)

_store: SessionStore | None = None
_embedder: EmbeddingClient | None = None
_db_path: Path | None = None
_init_lock: asyncio.Lock | None = None
_reindex_task: asyncio.Task | None = None  # type: ignore[valid-type]


def _hybrid_fallback_reason() -> str:
    """Return a human-readable reason why hybrid search fell back to FTS5."""
    if _embedder is None:
        return "hybrid 降级为 fts5 — embedding 后端未初始化"
    if not _embedder.available:
        cfg = _embedder._backend
        if cfg == "gguf":
            if _embedder._gguf_path:
                if Path(_embedder._gguf_path).exists():
                    return ("hybrid 降级为 fts5 — llama-cpp-python 未安装。"
                            "运行: uv pip install llama-cpp-python")
                return ("hybrid 降级为 fts5 — GGUF 文件未找到。"
                        "下载模型后使用 memory_set_embedding 配置路径")
            return "hybrid 降级为 fts5 — 未配置 GGUF 模型路径"
        if cfg == "api":
            return ("hybrid 降级为 fts5 — API key 为未解析的 ${VAR} 占位符或缺失。"
                    "使用 memory_set_embedding backend=api 配置真实 API key，"
                    "或改用本地模型: memory_set_embedding backend=gguf")
        return ("hybrid 降级为 fts5 — embedding 后端不可用。"
                "使用 memory_check_embedding 查看详情，"
                "使用 memory_set_embedding 配置嵌入后端")
    # embedder.available is True but embed_one() returned None
    return ("hybrid 降级为 fts5 — 查询嵌入生成失败（API 调用异常或超时）。"
            "检查 API key 是否正确，或切换为本地模型")


def _get_db_path() -> Path:
    """Return the database path for the current agent.

    Uses ``SLIFE_DATA_DIR`` (set by the main process) so dev and
    production environments each get their own location.
    """
    agent_id = os.environ.get("SLIFE_AGENT_ID", "slife")
    env_path = os.environ.get("SLIFE_MEMDB_DB")
    if env_path:
        return Path(env_path)
    data_dir = get_data_dir()
    return data_dir / f"{agent_id}.db"


async def _ensure_store() -> SessionStore:
    """Lazy-init the store and embedder inside FastMCP's event loop.

    This MUST run inside ``mcp.run()``'s event loop — ``asyncio.run()``
    creates a temporary loop that gets destroyed, causing ``aiosqlite``
    operations to hang forever because their background thread is bound
    to a loop that no longer exists.
    """
    global _store, _embedder, _init_lock
    if _store is not None:
        return _store

    if _init_lock is None:
        _init_lock = asyncio.Lock()

    async with _init_lock:
        if _store is not None:
            return _store

        assert _db_path is not None
        logger.info("memdb_lazy_init db=%s", _db_path)

        from slife.logfmt import elapsed

        with elapsed("embedder_init", logger, level=logging.INFO):
            _embedder = EmbeddingClient.from_config()
        _store = SessionStore(_db_path)
        with elapsed("store_setup", logger, level=logging.INFO, db=str(_db_path)):
            model_id = f"{_embedder.backend}:{_embedder._model}" if _embedder.available else ""
            await _store.setup(
                embedding_dim=_embedder.dimension,
                embedding_model=model_id,
            )
        if _embedder.available:
            logger.info(
                "embeddings_ready backend=%s model=%s dim=%d",
                _embedder.backend, _embedder._model, _embedder.dimension,
            )
        else:
            logger.info("embeddings_disabled backend=%s", _embedder.backend if _embedder else "none")
        return _store


# ═══════════════════════════════════════════════════════════════════════
# Harness tools (programmatic only — not exposed to LLM)
# ═══════════════════════════════════════════════════════════════════════


def _repair_orphan_tool_results(messages: list[dict]) -> int:
    """Insert synthetic tool results for orphaned tool_calls.

    Guarantees a persisted turn never contains an assistant ``tool_call``
    without a matching ``tool`` result — that only happens when a request
    is interrupted mid-execution (e.g. a hung tool that never returned).
    Without this, an orphaned call survives in the DB and gets re-repaired
    on every session restore.

    Position-correct: each synthetic result is inserted immediately after
    the assistant message that owns the orphaned call.  Returns the number
    of synthetic results inserted.
    """
    if not messages:
        return 0
    resulted = {
        m.get("tool_call_id")
        for m in messages if m.get("role") == "tool"
    }
    repaired = 0
    out: list[dict] = []
    for m in messages:
        out.append(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tid = tc.get("id")
                if tid and tid not in resulted:
                    out.append({
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": "Error: request cancelled by user",
                    })
                    repaired += 1
    if repaired:
        messages[:] = out
    return repaired


@mcp.tool(name="__memory_save_turn", description="Save a turn. Harness-only.")
async def __memory_save_turn(
    user_message: str = "",
    messages: list[dict] | None = None,
    token_count: int = 0,
    who_helped: str = "",
    what_model: str = "",
    channel: str = "",
) -> str:
    store = await _ensure_store()
    try:
        # Invariant: never persist an orphaned tool_call.  The harness
        # already repairs its in-memory conversation, but this is the
        # single persistence choke point — guard regardless of caller.
        if messages:
            repaired = _repair_orphan_tool_results(messages)
            if repaired:
                logger.info("save_turn_repaired_orphans count=%d", repaired)

        rowid = await store.save_turn(
            user_message=user_message, messages=messages,
            token_count=token_count, who_helped=who_helped, what_model=what_model,
            channel=channel, embedder=_embedder,
        )
        return json.dumps({"rowid": rowid, "status": "saved"}, ensure_ascii=False)
    except Exception as e:
        logger.exception("save_turn_failed user_msg=%.80s", user_message)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(name="__memory_get_recent_turns", description="Load recent turns for restore. Harness-only.")
async def __memory_get_recent_turns(limit: int = 50) -> str:
    store = await _ensure_store()
    try:
        turns = await store.get_recent_turns(limit=limit)
        return json.dumps({"turns": turns}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("get_recent_turns_failed limit=%d", limit)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _count_unembedded() -> int:
    """Quick count of turns needing reindex (non-blocking if store is ready)."""
    if _store is None:
        return 0
    try:
        return await _store.count_unembedded()
    except Exception:
        return 0


async def _count_all_embedded() -> int:
    """Count already-embedded turns (non-blocking if store is ready)."""
    if _store is None:
        return 0
    try:
        assert _store._conn is not None
        cursor = await _store._conn.execute(
            "SELECT COUNT(DISTINCT diary_rowid) FROM diary_semantic",
        )
        row = await cursor.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


async def _reindex_impl(reset: bool = False, batch_limit: int = 10) -> dict:
    """Core reindex logic — shared by background reindex tasks.

    Returns a dict with total, indexed, remaining, complete.
    """
    store = await _ensure_store()
    if not _embedder or not _embedder.available:
        # If embedding is configured but the runtime check fails
        # (e.g. llama-cpp-python not installed yet), keep retrying.
        # If embedding was never configured, exit cleanly — the task
        # will be restarted by memory_set_embedding when needed.
        from slife.plugins.memdb.embedding_config import read_embedding_config
        cfg = read_embedding_config()
        if cfg and cfg.get("enabled", True):
            return {"complete": False, "reason": "embedder unavailable, will retry"}
        return {"complete": True, "reason": "embedder not configured"}

    if reset:
        cleared = await store.clear_all_embeddings()
        logger.info("reindex_reset cleared=%d", cleared)

    total = await store.count_unembedded()
    if total == 0:
        return {"total": 0, "indexed": 0, "remaining": 0, "complete": True}

    from slife.plugins.memdb.store import _chunk_text, _turn_text_for_embedding

    turns = await store.get_unembedded_turns(limit=batch_limit)
    indexed = 0
    for turn in turns:
        try:
            embed_text = _turn_text_for_embedding(
                turn["user_message"],
                json.loads(turn.get("messages", "[]")),
            )
            if embed_text.strip():
                chunks = _chunk_text(embed_text)
                valid = [c for c in chunks if len(c) // 4 <= _embedder.max_tokens]
                if valid:
                    embeddings = await _embedder.embed(valid)
                    if embeddings:
                        for idx, emb in enumerate(embeddings):
                            if emb:
                                await store.upsert_embedding(
                                    diary_rowid=turn["rowid"], chunk_index=idx,
                                    summary="", tags="",
                                    created_at=turn["created_at"],
                                    turn_embedding=emb,
                                )
            indexed += 1
        except Exception as e:
            logger.debug("reindex_skip rowid=%s err=%s", turn["rowid"], e)

    remaining = await store.count_unembedded()
    return {
        "total": total, "indexed": indexed,
        "remaining": remaining, "complete": remaining == 0,
    }


async def _background_reindex(reset: bool = False) -> None:
    """Run _reindex_impl in small batches until complete.

    Called by ``_reinit_store_after_model_change`` after the vec0 table
    has been migrated (or confirmed unchanged).  Each batch is small
    (5 turns) so it doesn't block the event loop.
    """
    import asyncio as _asyncio

    batches = 0
    if reset:
        try:
            cleared = await _reindex_impl(reset=True, batch_limit=0)
            logger.info("background_reindex_reset cleared=%d", cleared.get("total", 0))
        except Exception as e:
            logger.warning("background_reindex_reset_error err=%s", e)
    try:
        while True:
            result = await _reindex_impl(reset=False, batch_limit=5)
            batches += 1
            if result.get("complete"):
                logger.info("background_reindex_done batches=%d", batches)
                return
            await _asyncio.sleep(0.5)
    except Exception as e:
        logger.warning("background_reindex_aborted err=%s", e)


async def _reinit_store_after_model_change() -> None:
    """Close + re-setup the store so migration runs, then reindex.

    Must be called after ``reload_embedder()`` when the embedding model
    changed.  Runs as a background task — ``memory_set_embedding``
    returns immediately while this runs asynchronously.

    Sets ``_store = None`` first so concurrent ``_ensure_store`` calls
    see an uninitialized store and create a new one (protected by its
    own lock).  After migration, the background reindex populates the
    vec0 table with new-model vectors.
    """
    global _store
    if _embedder is None:
        return

    # Null out the global so concurrent _ensure_store calls know to
    # reinitialize.  The old connection is closed below.
    old_store = _store
    _store = None

    if old_store is not None:
        try:
            await old_store.close()
        except Exception as e:
            logger.debug("store_close_error err=%s", e)
        del old_store

    model_id = (
        f"{_embedder.backend}:{_embedder._model}"
        if _embedder.available else ""
    )
    logger.info(
        "store_reinit_start db=%s model=%s dim=%d",
        _db_path, model_id, _embedder.dimension,
    )

    new_store = SessionStore(_db_path)  # type: ignore[arg-type]
    from slife.logfmt import elapsed
    with elapsed("store_reinit", logger, level=logging.INFO, db=str(_db_path)):
        await new_store.setup(
            embedding_dim=_embedder.dimension,
            embedding_model=model_id,
        )
    _store = new_store
    logger.info("store_reinited model=%s dim=%d", model_id, _embedder.dimension)

    # Background reindex will populate the (possibly migrated) vec0 table.
    await _background_reindex(reset=False)


# ═══════════════════════════════════════════════════════════════════════
# LLM-visible tools
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="memory_list_recent",
    description=(
        "List recent memories (newest first): rowid, truncated user_message, "
        "summary, tags, created_at. Use memory_open for full content."
    ),
)
async def memory_list_recent(limit: int = 20) -> str:
    store = await _ensure_store()
    try:
        entries = await store.list_recent(limit=limit)
        for e in entries:
            um = e.get("user_message", "")
            if len(um) > 200:
                e["user_message"] = um[:200] + "…"
        return json.dumps(entries, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("list_recent_failed limit=%d", limit)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_count",
    description=(
        "Count memories. No params: total. since/until: count in an ISO time "
        "range (use 'since' alone for 'since last month'). query+mode: count "
        "search matches (grep/fts5)."
    ),
)
async def memory_count(
    since: str | None = None,
    until: str | None = None,
    query: str | None = None,
    mode: str = "fts5",
) -> str:
    store = await _ensure_store()
    try:
        result = await store.count_turns(
            since=since, until=until, query=query, mode=mode,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("count_failed query=%s mode=%s", query, mode)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_open",
    description=(
        "Load a memory by rowid: full messages (OpenAI JSON) incl. thinking, "
        "tool calls, tool results. rowid from memory_list_recent / memory_search."
    ),
)
async def memory_open(rowid: int) -> str:
    store = await _ensure_store()
    try:
        turn = await store.get_turn(rowid=rowid)
        if turn is None:
            return json.dumps(
                {"error": f"未找到 turn rowid={rowid}"}, ensure_ascii=False,
            )
        return json.dumps(turn, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("open_failed rowid=%s", rowid)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_search",
    description=(
        "Search memories (each result = one turn). "
        "Modes: 'grep' exact substring (error messages, file paths, code); "
        "'fts5' BM25 keyword ranking; 'hybrid' fts5 + semantic (default); "
        "'time' browse by date range, no query. "
        "since/until = ISO datetime — convert relative time ('yesterday' → date). "
        "Use memory_open for full turns."
    ),
)
async def memory_search(
    query: str = "",
    mode: str = "hybrid",
    limit: int = 10,
    since: str | None = None,
    until: str | None = None,
) -> str:
    store = await _ensure_store()
    mode = mode.lower()
    if mode not in ("grep", "fts5", "hybrid", "time"):
        mode = "hybrid"

    if mode == "time":
        try:
            hits = await store.search_time(limit=limit, since=since, until=until)
            return json.dumps({"mode": "time", "since": since, "until": until, "results": hits},
                              ensure_ascii=False, indent=2)
        except Exception as e:
            logger.exception("search_time_failed since=%s until=%s", since, until)
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    if not query.strip():
        return json.dumps({"error": "query 不能为空（time 模式不需要 query）"}, ensure_ascii=False)

    try:
        if mode == "grep":
            hits = await store.search_grep(pattern=query, limit=limit,
                                             since=since, until=until)
            return json.dumps({"mode": "grep", "query": query, "results": hits,
                               "hint": "" if hits else f"未找到包含 '{query}' 的记忆"},
                              ensure_ascii=False, indent=2)

        if mode == "fts5":
            hits = await store.search_keyword(query=query, limit=limit,
                                                since=since, until=until)
            return json.dumps({"mode": "fts5", "query": query, "results": hits,
                               "hint": "" if hits else f"未找到与 '{query}' 相关的记忆"},
                              ensure_ascii=False, indent=2)

        # hybrid
        keyword_hits = await store.search_keyword(query=query, limit=limit * 2,
                                                     since=since, until=until)
        semantic_hits: list[dict] = []
        semantic_available = False
        if _embedder and _embedder.available:
            emb = await _embedder.embed_one(query)
            if emb:
                semantic_hits = await store.search_semantic(embedding=emb,
                                                              limit=limit * 2,
                                                              since=since, until=until)
                semantic_available = True

        merged = merge_hybrid(keyword_hits, semantic_hits)

        # Build diagnostic hint when hybrid mode degrades to keyword-only.
        hint = ""
        if merged:
            if not semantic_available:
                hint = _hybrid_fallback_reason()
        else:
            hint = "没有找到相关的记忆"

        return json.dumps({
            "mode": "hybrid" if semantic_available else "fts5",
            "query": query,
            "results": merged[:limit],
            "hint": hint,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("search_failed query=%s mode=%s", query, mode)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_summarize",
    description=(
        "Write a summary (1-2 sentences) and comma-separated tags for a turn, "
        "making it findable via search. Both optional."
    ),
)
async def memory_summarize(
    rowid: int,
    summary: str | None = None, tags: str | None = None,
) -> str:
    store = await _ensure_store()
    try:
        await store.update_summary(rowid=rowid, summary=summary, tags=tags)

        if summary and _embedder and _embedder.available:
            try:
                emb = await _embedder.embed_one(summary)
                if emb:
                    assert store._conn is not None
                    cursor = await store._conn.execute(
                        "SELECT tags, created_at FROM diary WHERE rowid = ?",
                        (rowid,),
                    )
                    row = await cursor.fetchone()
                    if row:
                        # Replace old chunks (summary is short — one chunk)
                        await store._clear_chunks(rowid)
                        await store.upsert_embedding(
                            diary_rowid=rowid, chunk_index=0,
                            summary=summary, tags=tags or row["tags"] or "",
                            created_at=row["created_at"], turn_embedding=emb,
                        )
            except Exception as e:
                logger.debug("embedding_upsert_skipped err=%s", e)

        return json.dumps({"status": "已更新", "rowid": rowid}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("summarize_failed rowid=%s", rowid)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════
# Embedding config tools (unchanged)
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="memory_check_embedding",
    description=(
        "Embedding backend status + reindex progress: backend, model, dimension, "
        "available, unembedded count, hints."
    ),
)
async def memory_check_embedding() -> str:
    from slife.plugins.memdb.embedding_config import make_check_report
    try:
        report = make_check_report()
        unembedded = await _count_unembedded()
        report["unembedded"] = unembedded
        if unembedded > 0 and report.get("available"):
            report["hint"] = (
                report.get("hint", "") +
                f" 后台索引进行中 — {unembedded} 条 turn 待嵌入。"
            ).strip()
        return json.dumps(report, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("check_embedding_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_set_embedding",
    description=(
        "Configure the embedding backend (gguf/transformer/api) for hybrid search. "
        "Existing turns auto-reindex in the background; keyword search stays "
        "available meanwhile."
    ),
)
async def memory_set_embedding(
    backend: str = "",
    model: str = "bge-m3",
    gguf_path: str | None = None,
    dim: int = 0,
    device: str = "",
) -> str:
    """Configure the embedding backend.

    Args:
        backend: ``"gguf"``, ``"transformer"``, or ``"api"``.
        model: Model name. Default ``"bge-m3"``. For API backend this is
            the OpenAI model ID (e.g. ``"text-embedding-3-small"``).
        gguf_path: Path to .gguf file. Required when ``backend="gguf"``.
        dim: Explicit embedding dimension. Auto-detected when 0 (default).
        device: Device override for transformer backend
            (``"cpu"`` / ``"cuda"``). Auto-detect when empty.
    """
    from slife.plugins.memdb.embedding_config import (
        write_embedding_config, validate_gguf_path,
        get_first_provider_api_key, reload_embedder,
    )
    backend = backend.lower().strip()
    if backend not in ("gguf", "transformer", "api"):
        return json.dumps(
            {"error": f"不支持的后端 '{backend}'。可选: 'gguf'、'transformer' 或 'api'"},
            ensure_ascii=False, indent=2,
        )
    cfg: dict = {"model": model, "backend": backend, "enabled": True}
    if backend == "gguf":
        if not gguf_path:
            return json.dumps({"error": "GGUF 后端需要 gguf_path 参数"}, ensure_ascii=False, indent=2)
        ok, msg = validate_gguf_path(gguf_path)
        if not ok:
            return json.dumps({"error": f"GGUF 文件校验失败: {msg}"}, ensure_ascii=False, indent=2)
        cfg["gguf_path"] = msg
        if dim > 0:
            cfg["dim"] = dim
    elif backend == "transformer":
        # Model name is the HuggingFace model ID (e.g. "BAAI/bge-m3")
        if dim > 0:
            cfg["dim"] = dim
        if device:
            cfg["device"] = device
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

        # Fire store reinit + reindex as a background task so
        # memory_set_embedding returns immediately.  The task closes
        # the old store connection, re-runs setup (triggering vec0 table
        # migration if the model dimension or identity changed), then
        # reindexes all turns with the new model.
        global _reindex_task
        if _reindex_task and not _reindex_task.done():
            _reindex_task.cancel()
        _reindex_task = asyncio.create_task(_reinit_store_after_model_change())
        logger.info("background_reinit_and_reindex_started")
        status["hint"] = (
            "后台正在迁移向量表并重建索引，关键词搜索正常可用，"
            "语义搜索将在索引完成后恢复。"
        )

        return json.dumps(status, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("set_embedding_failed backend=%s model=%s", backend, model)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_set_enabled",
    description=(
        "Enable/disable semantic (hybrid) search. Disabling preserves embeddings; "
        "re-enabling re-indexes turns saved meanwhile."
    ),
)
async def memory_set_enabled(enabled: bool) -> str:
    from slife.plugins.memdb.embedding_config import set_embedding_enabled, reload_embedder
    try:
        ok = set_embedding_enabled(enabled)
        if not ok:
            return json.dumps(
                {"error": "没有已配置的 embedding。先使用 memory_set_embedding 配置。"},
                ensure_ascii=False,
            )
        status = await reload_embedder()

        if enabled:
            global _reindex_task
            if _reindex_task and not _reindex_task.done():
                _reindex_task.cancel()
            _reindex_task = asyncio.create_task(_background_reindex())
            unembedded = await _count_unembedded()
            status["message"] = "Semantic search enabled."
            if unembedded > 0:
                status["reindex"] = f"Background reindex started, {unembedded} items pending"
        else:
            embedded_count = await _count_all_embedded()
            status["message"] = "Semantic search disabled. Keyword search still available."
            if embedded_count > 0:
                status["preserved"] = f"{embedded_count} existing embeddings preserved."
        return json.dumps(status, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("set_enabled_failed enabled=%s", enabled)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── Entry point ──────────────────────────────────────────────────────


def main():
    """Run the slife-memdb server on Streamable HTTP transport.

    Store and embedder are lazily initialised on the first tool call
    INSIDE FastMCP's event loop — this avoids the aiosqlite connection
    being bound to a temporary loop that gets destroyed by asyncio.run().
    """
    import argparse

    from slife.server_utils import run_plugin_server, shutdown_server_logging

    global _db_path

    parser = argparse.ArgumentParser(description="slife-memdb server")
    parser.add_argument("--db", default=None)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    _db_path = Path(args.db).expanduser() if args.db else _get_db_path()

    logger.info(
        "memdb_start log=%s pid=%s db=%s", _log_path, os.getpid(), _db_path,
    )

    try:
        run_plugin_server(mcp, port=args.port)
    finally:
        logger.info("memdb_stop log=%s pid=%s db=%s", _log_path, os.getpid(), _db_path)
        shutdown_server_logging()


if __name__ == "__main__":
    main()
