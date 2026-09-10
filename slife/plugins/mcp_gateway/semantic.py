"""MCP gateway semantic manager: a subclass of the shared memdb actor.

The memdb ``SemanticManager`` (gate + embedder + event-driven drainer) is
the one implementation used by memdb and memfiles; this plugin previously
carried its own ~270-line drift-prone copy.  The only genuine differences
are the four hook points the base class exposes:

  - ``_new_embedder``: memdb reads its ``embeddings`` config; the gateway
    takes the connecting host's endpoint (``client_embeddings``), which
    wins when present.
  - ``_start_enabled``: memdb gates on the ``enabled`` flag; the gateway
    gates on a usable (non-placeholder) host-provided base_url.
  - ``_on_model_selected``: memdb migrates vec0 in place; the gateway
    drops stale vectors via its meta/drop contract (different store).

  ``_embed_doc`` is NOT overridden: long tool schemas are chunked at the
  embedding model's token limit exactly like memdb/memfiles documents —
  a schema beyond the limit must never reach the endpoint whole (an
  OpenAI-compatible service 400s ``context_length_exceeded``, the chunk
  would stay unembedded forever, and the drainer would stall).

Everything else — the drain loop, no-progress bound, status readers,
``enable``/``disable``/``close`` transitions — is inherited unchanged.
"""

import logging

from slife.plugins.memdb.semantic import SemanticManager as _BaseSemanticManager

logger = logging.getLogger(__name__)


class SemanticManager(_BaseSemanticManager):
    """The semantic-search actor for the MCP gateway tool catalog."""

    def __init__(self, store, config_path: str | None = None,
                 client_embeddings: dict | None = None):
        super().__init__(store, config_path=config_path)
        # Host-provided embedding endpoint from the connection's initialize
        # handshake (``clientInfo``) — the gateway's sole embedding source.
        self._client_embeddings = client_embeddings

    # ── hook overrides ──────────────────────────────────────────────

    def _new_embedder(self):
        from slife.plugins.mcp_gateway.embeddings import EmbeddingClient
        return EmbeddingClient.from_plugin_config(
            config_path=self._config_path,
            override=self._client_embeddings,
        )

    def _start_enabled(self) -> bool:
        emb = self._new_embedder()
        return bool(emb is not None and emb.available)

    async def _on_model_selected(self, embedder) -> None:
        """Record the model identity and drop vectors that live in a
        different vector space (the gateway store's meta/drop contract)."""
        model_id = f"api:{embedder.model}"
        stored_model = await self._store.get_meta("embedding_model")
        if stored_model is not None and stored_model != model_id:
            logger.info(
                "embedding_model_changed old=%s new=%s — dropping old vectors",
                stored_model, model_id,
            )
            await self._store.drop_embeddings()
        await self._store.set_meta("embedding_model", model_id)

    def _unavailable_reason(self, embedder) -> str:
        if not embedder.base_url:
            return (
                "host passed no embedding endpoint — configure the top-level "
                "'embeddings' section of slife.json5 to enable semantic search"
            )
        return (
            "api backend unavailable — base_url is a placeholder or unreachable. "
            "Check the 'embeddings' section in slife.json5."
        )


__all__ = ["SemanticManager"]