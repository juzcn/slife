"""slife-memdb server — FastMCP server for the Turns DB (turn-based memory).

Each turn (user message + assistant response) is an independent,
immutable row addressed by its turn id.  No sessions, no lifecycle —
just turns.  Restore loads the most recent N turns by id.

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
from slife.plugins.memdb.search import (
    SCORE_BAND_HINT, annotate_scores, merge_hybrid,
)
from slife.plugins.memdb.semantic import SemanticManager
from slife.server_utils import create_plugin_server, warm_after_handshake


@asynccontextmanager
async def _memdb_lifespan(_app):
    """Initialise the store, then serve; graceful shutdown.

    Readiness (MCP plugin contract): the store must be able to serve before
    the server answers the harness's ``initialize`` — an unusable store is
    fatal to startup.  It raises here, so the lifespan fails, the port signal
    never fires, and the harness reports the plugin FAILED instead of
    serving broken.
    """
    await _ensure_store_ready()
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


async def _ensure_store_ready() -> None:
    """Establish the plugin's serving capacity (the turn store).

    The readiness requirement encoded in initialization: the store can serve
    — connection open, schema in place, a query succeeds.  A failure here is
    fatal (the lifespan raises, so ``initialize`` never completes); runtime
    self-healing is no longer available, the harness's watchdog retries the
    whole process instead.
    """
    try:
        store = await _ensure_store()
        async with store._c.execute("SELECT 1") as cur:
            await cur.fetchone()
    except Exception as e:
        logger.error("store_unusable_at_startup err=%s", e)
        raise


mcp, _log_path, logger = create_plugin_server(
    "slife-memdb",
    instructions=(
        "slife-memdb — the Turns DB: turn-based long-term knowledge. "
        "Every turn (user question + your response) is one row, addressed by "
        "its turn id. "
        "LLM-visible tools: turn_list, turn_search (grep/fts5/hybrid/time), "
        "turn_read, turn_token_usage, turn_count, turn_summarize, "
        "memdb_semantic_status. "
        "All data is automatically scoped to the current agent."
    ),
    lifespan=_memdb_lifespan,
)

_store: SessionStore | None = None
_manager: SemanticManager | None = None
_db_path: Path | None = None
_init_lock: asyncio.Lock | None = None


def _rename_rowid_to_turn_id(entries: list[dict]) -> None:
    """Map a store result's internal ``rowid`` key to the model-visible
    ``turn_id``, in place.  The store layer uses the SQLite rowid; the LLM
    sees and addresses turns by their turn id.  Also drops ``diary_rowid`` —
    the semantic search's dedup key, which holds the same value and is
    internal noise for the model."""
    for e in entries:
        if "rowid" in e:
            e["turn_id"] = e.pop("rowid")
        e.pop("diary_rowid", None)


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

    # Probe the embedding config just to size the vec0 table up front.
    # Only defer (create vec0 with the real width after the model loads)
    # when the width can't be trusted yet: transformer reports its dim only
    # once loaded, and an unknown gguf/api model carries a provisional guess
    # that would silently drop every mis-sized embedding.  The model itself
    # is loaded later by SemanticManager.enable() in the background — saves
    # never wait on it.
    probe = EmbeddingClient.from_config()
    defer_vec0 = bool(
        probe.available
        and (probe.backend == "transformer" or not probe.dimension_known)
    )
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
    return _store


# Warm the semantic manager only AFTER the first tools/list completed the
# MCP handshake: the llama_cpp model load holds the GIL and would freeze
# the startup path (port signal / initialize) if it ran from the lifespan.
# Handshake-first keeps readiness intact; a slow or failed load stays a
# warning (keyword search still works) — never a startup gate.
async def _warm_semantic() -> None:
    manager = _manager
    if manager is None:
        return
    await manager.start()


warm_after_handshake(mcp, _warm_semantic, name="semantic")


# ═══════════════════════════════════════════════════════════════════════
# Harness tools (programmatic only — not exposed to LLM)
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(name="__memory_save_turn", description="Save a turn. Internal — called by the agent loop.")
async def __memory_save_turn(
    user_message: str = "",
    messages: list[dict] | None = None,
    token_count: int = 0,
    prompt_tokens: int = 0,
    who_helped: str = "",
    what_model: str = "",
    channel: str = "",
    channel_data: str = "{}",
    created_at: str | None = None,
    completed_at: str | None = None,
    summary: str | None = None,
    tags: str | None = None,
) -> str:
    try:
        # Hold the lifecycle lock across the save so a concurrent store
        # build cannot race the insert.
        async with _get_init_lock():
            store = await _ensure_store_locked()
            rowid = await store.save_turn(
                user_message=user_message, messages=messages,
                token_count=token_count,
                prompt_tokens=prompt_tokens,
                who_helped=who_helped, what_model=what_model,
                channel=channel, channel_data=channel_data,
                created_at=created_at,
                completed_at=completed_at,
            )
            # A rowid-less turn_summarize captured the current turn's
            # annotation — apply it to the row just written (best-effort).
            if summary is not None or tags is not None:
                await store.update_summary(
                    rowid=rowid, summary=summary, tags=tags,
                )
        # A saved turn is new work for the index drainer — wake it
        # (non-blocking event.set(), no DB work).
        if _manager is not None:
            _manager.on_saved()
        return json.dumps({"turn_id": rowid, "status": "saved"}, ensure_ascii=False)
    except Exception as e:
        logger.exception("save_turn_failed user_msg=%.80s", user_message)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(name="__memory_get_recent_turns", description="Load recent turns for restore. Internal — called by the main process.")
async def __memory_get_recent_turns(limit: int = 50, after_rowid: int = 0) -> str:
    store = await _ensure_store()
    try:
        turns = await store.get_recent_turns(limit=limit, after_rowid=after_rowid)
        return json.dumps({"turns": turns}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("get_recent_turns_failed limit=%d", limit)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="__memory_reload_semantic",
    description="Reload the semantic index after an embeddings config change. Internal — called by the harness.",
)
async def __memory_reload_semantic(enabled: bool = True) -> str:
    """Re-read the shared embeddings config and rebuild (or tear down) the
    semantic index.  ``enabled=True`` → ``SemanticManager.enable()`` (stops
    the drainer, migrates vec0 in place, restarts the drainer); ``False`` →
    ``disable()`` (stops the drainer, keeps embeddings on disk).  Called by
    the harness's ``embeddings_*`` native tools after a config change."""
    try:
        manager = await _ensure_manager_for_reload()
        if enabled:
            status = await manager.enable()
            status["status"] = "reloaded"
            status["message"] = "Semantic index reloaded (reindexing in background)."
        else:
            status = await manager.disable()
            status["status"] = "disabled"
        return json.dumps(status, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("memory_reload_semantic_failed enabled=%s", enabled)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _ensure_manager_for_reload() -> SemanticManager:
    """Return a live SemanticManager for the reload tool.

    The store + manager are built lazily on first tool call; a reload after
    startup must ensure they exist so the manager can re-read the config.
    """
    async with _get_init_lock():
        await _ensure_store_locked()
    assert _manager is not None
    return _manager


@mcp.tool(
    name="__memory_context_start_advance",
    description="Advance the persisted live-context start by count rows. Internal — called by the agent loop.",
)
async def __memory_context_start_advance(count: int) -> str:
    """Persist the live-context boundary after the internal trim removed
    *count* oldest turns.  Restore starts where the boundary points, so
    startup rebuilds the exit-time context instead of re-slicing 20%."""
    try:
        async with _get_init_lock():
            store = await _ensure_store_locked()
            boundary = await store.advance_context_start(count)
            if _manager is not None:
                _manager.on_saved()
        return json.dumps({"context_start": boundary}, ensure_ascii=False)
    except Exception as e:
        logger.exception("context_start_advance_failed count=%d", count)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="__memory_context_start_latest",
    description="Move the live-context start to the latest saved turn. Internal — called by the agent loop.",
)
async def __memory_context_start_latest() -> str:
    """Flush the boundary to the latest row — the fresh start after
    ``clear_context``.  Only turns saved afterwards come back on restore."""
    try:
        async with _get_init_lock():
            store = await _ensure_store_locked()
            boundary = await store.set_context_start_latest()
            if _manager is not None:
                _manager.on_saved()
        return json.dumps({"context_start": boundary}, ensure_ascii=False)
    except Exception as e:
        logger.exception("context_start_latest_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════
# LLM-visible tools
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="turn_list",
    description=(
        "List turns (newest first): turn id, truncated user_message, summary, "
        "tags, created_at. Use turn_read for full content. "
        "before_turn_id / after_turn_id anchor the window by turn id (exclusive): "
        "page older than a [INFO: {\"turn_id\": N, …}] footnote with before_turn_id, newer "
        "with after_turn_id."
    ),
)
async def turn_list(
    limit: int = 20,
    before_turn_id: int | None = None,
    after_turn_id: int | None = None,
) -> str:
    """List turns, newest first.

    Args:
        limit: Maximum number of turns to return.
        before_turn_id: Only turns with id < this (older) — page back from a turn you can see in context.
        after_turn_id: Only turns with id > this (newer).
    """
    store = await _ensure_store()
    try:
        entries = await store.list_recent(
            limit=limit,
            before_rowid=before_turn_id, after_rowid=after_turn_id,
        )
        for e in entries:
            um = e.get("user_message", "")
            if len(um) > 200:
                e["user_message"] = um[:200] + "…"
        return json.dumps(entries, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("list_recent_failed limit=%d", limit)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="turn_token_usage",
    description=(
        "Token consumption per turn. Options: turn_id (one turn), since/until "
        "(ISO datetime time range), limit (cap on turns returned, default 50). "
        "Each turn reports token_count (cumulative billed tokens) and "
        "prompt_tokens (context size at the last call). Returns the turns "
        "plus a summary of totals/averages across the filtered set."
    ),
)
async def turn_token_usage(
    turn_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> str:
    """Token consumption by turn, filtered by turn id or time range.

    Args:
        turn_id: Restrict to a single turn by its id.
        since: Lower bound, ISO datetime (relative words like 'yesterday' accepted).
        until: Upper bound, ISO datetime.
        limit: Maximum number of turns to return (newest first).
    """
    store = await _ensure_store()
    try:
        result = await store.token_usage(
            rowid=turn_id, since=since, until=until, limit=limit,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("token_usage_failed turn_id=%s", turn_id)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="turn_count",
    description=(
        "Count turns. No params: total. since/until: count in an ISO time "
        "range (use 'since' alone for 'since last month'). query+mode: count "
        "search matches (grep/fts5)."
    ),
)
async def turn_count(
    since: str | None = None,
    until: str | None = None,
    query: str | None = None,
    mode: str = "fts5",
) -> str:
    """Count turns.

    Args:
        since: Lower bound, ISO datetime. "since" alone = count since that time.
        until: Upper bound, ISO datetime.
        query: Search text to count matches for (grep/fts5 modes).
        mode: Search mode for counting: grep or fts5 (default fts5).
    """
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
    name="turn_read",
    description=(
        "Load a turn by turn id: full messages (OpenAI JSON) incl. thinking, "
        "tool calls, tool results. The id comes from turn_list / "
        "turn_search (each result carries the turn id)."
    ),
)
async def turn_read(turn_id: int) -> str:
    """Load a full turn by turn id.

    Args:
        turn_id: The turn id (same id as a `[INFO: {"turn_id": N, …}]` footnote or a
            turn_list / turn_search result).
    """
    store = await _ensure_store()
    try:
        turn = await store.get_turn(rowid=turn_id)
        if turn is None:
            return json.dumps(
                {"error": f"turn not found turn_id={turn_id}"}, ensure_ascii=False,
            )
        if "rowid" in turn:
            turn["turn_id"] = turn.pop("rowid")
        return json.dumps(turn, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("open_failed turn_id=%s", turn_id)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="turn_search",
    description=(
        "Search turns (each result = one turn, carrying its turn id). "
        "Modes: 'grep' exact substring, 'fts5' BM25 keyword, 'hybrid' "
        "fts5 + semantic (default), 'time' browse by date range (no query). "
        "Use turn_read with the result's turn id for full turns."
    ),
)
async def turn_search(
    query: str = "",
    mode: str = "hybrid",
    limit: int = 10,
    since: str | None = None,
    until: str | None = None,
) -> str:
    """Search memories (each result = one turn).

    Args:
        query: The search text. Required except for mode="time".
        mode: grep | fts5 | hybrid (default) | time. See description.
        limit: Maximum results.
        since: Lower bound, ISO datetime (relative words like 'yesterday' accepted).
        until: Upper bound, ISO datetime.
    """
    store = await _ensure_store()
    # Search only READS the semantic gate — no side effects, no reindex kick.
    manager = _manager
    mode = mode.lower()
    if mode not in ("grep", "fts5", "hybrid", "time"):
        mode = "hybrid"
    # Clamp before use — the store methods clamp internally, but the hybrid
    # final slice (`merged[:limit]`) and time mode use the raw LLM value, so a
    # limit of 0 (→ []) or a negative (→ slices from the tail) would slip
    # through.
    limit = _clamp_limit(limit)

    if mode == "time":
        try:
            hits = await store.search_time(limit=limit, since=since, until=until)
            _rename_rowid_to_turn_id(hits)
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
            _rename_rowid_to_turn_id(hits)
            return json.dumps({"mode": "grep", "query": query, "results": hits,
                               "hint": "" if hits else f"no memories contain '{query}'"},
                              ensure_ascii=False, indent=2)

        if mode == "fts5":
            hits = await store.search_keyword(query=query, limit=limit,
                                              since=since, until=until)
            _rename_rowid_to_turn_id(hits)
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

        # The store keys both hit lists on the internal ``rowid`` — rename
        # to the model-visible ``turn_id`` BEFORE the RRF merge, which aligns
        # on ``turn_id``.  Otherwise every item's key lookup misses and the
        # merge collapses to empty (keyword=6 semantic=6 merged=0).  This
        # also keeps hybrid results keyed like every other turn_search mode.
        _rename_rowid_to_turn_id(keyword_hits)
        _rename_rowid_to_turn_id(semantic_hits)

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

        results = merged[:limit]
        if semantic_available and results:
            annotate_scores(results)
            hint = SCORE_BAND_HINT if not hint else f"{hint} · {SCORE_BAND_HINT}"

        return json.dumps({
            "mode": "hybrid" if semantic_available else "fts5",
            "query": query,
            "results": results,
            "hint": hint,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("search_failed query=%s mode=%s", query, mode)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="turn_summarize",
    description=(
        "Write a summary (1-2 sentences) and comma-separated tags for a turn, "
        "making it findable via keyword search. Both optional. "
        "Omit the turn id to annotate the CURRENT turn (applied when it "
        "completes; call during the turn). "
        "Does NOT touch the semantic index."
    ),
)
async def turn_summarize(
    turn_id: int | None = None,
    summary: str | None = None, tags: str | None = None,
) -> str:
    """Write a summary and tags for a turn, making it findable by keyword search.

    Args:
        turn_id: The turn id to annotate. Omit to annotate the current
            (in-flight) turn — applied when it completes and is saved.
        summary: A 1-2 sentence summary of the turn.
        tags: Comma-separated tags for keyword search.
    """
    store = await _ensure_store()
    try:
        if turn_id is None:
            # Current turn — captured at save time: save_to_memory extracts
            # this call and passes summary/tags to __memory_save_turn.  No
            # latest_rowid lookup (cross-source race), no write here.
            return json.dumps({
                "status": "captured",
                "turn_id": None,
                "message": (
                    "annotation captured for the current turn — applied when "
                    "the turn completes"
                ),
            }, ensure_ascii=False, indent=2)
        await store.update_summary(rowid=turn_id, summary=summary, tags=tags)
        return json.dumps({"status": "updated", "turn_id": turn_id}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("summarize_failed turn_id=%s", turn_id)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════
# Embedding config tools
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="memdb_semantic_status",
    description=(
        "Semantic index status for the Turns DB (memdb): model, "
        "dimension, available, semantic_ready (the search gate), state, "
        "unembedded, hint. Independent from memfiles_semantic_status — each "
        "plugin reindexes its own DB, so one can be semantically ready while "
        "the other is still building."
    ),
)
async def memdb_semantic_status() -> str:
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
                report["model"] = e._model
                report["dimension"] = e.dimension
                report["available"] = e.available
                report["loaded"] = e.loaded
            # hint by state
            state = manager.state
            if state == "ready":
                report["hint"] = (
                    f"Embedding model ready: "
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
                    "Semantic search disabled — enable with "
                    "embeddings_enable true, or edit the top-level "
                    "embeddings section in slife.json5."
                )
        return json.dumps(report, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("check_embedding_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="__check",
    description=(
        "Turns DB + semantic-search status as health entries. Internal — "
        "probed by the harness's system_health, never exposed to the LLM."
    ),
)
async def __check() -> str:
    """Return health-check entries for the turns DB + embedding status.

    Internal (``__`` prefix): probed by the harness's ``system_health``.
    The LLM has ``memdb_semantic_status`` for the semantic index; this is
    the technical status surface the harness aggregates.
    """
    from slife.plugins.memdb.embedding_config import make_check_report

    results: list[dict] = []

    # ── Database file ─────────────────────────────────────────────
    db_path = _db_path if _db_path is not None else _get_db_path()
    if db_path.exists():
        size_mb = db_path.stat().st_size / (1024 * 1024)
        results.append({
            "component": "memdb", "level": "ok", "key": "db",
            "value": f"{size_mb:.1f} MB",
            "hint": f"Database ready: {db_path}",
        })
    else:
        results.append({
            "component": "memdb", "level": "warning", "key": "db",
            "value": "not found",
            "hint": (f"Database file not found at {db_path}. "
                     "Will be created on first memory write."),
        })

    # ── Semantic search ───────────────────────────────────────────
    try:
        await _ensure_store()
        manager = _manager
        report = make_check_report()
        if manager is not None:
            report["semantic_ready"] = manager.semantic_ready
            report["state"] = manager.state
            report["reason"] = manager.reason
            report["unembedded"] = await manager.unembedded()
            e = manager.embedder
            if e is not None:
                # live embedder facts override the config probe
                report["model"] = e._model
                report["dimension"] = e.dimension
                report["available"] = e.available
                report["loaded"] = e.loaded
        if report.get("configured") is False or not report.get("available"):
            results.append({
                "component": "memdb", "level": "warning", "key": "embedding",
                "value": "unavailable",
                "hint": report.get("hint")
                        or "Semantic search unavailable; keyword search works.",
            })
        elif report.get("semantic_ready"):
            results.append({
                "component": "memdb", "level": "ok", "key": "embedding",
                "value": "ready",
                "hint": (f"Semantic search ready "
                         f"({report.get('model', '?')}, "
                         f"dim={report.get('dimension')})."),
            })
        else:
            results.append({
                "component": "memdb", "level": "warning", "key": "embedding",
                "value": report.get("state", "building"),
                "hint": report.get("hint") or "Semantic index building.",
            })
    except Exception as e:
        logger.warning("memdb_check_failed err=%s", e)
        results.append({
            "component": "memdb", "level": "warning", "key": "embedding",
            "value": "unavailable",
            "hint": f"Semantic status unavailable: {e}",
        })
    return json.dumps(results, ensure_ascii=False, indent=2)


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
