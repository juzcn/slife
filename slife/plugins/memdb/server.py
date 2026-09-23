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
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from slife.agent.message_history import TokenizerUnavailable, estimate_turn_tokens
from slife.plugins.memdb.recall import RecallPolicy, fit_budget, gate_turns
from slife.plugins.memdb.store import SessionStore, _clamp_limit
from slife.plugins.memdb.search import (
    hybrid_hint, rename_rowid_to_turn_id, run_hybrid,
)
from slife.plugins.memdb.semantic import SemanticManager
from slife.server_utils import create_plugin_server, warm_after_ready
from slife.timeutil import BOUND_GRAMMAR, InvalidTimeBound


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
        "LLM-visible tools: turn_search, turn_list, turn_read, "
        "turn_token_usage, turn_count, turn_summarize. "
        "All data is automatically scoped to the current agent."
    ),
    lifespan=_memdb_lifespan,
)

_store: SessionStore | None = None
_manager: SemanticManager | None = None
_db_path: Path | None = None
_init_lock: asyncio.Lock | None = None
_recall_policy_cache: "RecallPolicy | None" = None


def _recall_policy() -> RecallPolicy:
    """Recall's own caps, read once from the agent config.

    The caps are recall's *configuration*, not its caller's arguments: the
    discriminator chooses what to look for, never how much to take.  A config
    that cannot be read degrades to the dataclass defaults rather than failing
    the turn — a wrong-sized context beats no context.
    """
    global _recall_policy_cache
    if _recall_policy_cache is not None:
        return _recall_policy_cache
    policy = RecallPolicy()
    try:
        from slife.config import Config
        from slife.paths import get_config_path

        cfg = Config.from_yaml(get_config_path())
        policy = RecallPolicy(
            min_similarity=cfg.recall_min_similarity,
            limit=cfg.recall_limit,
            # The budget IS the context floor — the same knob the trim
            # compacts to.  Both modes therefore hold the context at the same
            # size, so flipping `rebuild_message` changes how the context is
            # chosen, never how big it is.
            token_budget=int(
                cfg.active_model.context_window * cfg.context_floor
            ),
        )
    except Exception:
        logger.warning("recall_policy_defaulted", exc_info=True)
    _recall_policy_cache = policy
    return policy


def _get_db_path() -> Path:
    """Return the database path for the current agent.

    ``SLIFE_MEMDB_DB`` override, else the per-agent ``<agent>.db`` under the
    data dir — both resolved by the single :func:`slife.paths.get_memdb_db_path`.
    """
    from slife.paths import get_memdb_db_path

    return get_memdb_db_path()


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

    # Handshake-fast: the store is built WITHOUT vectors.  Embedding setup
    # (config read, sqlite-vec load, vec0 table) is heavy and belongs on the
    # post-handshake warm path (SemanticManager.enable() re-runs the schema
    # with the real width via reconfigure_for_embedding), never in the
    # lifespan — the plugin contract says the lifespan must stay
    # handshake-fast, and embeddings are optional: a slow or unavailable
    # backend must not gate startup.  Keyword search serves with no vectors;
    # the vec0 tables appear once the embedding model loads.
    _store = SessionStore(_db_path)
    with elapsed("store_setup", logger, level=logging.INFO, db=str(_db_path)):
        await _store.setup(embedding_dim=0, embedding_model="")

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


warm_after_ready(mcp, _warm_semantic, name="semantic")


# ═══════════════════════════════════════════════════════════════════════
# Harness tools (programmatic only — not exposed to LLM)
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(name="__memory_save_turn", description="Save a turn. Internal — called by the agent loop.")
async def __memory_save_turn(
    user_message: str = "",
    messages: list[dict] | None = None,
    token_count: int = 0,
    context_tokens: int = 0,
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
                context_tokens=context_tokens,
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


@mcp.tool(
    name="__memory_reload_semantic",
    description="Reload the semantic index after an embeddings config change. Internal — called by the harness.",
)
async def __memory_reload_semantic(enabled: bool = True) -> str:
    """Re-read the shared embeddings config and rebuild (or tear down) the
    semantic index.  ``enabled=True`` → ``SemanticManager.enable()`` (stops
    the drainer, migrates vec0 in place, restarts the drainer); ``False`` →
    ``disable()`` (stops the drainer, keeps embeddings on disk).  Called by
    the harness's ``embeddings_*`` builtin tools after a config change."""
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
    name="__memory_context_turns_drop",
    description="Drop turn ids from the persisted live-context list. Internal — called by the agent loop.",
)
async def __memory_context_turns_drop(turn_ids: list[int]) -> str:
    """Remove *turn_ids* from the persisted live-context list after the
    internal trim evicted them.  Restore replays exactly what is left, so
    startup rebuilds the exit-time context instead of re-slicing 20%."""
    try:
        async with _get_init_lock():
            store = await _ensure_store_locked()
            remaining = await store.drop_context_turns(turn_ids)
        return json.dumps({"context_turns": remaining}, ensure_ascii=False)
    except Exception as e:
        logger.exception("context_turns_drop_failed ids=%d", len(turn_ids))
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="__memory_turn_recall",
    description="Select the turns that form the next turn's context. Internal — called by the agent loop.",
)
async def __memory_turn_recall(
    query: str = "",
    since: str | None = None,
    until: str | None = None,
) -> str:
    """Return the recalled turn ids — nothing else.

    Args:
        query: Search text; an empty string means no search (browse by time
            instead).
        since: Lower bound — ISO date/datetime, or a relative phrase (the
            grammar is the LLM-facing ``turn_search``'s).
        until: Upper bound — same grammar as since.

    Called by the agent loop before every turn, where the answer *overrides*
    the context with no reconciliation against what was already there.  The
    ids are all the rebuild reads out of it; it fetches the turns themselves
    with ``__memory_turns_by_ids``.

    A *query* runs the hybrid search; a time range without one browses that
    period; neither is an empty call, which recalls nothing.  Either way the
    three caps come from recall's own configuration (``agent.recall_*``), not
    from the caller — see :mod:`slife.plugins.memdb.recall`.

    Returns ``{"turns": [ids]}``, ascending (chronological).  A degraded
    semantic leg does not change the answer — the selection stands either way
    — so it is logged (``recall_degraded``) rather than returned.

    **There is no error return.**  The caller's only safe reading of "error"
    is "keep the context you have", while an empty selection is a legitimate
    answer that *overrides* the context with nothing — so anything that is
    not a store failure is answered as an empty selection: a time bound the
    grammar rejects, a query the store cannot parse, a pipeline bug.  A
    **store failure is fatal** instead (``sqlite3.Error`` propagates, like
    the startup readiness check): a plausible-looking empty list from a broken
    database is worse than no answer, because it silently wipes the context.
    A **tokenizer failure is fatal** in the same way (``TokenizerUnavailable``
    propagates): it is an environment failure like the store's, and no turn
    can be sized — let alone selected within a budget — without it.
    """
    policy = _recall_policy()
    try:
        async with _get_init_lock():
            store = await _ensure_store_locked()
            manager = _manager

            if not query.strip():
                if not (since or until):
                    # No query and no range: nothing was asked for.  The caller
                    # that means "the context is sufficient" does not call at
                    # all (the agent loop reads an empty parameter object before
                    # it gets here) — so reaching this means an empty call, and
                    # an empty call recalls nothing.
                    logger.info("recall_no_criteria")
                    return json.dumps({"turns": []}, ensure_ascii=False)
                # Time branch.  This branch MUST run before the hybrid legs:
                # an empty query reaches FTS5 as `MATCH ''` (an OperationalError)
                # and embeds to noise, so the legs cannot express "no query".
                hits = await store.search_time(
                    limit=policy.limit, since=since, until=until,
                )
                ranked = [h["rowid"] for h in hits][: policy.limit]
            else:
                result = await run_hybrid(
                    store, manager, query=query, limit=policy.limit,
                    since=since, until=until, overfetch=3,
                )
                ranked = gate_turns(result.hits, policy=policy)
                if not result.semantic_available:
                    # The selection stands either way, but a degraded leg
                    # widens the empty case — which clears the context.
                    logger.info("recall_degraded reason=%.120s",
                                hybrid_hint(result))

            # The token budget needs each turn's stored messages, so it is a
            # second phase over what gating kept.
            turns = await store.get_turns_by_ids(ranked)
            costs = {t["rowid"]: estimate_turn_tokens(t) for t in turns}
            selected = fit_budget(ranked, costs, policy.token_budget)

        return json.dumps({"turns": selected}, ensure_ascii=False)
    except InvalidTimeBound as e:
        # An unrecognized bound is an input outcome, not a defect: the phrase
        # matched no calendar period, so it selects nothing.  Answering
        # "nothing" keeps the turn running.
        logger.info("turn_recall_unusable_bound query=%.60s err=%s", query, e)
        return json.dumps({"turns": []}, ensure_ascii=False)
    except sqlite3.Error as e:
        logger.error("turn_recall_store_fatal query=%.60s err=%s", query, e)
        raise
    except TokenizerUnavailable as e:
        # Fatal, for the same reason a store failure is.  Every row's cost —
        # and therefore the budget the selection is fitted to — is measured by
        # the tokenizer, so without one there is no budget and no selection.
        # Falling through to the empty answer below would be the *worst* of
        # the shapes: the caller reads an empty selection as "clear the
        # context", so a wiped context would look exactly like a thin recall.
        # Raising reaches the caller as the MCP error string that makes
        # recall_turns answer None — keep the context.
        logger.error("turn_recall_tokenizer_fatal query=%.60s err=%s", query, e)
        raise
    except Exception:
        logger.warning("turn_recall_empty query=%.60s", query, exc_info=True)
        return json.dumps({"turns": []}, ensure_ascii=False)


@mcp.tool(
    name="__memory_turns_by_ids",
    description="Fetch turns by id, in the caller's order. Internal — called by the agent loop.",
)
async def __memory_turns_by_ids(turn_ids: list[int]) -> str:
    """Return the full turn rows for *turn_ids*, in the given order.

    The companion to ``__memory_turn_recall``: recall selects *which* turns
    (ids only, by design), this retrieves them.  Kept separate so recall's
    answer stays a plain id list.

    Same contract as its companion: no error return, and a **store failure is
    fatal** (``sqlite3.Error`` propagates).  A non-store failure yields no
    turns — which the caller reads as "the selection could not be built", the
    one case where it keeps the context rather than honouring the selection.
    """
    try:
        async with _get_init_lock():
            store = await _ensure_store_locked()
            turns = await store.get_turns_by_ids(turn_ids)
        return json.dumps({"turns": turns}, ensure_ascii=False)
    except sqlite3.Error as e:
        logger.error("turns_by_ids_store_fatal ids=%d err=%s", len(turn_ids), e)
        raise
    except Exception:
        logger.warning("turns_by_ids_empty ids=%d", len(turn_ids), exc_info=True)
        return json.dumps({"turns": []}, ensure_ascii=False)


@mcp.tool(
    name="__memory_context_turns_set",
    description="Replace the persisted live-context list. Internal — called by the agent loop.",
)
async def __memory_context_turns_set(turn_ids: list[int]) -> str:
    """Replace the live-context list with *turn_ids*.

    The write behind the per-turn rebuild: recall's selection **overrides**
    the previous context, so this replaces rather than merges.  The ids must
    already be the complete, ordered selection — a partial list silently
    shrinks the context, which is why the harness only calls this after a
    known-good recall.
    """
    try:
        async with _get_init_lock():
            store = await _ensure_store_locked()
            written = await store.set_context_turns(turn_ids)
        return json.dumps({"context_turns": written}, ensure_ascii=False)
    except Exception as e:
        logger.exception("context_turns_set_failed ids=%d", len(turn_ids))
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="__memory_context_turns_clear",
    description="Empty the persisted live-context list. Internal — called by the agent loop.",
)
async def __memory_context_turns_clear() -> str:
    """Empty the live-context list — the write behind an empty recall
    selection (no turn qualified for the turn).  Turns saved afterwards
    re-enter it as they save."""
    try:
        async with _get_init_lock():
            store = await _ensure_store_locked()
            await store.clear_context_turns()
        return json.dumps({"context_turns": []}, ensure_ascii=False)
    except Exception as e:
        logger.exception("context_turns_clear_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════
# LLM-visible tools
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="turn_search",
    description=(
        "Search turns (each result carries its turn_id): mode hybrid "
        "(default)/fts5/grep (regex). Use turn_read for a full turn, or "
        "turn_list to browse. since/until window the search — "
        + BOUND_GRAMMAR + "."
    ),
)
async def turn_search(
    query: str, mode: str = "hybrid", limit: int = 20,
    since: str | None = None, until: str | None = None,
) -> str:
    """Search the turn history (each result = one turn).

    Args:
        query: The search text.
        mode: hybrid (default) | fts5 | grep (regex).
        limit: Maximum results.
        since: Lower bound on when the turn was written — ISO date/datetime or
            a relative phrase (today/yesterday/tomorrow/now, last|this
            week|month|quarter|year, "<N> days|weeks|months|years ago");
            omit for no lower bound.
        until: Upper bound — same grammar as since.
    """
    # Search only READS the semantic gate — no side effects, no reindex kick.
    store = await _ensure_store()
    manager = _manager
    mode = mode.lower()
    if mode not in ("hybrid", "fts5", "grep"):
        mode = "hybrid"
    # Clamp before use — the store methods clamp internally, but the hybrid
    # final slice (`hits[:limit]`) uses the raw value, so a limit of 0 (→ [])
    # or a negative (→ slices from the tail) would slip through.
    limit = _clamp_limit(limit)

    if not query.strip():
        # An empty query is not a search, and it must not become one: grep
        # compiles the empty pattern, which matches EVERY string.  Browsing is
        # what turn_list is for.
        return json.dumps(
            {"error": "query must not be empty — to browse instead of "
                      "search, use turn_list"},
            ensure_ascii=False,
        )

    try:
        if mode == "grep":
            try:
                hits = await store.search_grep(
                    pattern=query, limit=limit, since=since, until=until,
                )
            except re.error as e:
                # grep is a regex: an unusable pattern is the caller's to fix,
                # and saying so beats a silent empty result.
                return json.dumps(
                    {"error": f"invalid regex {query!r}: {e}"},
                    ensure_ascii=False,
                )
            rename_rowid_to_turn_id(hits)
            return json.dumps(
                {"mode": "grep", "query": query, "results": hits,
                 "hint": "" if hits else f"no turns contain '{query}'"},
                ensure_ascii=False, indent=2,
            )

        if mode == "fts5":
            hits = await store.search_keyword(
                query=query, limit=limit, since=since, until=until,
            )
            rename_rowid_to_turn_id(hits)
            return json.dumps(
                {"mode": "fts5", "query": query, "results": hits,
                 "hint": "" if hits else f"no turns related to '{query}'"},
                ensure_ascii=False, indent=2,
            )

        # hybrid — the composition lives in ONE place (search.run_hybrid);
        # this tool only formats its result.  The hits are already renamed to
        # turn_id and carry the normalized similarity.
        result = await run_hybrid(
            store, manager, query=query, limit=limit,
            since=since, until=until,
        )
        # Report the mode that actually RAN.  A degraded hybrid is an fts5
        # result, and answering "hybrid" would misreport what was searched.
        return json.dumps({
            "mode": "hybrid" if result.semantic_available else "fts5",
            "query": query,
            "results": result.hits[:limit],
            "hint": hybrid_hint(result),
        }, ensure_ascii=False, indent=2)
    except InvalidTimeBound as e:
        # A bound in no known grammar is the caller's to fix: saying so beats a
        # logged traceback, and beats the silent empty result a pass-through
        # bound used to produce (SQLite compares the text, matches nothing, and
        # reports it as "no matches").
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    except Exception as e:
        logger.exception("search_failed query=%s mode=%s", query, mode)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="turn_list",
    description=(
        "List turns (newest first), optionally within a since/until range; "
        "pass a higher offset to page."
    ),
)
async def turn_list(
    since: str | None = None, until: str | None = None,
    limit: int = 50, offset: int = 0,
) -> str:
    """List turns, newest first — the browse to turn_search's search.

    Args:
        since: Lower bound on when the turn was written — ISO date/datetime or
            a relative phrase (today/yesterday/tomorrow/now, last|this
            week|month|quarter|year, "<N> days|weeks|months|years ago");
            omit for no lower bound.
        until: Upper bound — same grammar as since.
        limit: Maximum entries to return.
        offset: Skip this many entries (for paging).
    """
    store = await _ensure_store()
    try:
        data = await store.turn_list(
            since=since, until=until, limit=limit, offset=offset,
        )
    except InvalidTimeBound as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    except Exception as e:
        logger.exception("turn_list_failed limit=%s offset=%s", limit, offset)
        return json.dumps({"error": str(e)}, ensure_ascii=False)

    entries = data["entries"]
    rename_rowid_to_turn_id(entries)
    for entry in entries:
        message = entry.get("user_message") or ""
        if len(message) > 200:
            # The ellipsis matters: without it a cut message reads as a short
            # one, and the caller has no reason to call turn_read.
            entry["user_message"] = message[:200] + "…"
    return json.dumps(
        {"total": data["total"], "limit": len(entries),
         "offset": max(0, offset), "entries": entries},
        ensure_ascii=False, indent=2,
    )


@mcp.tool(
    name="turn_token_usage",
    description=(
        "Token usage per turn, with totals/averages; filter by turn id or "
        "ISO time range. token_count = billed cumulative tokens for the "
        "turn, context_tokens = the last call's prompt+completion (the "
        "persisted history's context size)."
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
        since: Lower bound — ISO datetime/date or today/yesterday/tomorrow.
        until: Upper bound — ISO datetime/date or today/yesterday/tomorrow.
        limit: Maximum number of turns to return (newest first).
    """
    store = await _ensure_store()
    try:
        result = await store.token_usage(
            rowid=turn_id, since=since, until=until, limit=limit,
        )
        # No user_message in the dump — the tool is usage-only, and message
        # text would burn tokens for nothing (the ids are enough to turn_read).
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("token_usage_failed turn_id=%s", turn_id)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="turn_count",
    description=(
        "Count turns (all, within an ISO time range, or search matches via "
        "grep/fts5)."
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
        since: Lower bound — ISO datetime/date or today/yesterday/tomorrow.
        until: Upper bound — ISO datetime/date or today/yesterday/tomorrow.
        query: Search text to count matches for (grep/fts5 modes).
        mode: grep or fts5 (default fts5).
    """
    store = await _ensure_store()
    try:
        result = await store.count_turns(
            since=since, until=until, query=query, mode=mode,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except InvalidTimeBound as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    except Exception as e:
        logger.exception("count_failed query=%s mode=%s", query, mode)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="turn_read",
    description=(
        "Load a full turn by turn id (messages incl. thinking, tool calls, "
        "tool results)."
    ),
)
async def turn_read(turn_id: int) -> str:
    """Load a full turn by turn id.

    Args:
        turn_id: Turn id (from turn_search / turn_list / a [INFO] footnote).
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
    name="turn_summarize",
    description=(
        "Write a summary and tags for a turn, making it findable via keyword "
        "search (not the semantic index)."
    ),
)
async def turn_summarize(
    turn_id: int | None = None,
    summary: str | None = None, tags: str | None = None,
) -> str:
    """Write a summary and tags for a turn, making it findable by keyword search.

    Args:
        turn_id: Turn id to annotate (historical); omit for the current turn.
        summary: A 1-2 sentence summary of the turn.
        tags: Comma-separated tags.
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
                    "annotation captured for the current turn — written when "
                    "this turn is saved"
                ),
            }, ensure_ascii=False, indent=2)
        await store.update_summary(rowid=turn_id, summary=summary, tags=tags)
        return json.dumps({"status": "updated", "turn_id": turn_id}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("summarize_failed turn_id=%s", turn_id)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════
# Internal __check — raw technical status for the harness's system_health
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="__check",
    description=(
        "Turns DB + semantic-search raw status: DB file facts and the "
        "semantic-index gate. Internal — probed by the harness's "
        "system_health, never exposed to the LLM."
    ),
)
async def __check() -> str:
    """Return raw technical status facts for the turns DB + embedding index.

    Internal (``__`` prefix): probed by the harness's ``system_health``,
    which interprets the facts into health entries.  Facts only — no health
    levels, no remediation hints (a 体检报告, not a diagnosis).
    """
    from slife.plugins.memdb.embedding_config import make_check_report

    # ── Database file facts ──────────────────────────────────────
    db_path = _db_path if _db_path is not None else _get_db_path()
    db: dict = {"exists": db_path.exists()}
    if db["exists"]:
        db["size_mb"] = round(db_path.stat().st_size / (1024 * 1024), 1)
    db["path"] = str(db_path)

    # ── Semantic-search facts ────────────────────────────────────
    semantic: dict = {}
    try:
        await _ensure_store()
        manager = _manager
        semantic = make_check_report()
        semantic.pop("hint", None)  # facts only — remediation lives in the harness
        # The STORE's facts: whether the vector index can exist at all in this
        # process, and why not when it cannot.  Independent of the embedding
        # endpoint — a Python that cannot load SQLite extensions has no index
        # whatever the endpoint says.
        store_obj = _store
        if store_obj is not None:
            semantic["vec_available"] = store_obj._vec_available
            if not store_obj._vec_available:
                semantic["vec_reason"] = store_obj._vec_reason
        if manager is not None:
            semantic["semantic_ready"] = manager.semantic_ready
            semantic["state"] = manager.state
            semantic["reason"] = manager.reason
            semantic["unembedded"] = await manager.unembedded()
            e = manager.embedder
            if e is not None:
                # live embedder facts override the config probe
                semantic["model"] = e._model
                semantic["dimension"] = e.dimension
                semantic["available"] = e.available
                semantic["loaded"] = e.loaded
        else:
            semantic["semantic_ready"] = False
            semantic["state"] = "no_manager"
            semantic["unembedded"] = 0
    except Exception as e:
        logger.warning("memdb_check_failed err=%s", e)
        semantic["error"] = str(e)

    return json.dumps({"db": db, "semantic": semantic}, ensure_ascii=False, indent=2)


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
