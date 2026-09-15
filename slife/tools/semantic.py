"""Host-side semantic search for the unified tool catalog (tools.db).

The memdb ``SemanticManager`` (gate + embedder + event-driven drainer) is
the one implementation used by every semantic index in slife; this module
adapts it to the host catalog's store (``CatalogStore``'s drainer
contract + ``meta`` table) and builds its embedder from the HOST's active
embedding endpoint (``get_active_endpoint`` — the top-level ``embeddings``
section of slife.json5), the way memdb/memfiles do in their own processes.

Only the main process starts this manager: it is the single embedding
maintainer (the retired mcp-gateway wrapper no longer drains its own
store).  ``EmbeddingClient`` is the api-only OpenAI-compatible client that
formerly lived in the mcp plugin — moved here verbatim (httpx2).
"""

from __future__ import annotations

import asyncio
import logging

import httpx2

from slife.config import _resolve_secret
from slife.env import is_env_ref
from slife.plugins.memdb.embeddings import _guess_max_tokens  # shared token-limit guess
from slife.plugins.memdb.semantic import SemanticManager as _BaseSemanticManager
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

logger = logging.getLogger(__name__)


class EmbeddingClient:
    """OpenAI-compatible embeddings client (api backend only).

    Config comes from the host's active endpoint (``get_active_endpoint()``
    — the top-level ``embeddings`` section of slife.json5).  A usable
    ``base_url`` (non-empty, not a placeholder) ⇒ available; absent /
    placeholder ⇒ disabled (keyword/grep fallback).  An ``api_key`` that is a
    ``${VAR}`` placeholder is resolved through shell env → credstore;
    unresolvable placeholders degrade to empty (no ``Authorization`` header).
    """

    def __init__(
        self,
        model: str = "",
        api_key: str = "",
        base_url: str = "",
        dim: int = 0,
        dim_known: bool | None = None,
        enabled: bool = True,
        max_tokens: int = 0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ):
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._dim = dim
        self._dim_known = dim_known
        self._enabled = enabled
        self._loaded = False
        self._max_tokens = max_tokens or _guess_max_tokens(model)
        self._client: httpx2.AsyncClient | None = None
        self._client_init_lock = asyncio.Lock()
        self._transport = transport  # test hook (httpx2.MockTransport)

    # ── Construction from the host's active endpoint ───────────────

    @classmethod
    def from_endpoint(
        cls, ep: dict | None, quiet: bool = True,
    ) -> "EmbeddingClient":
        """Build from ``get_active_endpoint()``'s dict (``base_url``/``model``/
        ``api_key``).  Missing or placeholder ``base_url`` ⇒ disabled."""
        emb = None
        if isinstance(ep, dict):
            base_url = str(ep.get("base_url", ""))
            if base_url and not is_env_ref(base_url):
                emb = {
                    "base_url": base_url,
                    "model": str(ep.get("model", "")),
                    "api_key": str(ep.get("api_key", "")),
                }
        if not isinstance(emb, dict):
            return cls(enabled=False)
        base_url = str(emb.get("base_url", ""))
        model = str(emb.get("model", ""))
        api_key = str(emb.get("api_key", ""))
        if is_env_ref(api_key):
            # ${VAR} → shell env → credstore; unresolvable ⇒ no auth header
            # (a literal "Bearer ${VAR}" is never worth sending).
            resolved = _resolve_secret(api_key, accept_keyring_uri=True)
            api_key = "" if is_env_ref(resolved) else resolved
        enabled = bool(base_url) and not is_env_ref(base_url)
        return cls(
            model=model, api_key=api_key, base_url=base_url,
            dim_known=bool(model), enabled=enabled,
        )

    # ── Status ──────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return bool(self._enabled and self._base_url)

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def backend(self) -> str:
        return "api"

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def dimension_known(self) -> bool:
        return bool(self._dim_known)

    @property
    def max_tokens(self) -> int:
        """The model's context limit — the drainer's chunk ceiling."""
        return self._max_tokens

    @max_tokens.setter
    def max_tokens(self, value: int) -> None:
        if isinstance(value, int) and value > 0:
            self._max_tokens = value

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def api_key(self) -> str:
        return self._api_key

    # ── Load / discover ─────────────────────────────────────────────

    async def _get_client(self) -> httpx2.AsyncClient:
        if self._client is None:
            async with self._client_init_lock:
                if self._client is None:
                    headers = {}
                    if self._api_key:
                        headers["Authorization"] = f"Bearer {self._api_key}"
                    client_kwargs: dict = {
                        "headers": headers,
                        "timeout": httpx2.Timeout(_timeouts.timeouts.transport.embed),
                    }
                    if self._transport is not None:
                        client_kwargs["transport"] = self._transport
                    self._client = httpx2.AsyncClient(**client_kwargs)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def probe_available(self, timeout: float | None = None) -> bool:
        """Cheap availability probe — ``GET {base_url}/models``, short timeout.

        ``timeout`` defaults to the registry's ready.probe_endpoint (5s).
        Unlike :meth:`load`, this never runs an embed or waits long: it only
        checks the endpoint answers at all, so callers can auto-degrade fast
        when embeddings is unconfigured or misconfigured.
        """
        if timeout is None:
            timeout = _timeouts.timeouts.ready.probe_endpoint  # call-time lookup
        if not self.available:
            return False
        try:
            client = await self._get_client()
            resp = await asyncio.wait_for(
                client.get(f"{self._base_url}/models"),
                timeout=timeout,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning(
                "embedding_probe_failed base_url=%s err=%s",
                self._base_url, e,
            )
            return False

    async def load(self) -> bool:
        """Pin the model + real dimension from the endpoint. Returns True when ready."""
        if not self.available:
            return False
        try:
            if not await self._discover_model():
                if not self._dim_known:
                    await self._probe_api_dim()
            self._loaded = True
            logger.info(
                "embedding_loaded backend=api model=%s dim=%d base_url=%s",
                self._model, self._dim, self._base_url,
            )
            return True
        except Exception as e:
            logger.warning("embedding_load_failed err=%s", e)
            return False

    async def _discover_model(self) -> bool:
        """GET ``{base_url}/models`` to pin the model + dimension."""
        if not self._base_url:
            return False
        try:
            client = await self._get_client()
            resp = await client.get(f"{self._base_url}/models")
            resp.raise_for_status()
            entries = [
                m for m in (resp.json().get("data") or [])
                if isinstance(m, dict) and m.get("id")
            ]
        except Exception as e:
            logger.warning(
                "embedding_model_discover_failed base_url=%s err=%s",
                self._base_url, e,
            )
            return False
        if not entries:
            return False

        configured = self._model
        if configured:
            match = next((m for m in entries if m.get("id") == configured), None)
            if match is not None:
                new_dim = int(match.get("dimension") or 0)
                if new_dim:
                    self._dim = new_dim
                    self._dim_known = True
                self.max_tokens = int(match.get("max_tokens") or 0)
            return True  # configured id wins even when unlisted
        entry = entries[0]  # models are peers — no active marker on /v1/models
        self._model = entry["id"]
        new_dim = int(entry.get("dimension") or 0)
        if new_dim:
            self._dim = new_dim
            self._dim_known = True
        self.max_tokens = int(entry.get("max_tokens") or 0)
        return True

    async def _probe_api_dim(self) -> None:
        """Pin the real embedding width with one cheap single-token embed."""
        try:
            response = await self._call_api(["."])
        except Exception as e:
            logger.warning("embedding_dim_probe_failed err=%s", e)
            return
        if not response or not response[0]:
            return
        actual = len(response[0])
        if actual and actual != self._dim:
            logger.info(
                "api_dim_override model=%s guessed=%d actual=%d",
                self._model, self._dim, actual,
            )
            self._dim = actual
        self._dim_known = True

    # ── Embed ───────────────────────────────────────────────────────

    async def _call_api(self, texts: list[str]) -> list[list[float]] | None:
        """POST ``{base_url}/embeddings``. Returns embeddings or None on failure."""
        if not self.available:
            return None
        try:
            client = await self._get_client()
            resp = await client.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model, "input": texts},
            )
            resp.raise_for_status()
            payload = resp.json()
            return [
                d["embedding"] for d in (payload.get("data") or [])
                if isinstance(d, dict) and isinstance(d.get("embedding"), list)
            ]
        except Exception as e:
            logger.warning("embedding_call_failed model=%s err=%s", self._model, e)
            return None

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed a batch of texts. Returns None on failure."""
        if not texts:
            return []
        return await self._call_api(texts)

    async def embed_one(self, text: str) -> list[float] | None:
        """Embed a single text. Returns None on failure."""
        result = await self.embed([text])
        if not result:
            return None
        return result[0]


class SemanticManager(_BaseSemanticManager):
    """The semantic-search actor for the unified tool catalog (host-side).

    Hooks differ from memdb/memfiles only in the store's model identity
    contract (the catalog drops stale vectors via its ``meta``/``drop``
    contract instead of an in-place vec0 migration) and the embedder source
    (the host's active endpoint).  Chunking of long schemas is inherited.
    """

    def __init__(self, store, config_path: str | None = None):
        super().__init__(store, config_path=config_path)

    # ── hook overrides ──────────────────────────────────────────────

    def _new_embedder(self):
        from slife.plugins.memdb.embedding_config import get_active_endpoint
        ep = get_active_endpoint()
        return EmbeddingClient.from_endpoint(ep)

    def _start_enabled(self) -> bool:
        emb = self._new_embedder()
        return bool(emb is not None and emb.available)

    async def _on_model_selected(self, embedder) -> None:
        """Record the model identity and drop vectors that live in a
        different vector space (the catalog's meta/drop contract)."""
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
                "no embedding endpoint configured — add an 'embeddings' "
                "section to slife.json5 to enable semantic tool search"
            )
        return (
            "api backend unavailable — base_url is a placeholder or unreachable. "
            "Check the 'embeddings' section in slife.json5."
        )


__all__ = ["EmbeddingClient", "SemanticManager"]