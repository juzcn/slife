"""Embedding client — OpenAI-compatible HTTP backend only (httpx, no openai).

The plugin stays standalone (no ``slife`` / ``openai`` deps), so embeddings
go straight over ``httpx`` to any OpenAI-compatible ``/v1/embeddings``
endpoint — the deployment uses the ``local-embed`` plugin at
``http://127.0.0.1:17347/v1``.

Config is the single top-level ``embeddings`` section of ``mcp-plugin.json5``:
``{ base_url, model?, api_key? }``.  Present with a real ``base_url`` ⇒ the
client is available (semantic search runs); absent / placeholder ``base_url``
⇒ unavailable (keyword/grep fallback).  ``api_key`` may be empty (no auth
header), plaintext, or a ``${VAR}`` placeholder resolved at construction via
shell env → credstore (an unresolvable placeholder degrades to empty).
"""

import asyncio
import logging
from pathlib import Path

import httpx

from mcp_plugin.config import _resolve_secret, load_config, read_config

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 15.0
_EMBED_TIMEOUT = 60.0


def _looks_like_placeholder(value: str) -> bool:
    """True if *value* is an unresolved ``${VAR}`` placeholder."""
    return value.startswith("${") and value.endswith("}")


class EmbeddingClient:
    """OpenAI-compatible embeddings client (api backend only)."""

    def __init__(
        self,
        model: str = "",
        api_key: str = "",
        base_url: str = "",
        dim: int = 0,
        dim_known: bool | None = None,
        enabled: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._dim = dim
        self._dim_known = dim_known
        self._enabled = enabled
        self._loaded = False
        self._client: httpx.AsyncClient | None = None
        self._client_init_lock = asyncio.Lock()
        self._transport = transport  # test hook (httpx.MockTransport)

    # ── Construction from plugin config ─────────────────────────────

    @classmethod
    def from_plugin_config(
        cls, config_path: str | None = None, quiet: bool = True,
    ) -> "EmbeddingClient":
        """Build a client from the top-level ``embeddings`` section.

        No section, or a ``base_url`` that is a ``${VAR}`` placeholder ⇒
        ``enabled=False`` (semantic search off, keyword/grep fallback).
        An ``api_key`` that is a ``${VAR}`` placeholder is resolved through
        shell env → credstore; unresolvable placeholders degrade to empty
        (no ``Authorization`` header).
        """
        try:
            if config_path is None:
                raw = load_config()
            else:
                raw = read_config(Path(config_path))
        except Exception:
            raw = {}
        emb = raw.get("embeddings")
        if not isinstance(emb, dict):
            return cls(enabled=False)
        base_url = str(emb.get("base_url", ""))
        model = str(emb.get("model", ""))
        api_key = str(emb.get("api_key", ""))
        if _looks_like_placeholder(api_key):
            # ${VAR} → shell env → credstore; unresolvable ⇒ no auth header
            # (a literal "Bearer ${VAR}" is never worth sending).
            resolved = _resolve_secret(api_key)
            api_key = "" if _looks_like_placeholder(resolved) else resolved
        enabled = bool(base_url) and not _looks_like_placeholder(base_url)
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
    def base_url(self) -> str:
        return self._base_url

    @property
    def api_key(self) -> str:
        return self._api_key

    # ── Load / discover ─────────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._client_init_lock:
                if self._client is None:
                    headers = {}
                    if self._api_key:
                        headers["Authorization"] = f"Bearer {self._api_key}"
                    client_kwargs: dict = {
                        "headers": headers,
                        "timeout": httpx.Timeout(_EMBED_TIMEOUT),
                    }
                    if self._transport is not None:
                        client_kwargs["transport"] = self._transport
                    self._client = httpx.AsyncClient(**client_kwargs)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

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
        """GET ``{base_url}/models`` to pin the model + dimension.

        Configured model wins (its dimension picked up if reported); else the
        endpoint's ``active`` model, else the first entry.
        """
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
            return True  # configured id wins even when unlisted
        active = next((m for m in entries if m.get("active")), entries[0])
        self._model = active["id"]
        new_dim = int(active.get("dimension") or 0)
        if new_dim:
            self._dim = new_dim
            self._dim_known = True
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
