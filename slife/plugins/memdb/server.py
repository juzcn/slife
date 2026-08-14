"""slife-memdb server — FastMCP server for turn-based permanent memory.

Each turn (user message + assistant response) is an independent,
immutable row.  No sessions, no lifecycle — just turns.
Restore loads the most recent N turns by rowid.

The semantic-search lifecycle (embedder, index-completeness gate, index
drainer) is owned by ``SemanticManager`` (semantic.py).  This module only
wires the store and the MCP tools to it — no scattered lifecycle globals.

Usage:
    uv run python -m slife.plugins.memdb.server       # auto-assigned port (Streamable HTTP)
    uv run python -m slife.plugins.memdb.server --port 9877   # fixed port
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from slife.paths import get_data_dir
from slife.plugins.memdb.store import SessionStore, _clamp_limit
from slife.plugins.memdb.embeddings import EmbeddingClient
from slife.plugins.memdb.search import merge_hybrid
from slife.plugins.memdb.semantic import SemanticManager
from slife.server_utils import create_plugin_server


@asynccontextmanager
async def _memdb_lifespan(_app):
    """Initialise the store, then serve; graceful shutdown.

    Best-effort: if the store fails to init, the server still starts and
    retries lazily on the first tool call.
    """
    try:
        await _ensure_store()
    except Exception as e:
        logger.warning("eager_store_init_error err=%s", e)
    try:
        yield
    finally:
        global _store, _manager
        # Stop the semantic drainer BEFORE closing the store connection —
        # a canceled drainer must never write to a closed handle.
        if _manager is not None:
            await _manager.close()
            _manager = None
        if _store is not None:
            await _store.close()
            _store = None


mcp, _log_path, logger = create_plugin_server(
    "slife-memdb",
    instructions=(
        "slife-memdb — turn-based long-term knowledge. "
        "Every turn (user question + your response) is one row. "
        "LLM-visible tools: memory_list_recent, memory_search (grep/fts5/hybrid/time), "
        "memory_open, memory_summarize, memory_check/set/remove_embedding. "
        "All data is automatically scoped to the current agent."
    ),
    lifespan=_memdb_lifespan,
)

_store: SessionStore | None = None
_manager: SemanticManager | None = None
_db_path: Path | None = None
_init_lock: asyncio.Lock | None = None


def _get_db_path() -> Path:
    """Return the database path for the current agent.

    Uses ``SLIFE_DATA_DIR`` (set by the main process) so dev and
    production environments each get their own location.
    """
    agent_name = os.environ.get("SLIFE_AGENT_NAME", "slife")
    env_path = os.environ.get("SLIFE_MEMDB_DB")
    if env_path:
        return Path(env_path)
    data_dir = get_data_dir()
    return data_dir / f"{agent_name}.db"


def _get_init_lock() -> asyncio.Lock:
    """Return (creating if needed) the lock guarding store lifecycle.

    Serializes store creation and store writes so a concurrent
    ``_ensure_store`` can never build two stores.
    """
    global _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    return _init_lock


async def _ensure_store() -> SessionStore:
    """Lazy-init the store inside FastMCP's event loop.

    This MUST run inside ``mcp.run()``'s event loop — ``asyncio.run()``
    creates a temporary loop that gets destroyed, causing ``aiosqlite``
    operations to hang forever because their background thread is bound
    to a loop that no longer exists.
    """
    if _store is not None:
        return _store
    async with _get_init_lock():
        return await _ensure_store_locked()


async def _ensure_store_locked() -> SessionStore:
    """Build the store if needed. Caller must already hold ``_init_lock``."""
    global _store, _manager
    if _store is not None:
        return _store

    assert _db_path is not None
    logger.info("memdb_init db=%s", _db_path)

    from slife.logfmt import elapsed

    # Probe the embedding config just to size the vec0 table up front
    # (gguf/api know their dim; transformer defers until the model loads).
    # The model itself is loaded later by SemanticManager.enable() in the
    # background — saves never wait on it.
    probe = EmbeddingClient.from_config()
    defer_vec0 = bool(probe.available and probe.backend == "transformer")
    dim = 0 if defer_vec0 else (probe.dimension if probe.available else 0)
    model_id = (
        f"{probe.backend}:{probe._model}"
        if probe.available and not defer_vec0 else ""
    )

    _store = SessionStore(_db_path)
    with elapsed("store_setup", logger, level=logging.INFO, db=str(_db_path)):
        await _store.setup(embedding_dim=dim, embedding_model=model_id)
    logger.info("embeddings_configured=%s backend=%s model=%s",
                probe.available, probe.backend, probe._model)

    _manager = SemanticManager(_store)
    asyncio.create_task(_manager.start())
    return _store


# ═══════════════════════════════════════════════════════════════════════
# Harness tools (programmatic only — not exposed to LLM)
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(name="__memory_save_turn", description="Save a turn. Harness-only.")
async def __memory_save_turn(
    user_message: str = "",
    messages: list[dict] | None = None,
    images: list[str] | None = None,
    token_count: int = 0,
    who_helped: str = "",
    what_model: str = "",
    channel: str = "",
    created_at: str | None = None,
    completed_at: str | None = None,
) -> str:
    try:
        # Hold the lifecycle lock across the save so a concurrent store
        # build cannot race the insert.
        async with _get_init_lock():
            store = await _ensure_store_locked()
            rowid = await store.save_turn(
                user_message=user_message, messages=messages,
                images=images, token_count=token_count,
                who_helped=who_helped, what_model=what_model,
                channel=channel, created_at=created_at,
                completed_at=completed_at,
            )
        # A saved turn is new work for the index drainer — wake it
        # (non-blocking event.set(), no DB work).
        if _manager is not None:
            _manager.on_turn_saved()
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
                {"error": f"turn not found rowid={rowid}"}, ensure_ascii=False,
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
    # Search only READS the semantic gate — no side effects, no reindex kick.
    manager = _manager
    mode = mode.lower()
    if mode not in ("grep", "fts5", "hybrid", "time"):
        mode = "hybrid"
    # Clamp before use — the store methods clamp internally, but the hybrid
    # final slice (`merged[:limit]`) and time mode use the raw LLM value, so a
    # limit of 0 (→ []) or a negative (→ slices from the tail) would slip
    # through (REVIEW §1-10).
    limit = _clamp_limit(limit)

    if mode == "time":
        try:
            hits = await store.search_time(limit=limit, since=since, until=until)
            return json.dumps({"mode": "time", "since": since, "until": until, "results": hits},
                              ensure_ascii=False, indent=2)
        except Exception as e:
            logger.exception("search_time_failed since=%s until=%s", since, until)
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    if not query.strip():
        return json.dumps({"error": "query must not be empty (time mode needs no query)"}, ensure_ascii=False)

    try:
        if mode == "grep":
            hits = await store.search_grep(pattern=query, limit=limit,
                                           since=since, until=until)
            return json.dumps({"mode": "grep", "query": query, "results": hits,
                               "hint": "" if hits else f"no memories contain '{query}'"},
                              ensure_ascii=False, indent=2)

        if mode == "fts5":
            hits = await store.search_keyword(query=query, limit=limit,
                                              since=since, until=until)
            return json.dumps({"mode": "fts5", "query": query, "results": hits,
                               "hint": "" if hits else f"no memories related to '{query}'"},
                              ensure_ascii=False, indent=2)

        # hybrid
        keyword_hits = await store.search_keyword(query=query, limit=limit * 2,
                                                  since=since, until=until)
        semantic_hits: list[dict] = []
        semantic_available = False
        if (
            manager is not None
            and manager.semantic_ready
            and manager.embedder is not None
            and manager.embedder.available
        ):
            emb = await manager.embedder.embed_one(query)
            if emb:
                semantic_hits = await store.search_semantic(embedding=emb,
                                                            limit=limit * 2,
                                                            since=since, until=until)
                semantic_available = True

        merged = merge_hybrid(keyword_hits, semantic_hits)

        # Surface the degradation reason even when no keyword hits survive —
        # an empty result is exactly when a silent fallback would mislead.
        hint = ""
        if not semantic_available:
            if manager is not None and manager.semantic_ready:
                hint = ("hybrid degraded to fts5 — query embedding generation "
                        "failed (API error or timeout). Check the API key, or "
                        "switch to a local model")
            else:
                hint = manager.reason if manager else (
                    "hybrid degraded to fts5 — embedding backend unavailable")
            if not merged:
                hint += " — no keyword (fts5) matches either"
        elif not merged:
            hint = "no matching memories found"

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
        if summary and _manager is not None:
            try:
                # Re-embed from the summary, serialized with the drainer.
                await _manager.reembed_summary(rowid, summary, tags or "")
            except Exception as e:
                logger.debug("embedding_upsert_skipped err=%s", e)
        return json.dumps({"status": "updated", "rowid": rowid}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("summarize_failed rowid=%s", rowid)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════
# Embedding config tools
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="memory_check_embedding",
    description=(
        "Embedding backend status + index state: backend, model, dimension, "
        "available, semantic_ready (the search gate), state, unembedded, hint."
    ),
)
async def memory_check_embedding() -> str:
    from slife.plugins.memdb.embedding_config import make_check_report
    await _ensure_store()
    manager = _manager
    try:
        report = make_check_report()
        if manager is not None:
            report["semantic_ready"] = manager.semantic_ready
            report["state"] = manager.state
            report["reason"] = manager.reason
            report["unembedded"] = await manager.unembedded()
            e = manager.embedder
            if e is not None:
                # live embedder facts override the config probe
                report["backend"] = e.backend
                report["model"] = e._model
                report["dimension"] = e.dimension
                report["available"] = e.available
                report["loaded"] = e.loaded
            # hint by state
            state = manager.state
            if state == "ready":
                report["hint"] = (
                    f"{report.get('backend', '')} embedding model ready: "
                    f"{report.get('model', '')} (dim={report.get('dimension')})"
                )
            elif state in ("loading", "indexing"):
                report["hint"] = (
                    f"Semantic index building — {report.get('unembedded', 0)} "
                    "turns pending embedding. Keyword search remains available."
                )
            elif state == "stalled":
                report["hint"] = report.get("reason", "Semantic index stalled.")
            elif state == "disabled" and report.get("configured"):
                report["hint"] = (
                    "Semantic search disabled. Re-enable with "
                    "memory_set_enabled true, or reconfigure with "
                    "memory_set_embedding."
                )
        return json.dumps(report, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("check_embedding_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_set_embedding",
    description=(
        "Configure the embedding backend (gguf/transformer/api) for hybrid search. "
        "Existing turns auto-reindex in the background; keyword search stays "
        "available meanwhile. BLOCKS until the model is loaded."
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
        write_embedding_config, validate_gguf_path, get_first_provider_api_key,
    )
    backend = backend.lower().strip()
    if backend not in ("gguf", "transformer", "api"):
        return json.dumps(
            {"error": f"unsupported backend '{backend}'. Options: 'gguf', 'transformer', or 'api'"},
            ensure_ascii=False, indent=2,
        )
    cfg: dict = {"model": model, "backend": backend, "enabled": True}
    if backend == "gguf":
        if not gguf_path:
            return json.dumps({"error": "GGUF backend requires a gguf_path parameter"}, ensure_ascii=False, indent=2)
        ok, msg = validate_gguf_path(gguf_path)
        if not ok:
            return json.dumps({"error": f"GGUF file validation failed: {msg}"}, ensure_ascii=False, indent=2)
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
            return json.dumps({"error": "API backend requires an api_key"}, ensure_ascii=False, indent=2)
        if dim > 0:
            cfg["dim"] = dim
    try:
        write_embedding_config(cfg)
        await _ensure_store()  # builds the store + manager if needed
        manager = _manager
        assert manager is not None
        status = await manager.enable()  # BLOCKING: model load + migration
        status["backend"] = backend
        status["model"] = model
        if gguf_path:
            status["gguf_path"] = gguf_path
        status["hint"] = (
            "Semantic search resumes automatically when indexing finishes; "
            "keyword search remains available."
        )
        return json.dumps(status, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("set_embedding_failed backend=%s model=%s", backend, model)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="memory_set_enabled",
    description=(
        "Enable/disable semantic (hybrid) search. Disabling preserves embeddings; "
        "re-enabling re-indexes turns saved meanwhile. Enable blocks until the "
        "model is loaded."
    ),
)
async def memory_set_enabled(enabled: bool) -> str:
    from slife.plugins.memdb.embedding_config import set_embedding_enabled
    try:
        ok = set_embedding_enabled(enabled)
        if not ok:
            return json.dumps(
                {"error": "No embedding configured. Run memory_set_embedding first."},
                ensure_ascii=False,
            )
        store = await _ensure_store()
        manager = _manager
        assert manager is not None

        if enabled:
            status = await manager.enable()
            status["message"] = "Semantic search enabled."
            status["hint"] = (
                "Verifying the index for the current model (detects manual "
                "json5 config changes). Semantic search resumes when indexing "
                "finishes."
            )
        else:
            status = await manager.disable()
            status["message"] = "Semantic search disabled. Keyword search still available."
            embedded = await store.count_embedded()
            if embedded > 0:
                status["preserved"] = f"{embedded} existing embeddings preserved."
        return json.dumps(status, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("set_enabled_failed enabled=%s", enabled)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── Entry point ──────────────────────────────────────────────────────


def main():
    """Run the slife-memdb server on Streamable HTTP transport.

    The store is initialised eagerly in the lifespan (inside FastMCP's event
    loop — this avoids the aiosqlite connection being bound to a temporary
    loop that gets destroyed by asyncio.run()).  The semantic manager is
    started as a background task — the model loads without blocking startup,
    and saves never wait on it.
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
