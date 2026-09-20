"""Semantic lifecycle manager — shared by the memdb and memfiles plugins.

Owns the semantic-search gate (binary, single source of truth) and the
embedder + index drainer as one actor. ``enable`` / ``disable`` are blocking
config transitions; ``on_saved`` wakes the event-driven drainer.

Store contract (a "document source"): the store must provide
``count_unembedded()``, ``get_unembedded_docs(limit)`` (rows carrying
``doc_id``/``text``/``summary``/``tags``/``created_at``), and
``replace_embedding_chunks(doc, embeddings)``.  memdb's ``SessionStore``
serves turns; memfiles' ``MemfilesStore`` serves notes/diary/file summaries.
Each plugin constructs its own ``SemanticManager`` against its own store, so
the two plugins' gates are independent.

Why this exists: the previous design scattered ``_semantic_ready`` /
``_embedder`` / ``_reindex_task`` / ``_reinit_task`` across ``server.py``
module globals, poked from 6+ entry points and racing each other — a runtime
config change once left the gate stuck off until restart. Here all state is
owned in-process by one object, so the ``python -m`` double-module bug and
the cross-module ``reload_embedder`` global mutation are structurally
impossible.  The config is the top-level ``embeddings`` section (shared by
memdb + memfiles); ``enable`` re-reads it on every call.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

from slife.plugins.memdb.embeddings import EmbeddingClient
from slife.plugins.memdb.embedding_config import read_embedding_config

logger = logging.getLogger(__name__)

#: Max consecutive zero-progress drain batches in ONE drain session before the
#: drainer parks in ``stalled`` — a persistently failing embedder must not spin
#: forever.  Per session, not global: a stall costs nothing while idle, and the
#: wake that ends it grants a fresh budget.
MAX_REINDEX_NO_PROGRESS = 20
REINDEX_BATCH_LIMIT = 5

_DRAIN_INDEXING_REASON = (
    "hybrid degraded to fts5 — semantic index is building/rebuilding. "
    "Semantic search resumes automatically when indexing finishes."
)

_DRAIN_STALLED_REASON = (
    "semantic index stalled — the embedder failed repeatedly and gave up this "
    "round. It retries when new content arrives. Keyword search (fts5/grep/time) "
    "still works."
)


def _backend_unavailable_reason(embedder: EmbeddingClient) -> str:
    """Human reason why the configured backend is unavailable."""
    backend = embedder._backend
    if backend == "gguf":
        if embedder._gguf_path:
            if Path(embedder._gguf_path).exists():
                return ("gguf backend unavailable — llama-cpp-python not installed. "
                        "Run: uv pip install llama-cpp-python")
            return ("gguf backend unavailable — GGUF file not found. "
                    "Serve it via local-embed and point embeddings_model_set at it")
        return "gguf backend unavailable — no GGUF model path configured"
    if backend == "api":
        return ("api backend unavailable — base_url/api_key is missing or an "
                "unresolved ${VAR} placeholder. Configure it with embeddings_model_set")
    if backend == "transformer":
        return ("transformer backend unavailable — sentence-transformers not installed. "
                "Run: uv pip install sentence-transformers")
    return "embedding backend not configured"


class SemanticManager:
    """The semantic-search actor: gate + embedder + event-driven drainer.

    Shared by the memdb, memfiles and mcp_gateway plugins.  The diverging
    bits — how the embedder is built (memdb/memfiles read the ``embeddings``
    config; the gateway takes the connecting host's endpoint), how the store
    records a model change, and how one document is embedded — are hook
    methods (:meth:`_new_embedder`, :meth:`_on_model_selected`,
    :meth:`_embed_doc`, :meth:`_start_enabled`) overridden by subclasses.
    The gate, drain loop, no-progress bound and status readers are shared.
    """

    def __init__(self, store, config_path: str | None = None):
        self._store = store
        self._config_path = config_path
        self._embedder: EmbeddingClient | None = None
        self._semantic_ready = False
        self._state = "disabled"   # disabled | loading | indexing | ready | stalled
        self._reason = ""
        self._enabled = False
        self._drain_task: asyncio.Task | None = None
        self._work_event = asyncio.Event()
        self._enable_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()  # serializes index writes
        self._no_progress = 0

    # ── plugin hook points (overridden by subclasses) ────────────────
    # ``Any`` return/param on these: each plugin has its OWN structurally
    # identical EmbeddingClient class (memdb vs gateway), and a subclass
    # passing its own must not trip type checkers.

    def _new_embedder(self) -> Any:
        """Build the embedder for this plugin's config shape."""
        return EmbeddingClient.from_config(config_path=self._config_path)

    def _start_enabled(self) -> bool:
        """Whether :meth:`start` should enable (True) or disable (False)."""
        cfg = read_embedding_config()
        return bool(cfg and cfg.get("enabled", True))

    async def _on_model_selected(self, embedder: Any) -> None:
        """Apply a model identity to the store (memdb migrates vec0 in place;
        the gateway drops stale vectors via its meta/drop contract)."""
        model_id = f"{embedder.backend}:{embedder._model}"
        await self._store.reconfigure_for_embedding(
            embedding_dim=embedder.dimension,
            embedding_model=model_id,
        )

    async def _embed_doc(self, embedder: Any, doc: dict) -> bool:
        """Embed one unembedded document; return True when committed.

        Every document goes through the ONE shared chunker — memdb turns,
        memfiles cabinet docs, and the host catalog's tool schemas alike.
        A short document (a small tool schema) simply yields a single chunk;
        a long one is split on paragraph boundaries and then hard-split to
        the model's token limit, so an oversized schema can never ride as one
        chunk and stall the drainer on a provider rejection.
        """
        from slife.plugins.memdb.store import (
            _chunk_text, _split_chunks_to_token_limit,
        )
        embed_text = doc["text"]
        if not embed_text.strip():
            logger.debug("reindex_skip_empty_text doc_id=%s", doc.get("doc_id"))
            return False
        chunks = _chunk_text(embed_text)
        chunks = _split_chunks_to_token_limit(chunks, embedder.max_tokens)
        if not chunks:
            logger.debug(
                "reindex_skip_unchunkable doc_id=%s len=%d max_tokens=%s",
                doc.get("doc_id"), len(embed_text), embedder.max_tokens,
            )
            return False
        # Embed one chunk per request.  A long document batched into a
        # single request can exceed the client's embed timeout on a slow
        # (CPU) backend, so the whole doc times out and stays unembedded.
        # Per-chunk requests are small and fast; more requests are fine.
        embeddings: list[list[float]] = []
        for chunk in chunks:
            vec = await embedder.embed([chunk])
            if not vec or not vec[0]:
                # The embedder answered nothing (empty payload or a failed
                # call) — name the doc, or a stall is undiagnosable.
                logger.debug("reindex_skip_no_vector doc_id=%s", doc.get("doc_id"))
                return False
            embeddings.append(vec[0])
        if len(embeddings) != len(chunks):
            return False
        async with self._write_lock:
            await self._store.replace_embedding_chunks(doc, embeddings)
        return True

    def _unavailable_reason(self, embedder: Any) -> str:
        return _backend_unavailable_reason(embedder)

    # ── public entry points ──────────────────────────────────────────

    async def start(self) -> None:
        """Startup: enable when config present + enabled, else disable."""
        try:
            if self._start_enabled():
                await self.enable()
            else:
                await self.disable()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("semantic_start_error err=%s", e)

    async def enable(self) -> dict:
        """Blocking: load the model, migrate vec0 in place, start the drainer.

        Returns AFTER model load + migration, NOT after the reindex — the
        drainer keeps draining in the background until ``unembedded == 0``,
        at which point the gate opens on its own.
        """
        async with self._enable_lock:
            await self._stop_drainer()
            # ``_enabled`` is True exactly while a drain task is live: cleared
            # here, set only on the success path below.  The two degraded
            # returns leave it False, so a failed enable cannot leave a stale
            # True behind for on_saved() to wake nothing with.
            self._enabled = False
            self._semantic_ready = False
            await self._set_state("loading")

            embedder = self._new_embedder()
            if not embedder.available:
                await self._set_state("disabled", self._unavailable_reason(embedder))
                return self._status(
                    status="degraded", embedder=embedder,
                    message="Embedding backend unavailable — keyword search still works.",
                )

            self._embedder = embedder
            if not await embedder.load():
                await self._set_state("stalled", "embedding model failed to load")
                return self._status(
                    status="degraded", message="Embedding model failed to load.",
                )

            await self._on_model_selected(embedder)

            self._enabled = True
            self._no_progress = 0
            await self._set_state("indexing", _DRAIN_INDEXING_REASON)
            self._work_event.set()
            self._drain_task = asyncio.create_task(self._drain_loop())
            logger.info(
                "semantic_enabled backend=%s model=%s dim=%d state=indexing",
                embedder.backend, embedder._model, embedder.dimension,
            )
            return self._status(
                status="ok",
                message=f"Enabled {embedder.backend} backend: {embedder._model} (dim={embedder.dimension})",
            )

    async def disable(self) -> dict:
        """Blocking: stop the drainer, drop the embedder; embeddings on disk kept."""
        async with self._enable_lock:
            await self._stop_drainer()
            # No drainer to wake — keep ``on_saved``'s guard honest rather than
            # setting an event nothing awaits.
            self._enabled = False
            self._semantic_ready = False
            await self._set_state(
                "disabled",
                "semantic search disabled — keyword (fts5/grep/time) search still works",
            )
            self._embedder = None
            logger.info("semantic_disabled")
            return self._status(
                status="ok", message="Semantic search disabled. Keyword search still available.",
            )

    def on_saved(self) -> None:
        """A document was persisted — wake an idle drainer (non-blocking)."""
        if self._enabled:
            self._work_event.set()

    async def close(self) -> None:
        """Shutdown: cancel the drainer so it never writes a closed connection."""
        await self._stop_drainer()
        self._enabled = False

    # ── gate / status readers (no side effects) ──────────────────────

    @property
    def semantic_ready(self) -> bool:
        return self._semantic_ready

    @property
    def embedder(self) -> Any:
        """The active embedder.  ``Any`` like the hooks above — memdb and the
        gateway each carry their own structurally identical EmbeddingClient,
        so status readers must not trip on either."""
        return self._embedder

    @property
    def state(self) -> str:
        return self._state

    @property
    def reason(self) -> str:
        return self._reason

    async def unembedded(self) -> int:
        try:
            return await self._store.count_unembedded()
        except Exception:
            return 0

    # ── internal ─────────────────────────────────────────────────────

    def semantic_facts(self) -> dict:
        """The index's state as facts — what this manager's process knows.

        The ONE builder of the block, for both readers of it: a process that
        runs the drainer reports these live, and the catalog's manager
        publishes exactly this dict so a process that runs no drainer reads the
        same thing (``slife/mcp/host_server.py`` builds the health block from
        either source).

        The pending count is deliberately absent — it is a store fact,
        answerable by any process holding the db, and duplicating it here would
        make a published copy go stale against the row it counts.
        """
        e = self._embedder
        available = bool(e is not None and e.available)
        return {
            "configured": available,
            "available": available,
            "semantic_ready": self._semantic_ready,
            "state": self._state,
            "reason": self._reason,
            "model": e._model if e is not None else "",
            "dimension": e.dimension if e is not None else 0,
        }

    def _status(self, *, status: str = "ok", message: str = "",
                embedder: EmbeddingClient | None = None) -> dict:
        e = embedder if embedder is not None else self._embedder
        return {
            "status": status,
            "backend": e.backend if e else "",
            "model": e._model if e else "",
            "dimension": e.dimension if e else 0,
            "available": bool(e and e.available),
            "loaded": bool(e and e.loaded),
            "semantic_ready": self._semantic_ready,
            "state": self._state,
            "reason": self._reason,
            "message": message,
        }

    async def _stop_drainer(self) -> None:
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug("drainer_stop_error err=%s", e)
        self._drain_task = None

    async def _set_state(self, state: str, reason: str = "") -> None:
        """Move the state machine — the ONE writer of ``_state``/``_reason``.

        A transition and its announcement are the same event, so they are the
        same call: whatever a subclass publishes (the catalog's manager writes
        the shared index's state into ``tools.db``) is written by the call that
        moved the state, never by a second site that could drift from it.

        The publication is best-effort and awaited rather than fired off: two
        transitions in a row must land in the order they happened, or a reader
        could be left holding the older one forever.  A failing store can never
        reach the state machine — the state is already moved.
        """
        self._state = state
        self._reason = reason
        try:
            await self._publish_state()
        except Exception as e:
            logger.debug("semantic_publish_failed state=%s err=%s", state, e)

    async def _publish_state(self) -> None:
        """Announce a transition — a hook for subclasses with somewhere to put it.

        The base class publishes nothing: the state lives with the drainer, and
        only a SHARED index needs it written where other processes can read it.
        """

    async def _enter_stall(self, reason: str) -> None:
        """Close the gate and park in ``stalled`` — ALIVE, not finished.

        ``_enabled`` deliberately stays True.  It is both what lets
        :meth:`on_saved` deliver the wake that ends the stall, and what keeps
        the loop's guard true so the coroutine is still there to receive it.
        Clearing it (as this once did) killed the task and made every
        content-driven wake a silent no-op — a transient embedder failure then
        disabled semantic search until the next process restart.
        """
        self._semantic_ready = False
        await self._set_state("stalled", reason)

    async def _park_until_work(self) -> bool:
        """Wait for a wake; True when there is new work worth attempting.

        Grants a fresh no-progress budget: the bound applies to one drain
        session, so a parked drainer never burns it while idle and the wake
        after a stall is a new attempt rather than a continuation of the failed
        one.  That is the whole safety story — idle costs nothing, and each
        arrival buys one bounded round.
        """
        await self._work_event.wait()
        self._work_event.clear()
        if not self._enabled:
            return False
        self._no_progress = 0
        return True

    async def _pace_retry(self, backoff: float) -> float:
        """Sleep *backoff* after a failed batch; return the next (grown) value.

        Only ``asyncio.sleep(0)`` separated failing batches before, so the
        drainer retried a broken embedder as fast as the event loop allowed.
        """
        await asyncio.sleep(backoff)
        r = _timeouts.timeouts.ready
        return min(backoff * r.watchdog_backoff_multiplier, r.watchdog_backoff_max)

    async def _drain_loop(self) -> None:
        """Event-driven index drainer — the ONLY gate writer (opens at count==0)."""
        backoff = _timeouts.timeouts.ready.watchdog_backoff_initial
        while self._enabled:
            try:
                unembedded = await self._store.count_unembedded()
            except Exception as e:
                logger.warning("drainer_aborted err=%s", e)
                await self._enter_stall(f"semantic index unavailable: {e}")
                if not await self._park_until_work():
                    return
                backoff = _timeouts.timeouts.ready.watchdog_backoff_initial
                continue
            if unembedded == 0:
                self._semantic_ready = True
                await self._set_state("ready")
                if not await self._park_until_work():
                    return
                backoff = _timeouts.timeouts.ready.watchdog_backoff_initial
                continue
            self._semantic_ready = False
            await self._set_state("indexing", _DRAIN_INDEXING_REASON)
            try:
                result = await self._process_batch()
            except Exception as e:
                # An unexpected error (e.g. a row whose messages JSON is
                # malformed) must not kill the drainer task silently — count it
                # as no-progress so the bound parks the loop loudly instead of
                # leaving the semantic gate off with no trace.
                self._no_progress += 1
                logger.warning(
                    "drainer_batch_error no_progress=%d err=%s",
                    self._no_progress, e,
                )
                if self._no_progress >= MAX_REINDEX_NO_PROGRESS:
                    logger.warning(
                        "drainer_stalled — _process_batch failing persistently "
                        "(%d attempts); parked until new content arrives",
                        self._no_progress,
                    )
                    await self._enter_stall(_DRAIN_STALLED_REASON)
                    if not await self._park_until_work():
                        return
                    backoff = _timeouts.timeouts.ready.watchdog_backoff_initial
                    continue
                backoff = await self._pace_retry(backoff)
                continue
            if result.get("complete"):
                self._no_progress = 0
                backoff = _timeouts.timeouts.ready.watchdog_backoff_initial
                continue  # re-check → gate ON next iteration
            if result.get("indexed", 0) == 0:
                self._no_progress += 1
                if self._no_progress >= MAX_REINDEX_NO_PROGRESS:
                    logger.warning(
                        "drainer_stalled — embedder failing persistently "
                        "(%d attempts); parked until new content arrives. "
                        "remaining=%s stuck=%s", self._no_progress,
                        result.get("remaining"),
                        await self._stuck_doc_ids(),
                    )
                    await self._enter_stall(_DRAIN_STALLED_REASON)
                    if not await self._park_until_work():
                        return
                    backoff = _timeouts.timeouts.ready.watchdog_backoff_initial
                    continue
                backoff = await self._pace_retry(backoff)
                continue
            self._no_progress = 0
            backoff = _timeouts.timeouts.ready.watchdog_backoff_initial
            await asyncio.sleep(0)  # yield between batches

    async def _stuck_doc_ids(self, limit: int = 5) -> list:
        """Name the docs the drainer could not embed (diagnostic only).

        A stall that reports only a count sends the reader hunting; the ids
        point straight at the offending row.
        """
        try:
            docs = await self._store.get_unembedded_docs(limit=limit)
            return [d.get("doc_id") for d in docs]
        except Exception:
            return []

    async def _process_batch(self, batch_limit: int = REINDEX_BATCH_LIMIT) -> dict:
        """Embed one batch of unembedded documents.

        Returns ``{total, indexed, remaining, complete}``. Only documents
        whose embedding :meth:`_embed_doc` committed are counted — a
        partial/failed embed leaves the document fully unembedded so the
        next pass retries it (and the M7/no-progress bound still trips).
        """
        embedder = self._embedder
        if not embedder or not embedder.available:
            # NOT ``complete``: the loop reads complete as progress, so claiming
            # it here reset the no-progress bound and spun the drainer at
            # sleep(0) forever — neither stalling nor ever opening the gate.
            # That was the one state MAX_REINDEX_NO_PROGRESS could not catch.
            return {"total": 0, "indexed": 0, "remaining": 0, "complete": False,
                    "reason": "embedder unavailable"}
        total = await self._store.count_unembedded()
        if total == 0:
            return {"total": 0, "indexed": 0, "remaining": 0, "complete": True}
        docs = await self._store.get_unembedded_docs(limit=batch_limit)
        indexed = 0
        for doc in docs:
            try:
                if await self._embed_doc(embedder, doc):
                    indexed += 1
            except Exception as e:
                logger.debug("reindex_skip doc_id=%s err=%s", doc.get("doc_id"), e)
        remaining = await self._store.count_unembedded()
        return {
            "total": total, "indexed": indexed,
            "remaining": remaining, "complete": remaining == 0,
        }
