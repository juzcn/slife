"""Semantic lifecycle manager — gate + embedder + background drainer.

Owns the semantic-search gate (binary) and the tool-embedding drainer as one
actor.  ``enable`` / ``disable`` are blocking config transitions;
``on_saved`` wakes the event-driven drainer.

Store contract (a "document source"): ``count_unembedded()``,
``get_unembedded_docs(limit)`` (rows with ``doc_id`` + ``text``), and
``replace_embedding(doc_id, vec, model)`` — see ``mcp_plugin.store.ToolStore``.
One tool = one document = one embedding (tool names/descriptions are short,
so no chunking).

Why this exists: keeps the ``semantic_ready`` gate and the embedder in one
place so config changes (embeddings set / remove / model change) cannot race
each other into a stuck-off gate.
"""

import asyncio
import logging

from mcp_plugin.embeddings import EmbeddingClient

logger = logging.getLogger(__name__)

#: Max consecutive zero-progress drain batches before giving up —
#: a persistently failing embedder must not spin forever.
MAX_REINDEX_NO_PROGRESS = 20
REINDEX_BATCH_LIMIT = 10

_DRAIN_INDEXING_REASON = (
    "hybrid degraded to keyword — semantic index is building/rebuilding. "
    "Semantic search resumes automatically when indexing finishes."
)

_META_MODEL_KEY = "embedding_model"


def _backend_unavailable_reason(embedder: EmbeddingClient) -> str:
    """Human reason why the configured embedding backend is unavailable."""
    if not embedder.base_url:
        return (
            "embeddings not configured — add an 'embeddings' section to "
            "mcp-plugin.json5 (e.g. via mcp_embeddings_set) to enable semantic search"
        )
    return (
        "api backend unavailable — base_url is a placeholder or unreachable. "
        "Configure it with mcp_embeddings_set"
    )


class SemanticManager:
    """The semantic-search actor: gate + embedder + event-driven drainer."""

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
        self._no_progress = 0

    # ── public entry points ──────────────────────────────────────────

    async def start(self) -> None:
        """Startup: enable when the embeddings config is present, else disable."""
        try:
            from mcp_plugin.embeddings import EmbeddingClient

            probe = EmbeddingClient.from_plugin_config(config_path=self._config_path)
            if probe.available:
                await self.enable()
            else:
                await self.disable()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("semantic_start_error err=%s", e)

    async def enable(self) -> dict:
        """Blocking: load the model, start the drainer.

        Returns AFTER model load (NOT after the reindex) — the drainer keeps
        draining in the background until ``unembedded == 0``, at which point
        the gate opens on its own.
        """
        async with self._enable_lock:
            await self._stop_drainer()
            self._semantic_ready = False
            self._state = "loading"
            self._reason = ""

            embedder = EmbeddingClient.from_plugin_config(config_path=self._config_path)
            if not embedder.available:
                self._state = "disabled"
                self._reason = _backend_unavailable_reason(embedder)
                return self._status(
                    status="degraded", embedder=embedder,
                    message="Embedding backend unavailable — keyword search still works.",
                )

            self._embedder = embedder
            if not await embedder.load():
                self._state = "stalled"
                self._reason = "embedding model failed to load"
                return self._status(
                    status="degraded", message="Embedding model failed to load.",
                )

            # Model change → old vectors live in a different vector space.
            model_id = f"api:{embedder.model}"
            stored_model = await self._store.get_meta(_META_MODEL_KEY)
            if stored_model is not None and stored_model != model_id:
                logger.info(
                    "embedding_model_changed old=%s new=%s — dropping old vectors",
                    stored_model, model_id,
                )
                await self._store.drop_embeddings()
            await self._store.set_meta(_META_MODEL_KEY, model_id)

            self._enabled = True
            self._no_progress = 0
            self._state = "indexing"
            self._reason = _DRAIN_INDEXING_REASON
            self._work_event.set()
            self._drain_task = asyncio.create_task(self._drain_loop())
            logger.info(
                "semantic_enabled model=%s dim=%d state=indexing",
                embedder.model, embedder.dimension,
            )
            return self._status(
                status="ok",
                message=f"Enabled api backend: {embedder.model} (dim={embedder.dimension})",
            )

    async def disable(self) -> dict:
        """Blocking: stop the drainer, drop the embedder; vectors on disk kept."""
        async with self._enable_lock:
            await self._stop_drainer()
            self._semantic_ready = False
            self._state = "disabled"
            self._reason = (
                "semantic search disabled — keyword (fts5/grep) search still works"
            )
            self._embedder = None
            logger.info("semantic_disabled")
            return self._status(
                status="ok", message="Semantic search disabled. Keyword search still available.",
            )

    def on_saved(self) -> None:
        """A tool catalog change — wake an idle drainer (non-blocking)."""
        if self._enabled:
            self._work_event.set()

    async def close(self) -> None:
        """Shutdown: cancel the drainer so it never writes a closed connection."""
        await self._stop_drainer()
        self._enabled = False
        if self._embedder is not None:
            try:
                await self._embedder.close()
            except Exception:
                pass
            self._embedder = None

    # ── gate / status readers (no side effects) ──────────────────────

    @property
    def semantic_ready(self) -> bool:
        return self._semantic_ready

    @property
    def embedder(self) -> EmbeddingClient | None:
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

    def _status(self, *, status: str = "ok", message: str = "",
                embedder: EmbeddingClient | None = None) -> dict:
        e = embedder if embedder is not None else self._embedder
        return {
            "status": status,
            "backend": e.backend if e else "",
            "model": e.model if e else "",
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

    async def _drain_loop(self) -> None:
        """Event-driven index drainer — the ONLY gate writer (opens at count==0)."""
        while self._enabled:
            try:
                unembedded = await self._store.count_unembedded()
            except Exception as e:
                self._semantic_ready = False
                self._state = "stalled"
                self._reason = f"semantic index unavailable: {e}"
                self._enabled = False
                logger.warning("drainer_aborted err=%s", e)
                return
            if unembedded == 0:
                self._semantic_ready = True
                self._state = "ready"
                self._reason = ""
                await self._work_event.wait()
                self._work_event.clear()
                continue
            self._semantic_ready = False
            self._state = "indexing"
            self._reason = _DRAIN_INDEXING_REASON
            try:
                result = await self._process_batch()
            except Exception as e:
                self._no_progress += 1
                logger.warning(
                    "drainer_batch_error no_progress=%d err=%s",
                    self._no_progress, e,
                )
                if self._no_progress >= MAX_REINDEX_NO_PROGRESS:
                    self._state = "stalled"
                    self._semantic_ready = False
                    self._enabled = False
                    logger.warning(
                        "drainer_stalled — _process_batch failing persistently, giving up",
                    )
                    return
                await asyncio.sleep(0)
                continue
            if result.get("complete"):
                self._no_progress = 0
                continue  # re-check → gate ON next iteration
            if result.get("indexed", 0) == 0:
                self._no_progress += 1
                if self._no_progress >= MAX_REINDEX_NO_PROGRESS:
                    self._state = "stalled"
                    self._semantic_ready = False
                    self._enabled = False
                    logger.warning(
                        "drainer_stalled — embedder failing persistently, giving up. "
                        "remaining=%s", result.get("remaining"),
                    )
                    return
            else:
                self._no_progress = 0
            await asyncio.sleep(0)  # yield between batches

    async def _process_batch(self, batch_limit: int = REINDEX_BATCH_LIMIT) -> dict:
        """Embed one batch of unembedded tools (one tool = one vector).

        Returns ``{total, indexed, remaining, complete}``.  Only tools whose
        embedding succeeded are committed — a failed embed leaves the tool
        unembedded so the next pass retries it (and the no-progress bound
        still trips).
        """
        embedder = self._embedder
        if not embedder or not embedder.available:
            return {"total": 0, "indexed": 0, "remaining": 0, "complete": True,
                    "reason": "embedder unavailable"}
        total = await self._store.count_unembedded()
        if total == 0:
            return {"total": 0, "indexed": 0, "remaining": 0, "complete": True}
        docs = await self._store.get_unembedded_docs(limit=batch_limit)
        model_id = f"api:{embedder.model}"
        indexed = 0
        for doc in docs:
            text = doc.get("text", "")
            if not text.strip():
                continue
            vec = await embedder.embed_one(text)
            if vec:
                await self._store.replace_embedding(doc["doc_id"], vec, model_id)
                indexed += 1
        remaining = await self._store.count_unembedded()
        return {
            "total": total, "indexed": indexed,
            "remaining": remaining, "complete": remaining == 0,
        }
