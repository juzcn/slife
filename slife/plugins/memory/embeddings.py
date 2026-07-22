"""Embedding client — generates vectors for semantic search.

Supports two backends:
  1. Local GGUF model (llama-cpp-python) — offline, no API cost
  2. OpenAI-compatible API — remote, requires API key

Configured via slife.json5 → memory.embedding.

Falls back gracefully when embeddings are unavailable — keyword
search (FTS5) still works fine without vectors.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Known embedding dimensions and token limits by model family
_KNOWN_MODELS: dict[str, tuple[int, int]] = {
    # (dimension, max_tokens)
    "text-embedding-3-small": (1536, 8191),
    "text-embedding-3-large": (3072, 8191),
    "text-embedding-ada-002":  (1536, 8191),
    "bge-m3":                  (1024, 8192),
    "bge-large":               (1024, 512),
    "nomic-embed-text":        (768,  8192),
}


def _guess_dim(model: str, gguf_path: str | None = None) -> int:
    """Guess the embedding dimension from the model name."""
    for key, (dim, _) in _KNOWN_MODELS.items():
        if key in model.lower():
            return dim
    return 1024


def _guess_max_tokens(model: str) -> int:
    """Guess the token limit from the model name."""
    for key, (_, limit) in _KNOWN_MODELS.items():
        if key in model.lower():
            return limit
    return 8192


#: Maps each backend to the import that proves it's usable at runtime.
_BACKEND_RUNTIME_IMPORTS: dict[str, tuple[str, str]] = {
    "gguf": ("llama_cpp", "llama-cpp-python"),
    "api":  ("openai",   "openai"),
}


def _check_runtime(backend: str) -> bool:
    """Smoke-test that the Python packages a backend needs are importable.

    ``available`` must reflect *runtime* usability — not just whether a
    config file or GGUF file exists on disk.  Without this, a missing
    dependency is silently treated as "backend ready" until the first
    ``embed()`` call fails.
    """
    pair = _BACKEND_RUNTIME_IMPORTS.get(backend)
    if pair is None:
        return False
    pkg, _ = pair
    try:
        __import__(pkg)
        return True
    except ImportError:
        return False


class EmbeddingClient:
    """Generates embeddings using a local GGUF model or OpenAI API.

    Usage::

        # From config (auto-detects backend)
        client = EmbeddingClient.from_config()

        # Or explicit GGUF
        client = EmbeddingClient(gguf_path="/path/to/model.gguf")

        # Or explicit API
        client = EmbeddingClient(api_key="sk-...", model="text-embedding-3-small")

        vectors = await client.embed(["summary text"])
    """

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        api_key: str = "",
        base_url: str = "",
        gguf_path: str | None = None,
        dim: int = 0,
        quiet: bool = False,
    ):
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._gguf_path = gguf_path
        self._dim = dim or _guess_dim(model, gguf_path)
        self._client = None        # AsyncOpenAI or Llama
        self._backend: str = ""    # "gguf" | "api" | ""
        self._available = False

        _log_warn = logger.debug if quiet else logger.warning

        # Resolve backend
        if gguf_path and Path(gguf_path).exists():
            self._backend = "gguf"
            self._available = _check_runtime("gguf")
            if self._available:
                logger.info(
                    "embeddings_backend=gguf model=%s path=%s dim=%d",
                    model, gguf_path, self._dim,
                )
            else:
                _log_warn(
                    "embeddings_gguf_unavailable — GGUF file found (%s) but "
                    "llama-cpp-python is not installed. Install with: "
                    "pip install llama-cpp-python. "
                    "Semantic search will be unavailable; keyword search still works.",
                    gguf_path,
                )
        elif api_key:
            self._backend = "api"
            self._available = _check_runtime("api")
            if self._available:
                logger.info(
                    "embeddings_backend=api model=%s dim=%d", model, self._dim,
                )
            else:
                _log_warn(
                    "embeddings_api_unavailable — api_key configured but "
                    "openai package is not installed. Install with: "
                    "pip install openai. "
                    "Semantic search will be unavailable; keyword search still works.",
                )
        else:
            _log_warn(
                "embeddings_disabled — no gguf_path or api_key configured. "
                "Semantic search will be unavailable; keyword search still works."
            )

    @classmethod
    def from_config(cls, config_path: str | None = None, quiet: bool = False) -> "EmbeddingClient":
        """Create an EmbeddingClient from slife.json5 config.

        Looks for:
          - memory.embedding.gguf_path → local GGUF model (takes priority)
          - memory.embedding.model → model name (for metadata and dim guessing)
          - models.providers.<first>.api_key → for API backend
          - models.providers.<first>.base_url → for API backend

        When *quiet* is True, unavailability messages are logged at DEBUG
        instead of WARNING — useful for health checks that probe status
        without alarming the user.
        """
        from slife.paths import get_config_path

        _log_warn = logger.debug if quiet else logger.warning

        try:
            import json5
        except ImportError:
            _log_warn("json5_not_installed — embeddings disabled")
            return cls(api_key="", quiet=quiet)

        if config_path is None:
            config_path = str(get_config_path())
        config_path = Path(config_path)
        if not config_path.exists():
            _log_warn("config_not_found path=%s", config_path)
            return cls(api_key="", quiet=quiet)

        try:
            raw = json5.loads(config_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            _log_warn("config_parse_error err=%s", e)
            return cls(api_key="", quiet=quiet)

        # Parse embedding config
        memory_cfg = raw.get("memory", {})
        emb_cfg = memory_cfg.get("embedding", {}) if isinstance(memory_cfg, dict) else {}
        if not isinstance(emb_cfg, dict):
            emb_cfg = {}

        model = emb_cfg.get("model", "bge-m3")
        gguf_path = emb_cfg.get("gguf_path")

        # If a GGUF path is configured, use it (takes priority over API)
        if gguf_path:
            gguf_path = str(Path(gguf_path).expanduser())
            dim = emb_cfg.get("dim", _guess_dim(model, gguf_path))
            return cls(model=model, gguf_path=gguf_path, dim=dim, quiet=quiet)

        # Otherwise, try API backend
        api_key = ""
        base_url = ""

        models_cfg = raw.get("models", {})
        providers = models_cfg.get("providers", {}) if isinstance(models_cfg, dict) else {}
        for _pid, pcfg in providers.items():
            if isinstance(pcfg, dict):
                api_key = pcfg.get("api_key", "")
                base_url = pcfg.get("base_url", "")
                if api_key:
                    break

        if not api_key:
            _log_warn(
                "no_api_key and no gguf_path — embeddings disabled. "
                "FTS5 keyword search will still work."
            )

        dim = emb_cfg.get("dim", _guess_dim(model))
        return cls(model=model, api_key=api_key, base_url=base_url, dim=dim, quiet=quiet)

    @property
    def available(self) -> bool:
        """Whether embeddings are available."""
        return self._available

    @property
    def dimension(self) -> int:
        """Embedding vector dimension."""
        return self._dim

    @property
    def backend(self) -> str:
        """Which backend is in use: 'gguf', 'api', or ''."""
        return self._backend

    @property
    def max_tokens(self) -> int:
        """Max tokens the model accepts for a single embedding."""
        return _guess_max_tokens(self._model)

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Generate embeddings for a list of texts.

        Returns None on failure — callers should handle gracefully.
        """
        if not self._available:
            return None

        if not texts:
            return []

        # Filter empty strings
        valid = [t for t in texts if t.strip()]
        if not valid:
            return [[0.0] * self._dim for _ in texts]

        try:
            if self._backend == "gguf":
                return await self._call_gguf(valid)
            else:
                return await self._call_api(valid)
        except Exception as e:
            logger.warning(
                "embedding_failed backend=%s err=%s", self._backend, e,
            )
            return None

    async def _call_gguf(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings using a local GGUF model via llama-cpp."""
        try:
            from llama_cpp import Llama
        except ImportError:
            logger.warning(
                "llama_cpp not installed. Install with: "
                "pip install llama-cpp-python"
            )
            return None

        if self._client is None:
            logger.info(
                "loading_gguf path=%s dim=%d", self._gguf_path, self._dim,
            )
            # llama-cpp-python's Llama constructor is not async,
            # but it's fast enough to call synchronously.
            self._client = Llama(
                model_path=self._gguf_path,
                embedding=True,
                n_ctx=8192,
                verbose=False,
            )
            logger.info("gguf_loaded model=%s", self._model)

        # llama-cpp-python's create_embedding is synchronous.
        # For short summaries, this is fast enough. For bulk
        # embedding, consider running in a thread pool.
        embeddings = []
        for text in texts:
            result = self._client.create_embedding(text)
            emb = result["data"][0]["embedding"]
            embeddings.append(emb)

        return embeddings

    async def _call_api(self, texts: list[str]) -> list[list[float]]:
        """Call the OpenAI embeddings API."""
        try:
            from openai import AsyncOpenAI
        except ImportError:
            logger.warning(
                "openai not installed. Install with: pip install openai"
            )
            return None

        if self._client is None:
            kwargs: dict = {"api_key": self._api_key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = AsyncOpenAI(**kwargs)

        response = await self._client.embeddings.create(
            model=self._model,
            input=texts,
        )
        return [d.embedding for d in response.data]

    async def embed_one(self, text: str) -> list[float] | None:
        """Generate embedding for a single text. Convenience method."""
        result = await self.embed([text])
        if result is None:
            return None
        return result[0] if result else None
