"""Semantic lifecycle manager for the memdb plugin.

Owns the semantic-search gate (binary, single source of truth) and the
embedder + index drainer as one actor. ``enable`` / ``disable`` are blocking
config transitions; ``on_turn_saved`` wakes the event-driven drainer.

Why this exists: the previous design scattered ``_semantic_ready`` /
``_embedder`` / ``_reindex_task`` / ``_reinit_task`` across ``server.py``
module globals, poked from 6+ entry points and racing each other — a runtime
``memory_set_embedding`` once left the gate stuck off until restart. Here all
state is owned in-process by one object, so the ``python -m`` double-module
bug and the cross-module ``reload_embedder`` global mutation are structurally
impossible.
"""

import asyncio
import json
import logging
from pathlib import Path

from slife.plugins.memdb.embeddings import EmbeddingClient
from slife.plugins.memdb.embedding_config import read_embedding_config

logger = logging.getLogger(__name__)

#: Max consecutive zero-progress drain batches before giving up (REVIEW M7) —
#: a persistently failing embedder must not spin forever.
MAX_REINDEX_NO_PROGRESS = 20
REINDEX_BATCH_LIMIT = 5

_DRAIN_INDEXING_REASON = (
    "hybrid degraded to fts5 — semantic index is building/rebuilding. "
    "Semantic search resumes automatically when indexing finishes."
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
                    "Download the model and set its path with memory_set_embedding")
        return "gguf backend unavailable — no GGUF model path configured"
    if backend == "api":
        return ("api backend unavailable — API key is an unresolved ${VAR} placeholder "
                "or missing. Configure a real key with memory_set_embedding backend=api")
    if backend == "transformer":
        return ("transformer backend unavailable — sentence-transformers not installed. "
                "Run: uv pip install sentence-transformers")
    return "embedding backend not configured"


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
        self._write_lock = asyncio.Lock()  # serializes diary_semantic writes
        self._no_progress = 0

    # ── public entry points ──────────────────────────────────────────

    async def start(self) -> None:
        """Startup: enable when config present + enabled, else disable."""
        try:
            cfg = read_embedding_config()
            if cfg and cfg.get("enabled", True):
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
            self._semantic_ready = False
            self._state = "loading"
            self._reason = ""

            embedder = EmbeddingClient.from_config(config_path=self._config_path)
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

            model_id = f"{embedder.backend}:{embedder._model}"
            await self._store.reconfigure_for_embedding(
                embedding_dim=embedder.dimension,
                embedding_model=model_id,
            )

            self._enabled = True
            self._no_progress = 0
            self._state = "indexing"
            self._reason = _DRAIN_INDEXING_REASON
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
            self._semantic_ready = False
            self._state = "disabled"
            self._reason = (
                "semantic search disabled — keyword (fts5/grep/time) search still works"
            )
            self._embedder = None
            logger.info("semantic_disabled")
            return self._status(
                status="ok", message="Semantic search disabled. Keyword search still available.",
            )

    def on_turn_saved(self) -> None:
        """A turn was persisted — wake an idle drainer (non-blocking)."""
        if self._enabled:
            self._work_event.set()

    async def reembed_summary(self, rowid: int, summary: str, tags: str) -> None:
        """Re-embed a turn from its summary, serialized with drainer writes."""
        embedder = self._embedder
        if not summary or not embedder or not embedder.available:
            return
        async with self._write_lock:
            emb = await embedder.embed_one(summary)
            if not emb:
                return
            assert self._store._conn is not None
            cursor = await self._store._conn.execute(
                "SELECT tags, created_at FROM diary WHERE rowid = ?", (rowid,),
            )
            row = await cursor.fetchone()
            if row:
                await self._store.replace_embedding_chunks(
                    diary_rowid=rowid, summary=summary,
                    tags=tags or row["tags"] or "",
                    created_at=row["created_at"], embeddings=[emb],
                )

    async def close(self) -> None:
        """Shutdown: cancel the drainer so it never writes a closed connection."""
        await self._stop_drainer()
        self._enabled = False

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
            result = await self._process_batch()
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
        """Embed one batch of unembedded turns.

        Returns ``{total, indexed, remaining, complete}``. Only turns whose
        every chunk produced a vector are committed (atomic replace) and
        counted — a partial/failed embed leaves the turn fully unembedded so
        the next pass retries it (and the M7 no-progress bound still trips).
        """
        from slife.plugins.memdb.store import (
            _chunk_text, _split_chunks_to_token_limit, _turn_text_for_embedding,
        )
        embedder = self._embedder
        if not embedder or not embedder.available:
            return {"total": 0, "indexed": 0, "remaining": 0, "complete": True,
                    "reason": "embedder unavailable"}
        total = await self._store.count_unembedded()
        if total == 0:
            return {"total": 0, "indexed": 0, "remaining": 0, "complete": True}
        turns = await self._store.get_unembedded_turns(limit=batch_limit)
        indexed = 0
        for turn in turns:
            try:
                embed_text = _turn_text_for_embedding(
                    turn["user_message"], json.loads(turn.get("messages", "[]")),
                )
                if not embed_text.strip():
                    continue
                chunks = _chunk_text(embed_text)
                chunks = _split_chunks_to_token_limit(chunks, embedder.max_tokens)
                if not chunks:
                    continue
                embeddings = await embedder.embed(chunks)
                if (
                    embeddings
                    and len(embeddings) == len(chunks)
                    and all(emb for emb in embeddings)
                ):
                    async with self._write_lock:
                        await self._store.replace_embedding_chunks(
                            diary_rowid=turn["rowid"],
                            summary=turn.get("summary", ""),
                            tags=turn.get("tags", ""),
                            created_at=turn["created_at"],
                            embeddings=embeddings,
                        )
                    indexed += 1
            except Exception as e:
                logger.debug("reindex_skip rowid=%s err=%s", turn["rowid"], e)
        remaining = await self._store.count_unembedded()
        return {
            "total": total, "indexed": indexed,
            "remaining": remaining, "complete": remaining == 0,
        }
