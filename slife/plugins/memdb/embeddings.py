"""Embedding client — generates vectors for semantic search.

Supports three backends:
  1. Local GGUF model (llama-cpp-python) — offline, no API cost
  2. Local transformer model (sentence-transformers) — offline, HF hub
  3. OpenAI-compatible API — remote, requires API key

Configured via slife.json5 → top-level ``embeddings`` section (shared by
memdb + memfiles; OpenAI-compatible endpoints).

Falls back gracefully when embeddings are unavailable — keyword
search (FTS5) still works fine without vectors.
"""

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

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

#: Fallback dim/token-limit for an unknown model — *provisional* until the
#: backend reports the real width (see ``dimension_known``).
_DEFAULT_DIM = 1024
_DEFAULT_MAX_TOKENS = 8192


def _known_model(model: str) -> tuple[int, int] | None:
    """Best-effort (dimension, max_tokens) for a model family we recognise."""
    for key, pair in _KNOWN_MODELS.items():
        if key in model.lower():
            return pair
    return None


def _guess_dim(model: str, gguf_path: str | None = None) -> int:
    """Guess the embedding dimension from the model name."""
    pair = _known_model(model)
    return pair[0] if pair else _DEFAULT_DIM


def _guess_max_tokens(model: str) -> int:
    """Guess the token limit from the model name."""
    pair = _known_model(model)
    return pair[1] if pair else _DEFAULT_MAX_TOKENS


#: Maps each backend to the import that proves it's usable at runtime.
_BACKEND_RUNTIME_IMPORTS: dict[str, tuple[str, str]] = {
    "gguf":        ("llama_cpp",              "llama-cpp-python"),
    "transformer": ("sentence_transformers",  "sentence-transformers"),
    "api":         ("openai",                 "openai"),
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


def _looks_like_placeholder(value: str) -> bool:
    """True if *value* is an unresolved ``${VAR}`` or ``${VAR:-default}`` placeholder.

    The install template ships with ``api_key: "${DEEPSEEK_API_KEY}"`` —
    these are NOT real API keys and should be skipped when probing
    provider configs for an embedding backend.
    """
    return value.startswith("${") and value.endswith("}")


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
        dim_known: bool | None = None,
        quiet: bool = False,
        backend: str = "",
        device: str = "",
        enabled: bool = True,
    ):
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._gguf_path = gguf_path
        self._dim = dim or _guess_dim(model, gguf_path)
        # A configured ``dim`` or a recognised model family makes the width
        # authoritative; a bare guess must be confirmed by probing the
        # backend before the vec0 table is created — a wrong width silently
        # drops every mis-sized embedding.
        if dim_known is not None:
            self._dim_known = dim_known
        else:
            self._dim_known = (dim or 0) > 0 or _known_model(model) is not None
        self._client: Any = None        # AsyncOpenAI, Llama, or SentenceTransformer
        # Serializes the lazy AsyncOpenAI client creation — two concurrent
        # first-time API embeds would otherwise both create a client and leak
        # one.
        self._client_init_lock = asyncio.Lock()
        # In-flight load future — concurrent load() calls share one model
        # materialisation instead of each loading the local model (the
        # semantic gate calls load() from every search/check).
        self._loading: asyncio.Future | None = None
        # Serialises model inference — llama-cpp / sentence-transformers
        # instances are NOT safe for concurrent encode calls, and concurrent
        # searches (main agent + subagent) would otherwise crash the process
        # (gguf concurrent create_embedding → native abort).
        self._embed_lock = threading.Lock()
        self._backend: str = ""         # "gguf" | "transformer" | "api" | ""
        self._available = False
        self._device = device           # "cpu" | "cuda" (transformer only)
        self._enabled = enabled         # False when user called memory_disable_embedding

        # Explicitly disabled — skip all backend detection.
        if not enabled:
            self._available = False
            if not quiet:
                logger.info("embeddings_disabled_by_user")
            return

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
                    "embeddings_unavailable backend=gguf model=%s reason=llama_cpp_not_installed "
                    "hint='uv pip install llama-cpp-python'",
                    model,
                )
        elif backend == "transformer":
            self._backend = "transformer"
            self._available = _check_runtime("transformer")
            if self._available:
                logger.info(
                    "embeddings_backend=transformer model=%s dim=%d",
                    model, self._dim,
                )
            else:
                _log_warn(
                    "embeddings_unavailable backend=transformer model=%s reason=sentence_transformers_not_installed "
                    "hint='uv pip install sentence-transformers'",
                    model,
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
                    "embeddings_unavailable backend=api model=%s reason=openai_not_installed "
                    "hint='uv pip install openai'",
                    model,
                )
        else:
            _log_warn(
                "embeddings_unavailable backend=none reason=no_config"
            )

    @classmethod
    def from_config(cls, config_path: str | None = None, quiet: bool = False) -> "EmbeddingClient":
        """Create an EmbeddingClient from slife.json5 config.

        Reads the first-class top-level ``embeddings`` section — the shared
        memdb + memfiles config.  Two levels mirror the LLM
        ``models.providers`` shape:

          - ``embeddings.providers.<pid>.base_url/api_key`` — OpenAI-compatible
            endpoint (local-embed is one such endpoint)
          - ``embeddings.active_model`` = ``"provider/model"`` or bare
            ``"provider"`` — configuration-authoritative.  A bare provider
            (or a configured model the endpoint doesn't list) defers the
            model to the endpoint's /v1/models active model on ``load()``.

        When *quiet* is True, unavailability messages are logged at DEBUG
        instead of WARNING — useful for health checks that probe status
        without alarming the user.
        """
        from slife.paths import get_config_path

        _log_warn = logger.debug if quiet else logger.warning

        try:
            import json5
        except ImportError:
            _log_warn("json5_not_installed reason=json5_missing")
            return cls(api_key="", quiet=quiet)

        if config_path is None:
            config_path = str(get_config_path())
        config_path_obj: Path = Path(config_path)
        if not config_path_obj.exists():
            _log_warn("config_not_found path=%s", config_path_obj)
            return cls(api_key="", quiet=quiet)

        try:
            raw = json5.loads(config_path_obj.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            _log_warn("config_parse_error err=%s", e)
            return cls(api_key="", quiet=quiet)

        emb_cfg = raw.get("embeddings", {})
        if not isinstance(emb_cfg, dict):
            emb_cfg = {}

        enabled = bool(emb_cfg.get("enabled", True))
        if not enabled:
            return cls(enabled=False, quiet=quiet)

        from slife.plugins.memdb.embedding_config import _active_endpoint
        ep = _active_endpoint(emb_cfg)
        api_key = ep["api_key"]
        base_url = ep["base_url"]
        model = ep["model"]
        dim = ep["dim"]

        # Skip unresolved ${VAR} placeholders — they are NOT real API keys.
        # The install template ships with api_key: "${DEEPSEEK_API_KEY}" and
        # real resolution happens at the Config level, not here.
        if api_key and _looks_like_placeholder(api_key):
            api_key = ""

        if not base_url:
            _log_warn(
                "embeddings_unavailable backend=none reason=no_base_url"
            )

        # A configured ``dim`` is authoritative; a recognised model family
        # is a good guess.  Otherwise the width is provisional (1024) until
        # the backend reports it (probe / /v1/models) before vec0 is built.
        _dim_known = bool(dim) or _known_model(model) is not None
        if not dim:
            dim = _guess_dim(model)
        return cls(model=model, api_key=api_key, base_url=base_url, dim=dim,
                   dim_known=_dim_known, quiet=quiet, enabled=enabled)

    @property
    def available(self) -> bool:
        """Whether embeddings are available."""
        return self._available

    @property
    def loaded(self) -> bool:
        """Whether the backend model is actually in memory.

        ``available`` means "configured and usable"; ``loaded`` means the
        local model has been materialised.  The API backend is always
        "loaded" (no local model); gguf/transformer load lazily, so saves
        can skip embedding (defer to the reindex) until the model is here.
        """
        if not self._available:
            return False
        if self._backend == "api":
            return True
        return self._client is not None

    @property
    def dimension(self) -> int:
        """Embedding vector dimension."""
        return self._dim

    @property
    def dimension_known(self) -> bool:
        """Whether ``dimension`` is authoritative or a provisional guess.

        False when the model is outside ``_KNOWN_MODELS`` and no ``dim`` was
        configured — the real width is only known once the backend reports it
        (gguf ``n_embd`` after load, API after a probe embed).  Callers use
        this to defer creating the vec0 table until the width is real.
        """
        return self._dim_known

    @property
    def backend(self) -> str:
        """Which backend is in use: 'gguf', 'api', or ''."""
        return self._backend

    @property
    def max_tokens(self) -> int:
        """Max tokens the model accepts for a single embedding."""
        return _guess_max_tokens(self._model)

    async def ensure_loaded(self) -> int:
        """Ensure the backend model is loaded and ``self._dim`` reflects its
        real output dimension; return the dimension.

        Local models (transformer, and gguf via ``_load_gguf``) only report
        their width once loaded — the width is never a guess here.  Call
        before the vec0 table is created so the schema uses the real width:
        a guessed dimension silently drops every embedding of a different
        width.
        """
        if self._backend == "transformer" and self._client is None:
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
            except ImportError:
                return self._dim

            from slife.threads import run_daemon

            logger.info(
                "loading_transformer model=%s device=%s",
                self._model, self._device or "auto",
            )
            device = self._device or None  # None = auto-detect
            # SentenceTransformer can block on first load (download + warm-up);
            # run it on a daemon thread so a hung load can never hang the
            # plugin's interpreter shutdown (threads.py convention).
            self._client = await run_daemon(
                lambda: SentenceTransformer(self._model, device=device),
                name="transformer-load",
            )
            actual_dim = self._client.get_sentence_embedding_dimension()
            if actual_dim and actual_dim != self._dim:
                logger.info(
                    "transformer_dim_override configured=%d actual=%d",
                    self._dim, actual_dim,
                )
                self._dim = actual_dim
            logger.info("transformer_loaded model=%s dim=%d", self._model, self._dim)
        return self._dim

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
            elif self._backend == "transformer":
                return await self._call_transformer(valid)
            else:
                return await self._call_api(valid)
        except Exception as e:
            logger.warning(
                "embedding_failed backend=%s err=%s", self._backend, e,
            )
            return None

    @staticmethod
    def _read_model_dim(client) -> int:
        """Read a loaded llama-cpp model's embedding width defensively.

        ``n_embd`` is a bound *method* on llama_cpp 0.3.34 — ``int(n_embd)``
        raises ``TypeError`` and would fail every GGUF load.  Accept the
        property value or invoke the method, then convert; anything else
        (0, None, a type that won't coerce) degrades to 0 so the caller
        keeps its guessed dimension instead of crashing the load.
        """
        raw = getattr(client, "n_embd", 0)
        if callable(raw):
            try:
                raw = raw()
            except Exception:
                raw = 0
        try:
            return int(raw or 0)  # type: ignore
        except (TypeError, ValueError):
            return 0

    async def _load_gguf(self) -> None:
        """Materialise the llama-cpp Llama client for the gguf backend.

        Leaves ``_client`` None when llama-cpp-python is absent — the
        embed() wrapper then reports the backend unavailable.  The Llama
        constructor is synchronous and can block on first load; it runs on
        a daemon thread so a hung load can never hang plugin shutdown
        (threads.py convention).
        """
        try:
            from llama_cpp import Llama  # type: ignore[import-not-found]
        except ImportError:
            logger.warning(
                "embeddings_unavailable backend=gguf reason=llama_cpp_not_installed "
                "hint='uv pip install llama-cpp-python'"
            )
            return
        gguf_path = self._gguf_path
        assert gguf_path is not None  # guaranteed by _backend == "gguf"
        logger.info("loading_gguf path=%s dim=%d", gguf_path, self._dim)
        from slife.threads import run_daemon

        self._client = await run_daemon(
            lambda: Llama(
                model_path=gguf_path,
                embedding=True,
                n_ctx=8192,
                verbose=False,
            ),
            name="gguf-load",
        )
        # llama-cpp exposes the model's real width after load — correct a
        # guessed dimension now, before the vec0 table is created (a wrong
        # width silently drops every mis-sized embedding).
        actual_dim = self._read_model_dim(self._client)
        if actual_dim and actual_dim != self._dim:
            logger.info(
                "gguf_dim_override model=%s configured=%d actual=%d",
                self._model, self._dim, actual_dim,
            )
            self._dim = actual_dim
            self._dim_known = True
        logger.info("gguf_loaded model=%s", self._model)

    async def _probe_api_dim(self) -> None:
        """Pin the real API embedding width with one cheap single-token embed.

        For models outside ``_KNOWN_MODELS`` the guessed dimension may be
        wrong, and a wrong width silently drops every vec0 insert of a
        different size.  Called from ``load()`` before the semantic gate
        opens; on failure the guess stays and the next ``load()`` retries.
        """
        try:
            response = await self._call_api(["."])
        except Exception as e:
            logger.warning(
                "embedding_dim_probe_failed backend=api model=%s err=%s",
                self._model, e,
            )
            return
        if not response or not response[0]:
            logger.warning(
                "embedding_dim_probe_empty backend=api model=%s", self._model,
            )
            return
        actual = len(response[0])
        if actual and actual != self._dim:
            logger.info(
                "api_dim_override model=%s guessed=%d actual=%d",
                self._model, self._dim, actual,
            )
            self._dim = actual
        self._dim_known = True

    async def _discover_model(self) -> bool:
        """Query ``GET {base_url}/models`` to pin the model + dimension.

        Model selection is CONFIGURATION-AUTHORITATIVE: when the config
        names a model (``active_model = "provider/model"``), that id is
        used verbatim and only its ``dimension`` (if reported) is picked
        up.  When the config names no model (bare ``"provider"``), the
        endpoint's ``active`` model wins (local-embed sets ``active:
        true`` on /v1/models); otherwise the first entry.  On success this
        pins ``self._model`` / ``self._dim``.  Returns True on success.

        Defensive about attributes — tests construct clients via
        ``__new__`` without running ``__init__``.
        """
        base_url = getattr(self, "_base_url", "")
        if not base_url:
            return False
        try:
            from openai import AsyncOpenAI

            client = self._client
            if client is None:
                async with self._client_init_lock:
                    if self._client is None:
                        kwargs: dict = {"api_key": getattr(self, "_api_key", "")}
                        if base_url:
                            kwargs["base_url"] = base_url
                        self._client = AsyncOpenAI(**kwargs)
                    client = self._client
            models = await client.models.list()
        except Exception as e:
            logger.warning(
                "embedding_model_discover_failed base_url=%s err=%s",
                base_url, e,
            )
            return False

        entries = [m for m in (models.data or []) if getattr(m, "id", None)]
        if not entries:
            return False

        configured = getattr(self, "_model", "")
        if configured:
            # Configuration-authoritative: the configured id wins.  Just
            # pick up its dimension when the endpoint reports one.
            match = next(
                (m for m in entries if getattr(m, "id", "") == configured),
                None,
            )
            if match is not None:
                new_dim = int(getattr(match, "dimension", 0) or 0)
                if new_dim:
                    if new_dim != self._dim:
                        logger.info(
                            "api_dim_override model=%s configured=%d actual=%d",
                            self._model, self._dim, new_dim,
                        )
                        self._dim = new_dim
                    self._dim_known = True
                return True
            # Configured model not listed by the endpoint — keep it anyway
            # (the endpoint may serve it without listing), and probe its dim.
            return True

        # No configured model — endpoint's active (local-embed) or first entry.
        active = next(
            (m for m in entries if getattr(m, "active", False)),
            entries[0],
        )
        new_model = active.id
        new_dim = int(getattr(active, "dimension", 0) or 0)
        if new_model and new_model != self._model:
            logger.info(
                "embedding_model_active model=%s (was %s)", new_model, self._model,
            )
            self._model = new_model
        if new_dim:
            if new_dim != self._dim:
                logger.info(
                    "api_dim_override model=%s guessed=%d actual=%d",
                    self._model, self._dim, new_dim,
                )
                self._dim = new_dim
            self._dim_known = True
        return True

    async def load(self) -> bool:
        """Force the local backend model into memory; return True when loaded.

        gguf/transformer models load lazily on first embed.  After a reload
        there is nothing left to embed when the index is already complete,
        so nothing would materialise the model and the semantic gate would
        stay locked off — load it here so the gate can open.  Idempotent;
        the API backend is always ready (but probes its real dimension once
        when the configured model is not recognised).
        """
        if self._client is not None or self._backend == "api":
            # The API backend has no local model to load — but the model
            # itself is determined by the endpoint (e.g. the local-embed
            # plugin's active model), so discover it from /v1/models and pin
            # the real dimension before the vec0 table uses it.
            if self._backend == "api":
                if not await self._discover_model():
                    if not self._dim_known:
                        await self._probe_api_dim()
            return True
        if self._loading is not None:
            return await self._loading  # share the in-flight load
        ok = False
        self._loading = asyncio.get_running_loop().create_future()
        try:
            if self._backend == "transformer":
                await self.ensure_loaded()
            elif self._backend == "gguf":
                await self._load_gguf()
            ok = self._client is not None
        except Exception as e:
            logger.warning(
                "embedding_load_failed backend=%s err=%s", self._backend, e,
            )
        finally:
            # Always resolve the shared future and clear the slot.  A
            # CancelledError (a BaseException, not caught above) must not leave
            # _loading pointing at a future that never resolves — every later
            # load()/enable() would hang forever.
            if not self._loading.done():
                self._loading.set_result(ok)
            self._loading = None
        return ok

    async def _call_gguf(self, texts: list[str]) -> list[list[float]] | None:
        """Generate embeddings using a local GGUF model via llama-cpp."""
        if self._client is None:
            await self._load_gguf()
        if self._client is None:
            return None

        # llama-cpp-python's create_embedding is synchronous — offload it to a
        # daemon thread (threads.py convention) so a bulk embed or reindex
        # never blocks the asyncio event loop.
        from slife.threads import run_daemon

        def _encode() -> list[list[float]]:
            out = []
            for text in texts:
                # The Llama instance is NOT safe for concurrent
                # create_embedding — a burst of hybrid searches (main agent
                # + subagents share this server) would crash llama.cpp natively.
                # Serialise per call. threading.Lock, not asyncio.Lock: the
                # encode runs on a daemon thread, so the lock must arbitrate
                # across threads, and interleave single embeds between a
                # reindex's batch instead of blocking a search on the whole batch.
                with self._embed_lock:
                    result = self._client.create_embedding(text)
                out.append(result["data"][0]["embedding"])
            return out

        return await run_daemon(_encode, name="gguf-embed")

    async def _call_transformer(self, texts: list[str]) -> list[list[float]] | None:
        """Generate embeddings using a local HuggingFace model via
        sentence-transformers."""
        if self._client is None:
            # Loads the model (and corrects _dim to its real width).
            # Returns without loading if sentence-transformers is absent.
            await self.ensure_loaded()
        if self._client is None:
            logger.warning(
                "embeddings_unavailable backend=transformer reason=sentence_transformers_not_installed "
                "hint='uv pip install sentence-transformers'"
            )
            return None

        # encode() is synchronous; run it on a daemon thread (threads.py
        # convention — a blocked encode must never hang shutdown).
        from slife.threads import run_daemon

        def _encode() -> Any:
            # Same thread-safety rule as the gguf backend — a shared
            # SentenceTransformer instance must not encode concurrently.
            with self._embed_lock:
                return self._client.encode(
                    texts,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )

        embeddings = await run_daemon(_encode, name="transformer-encode")
        return [emb.tolist() for emb in embeddings]

    async def _call_api(self, texts: list[str]) -> list[list[float]] | None:
        """Call the OpenAI embeddings API."""
        try:
            from openai import AsyncOpenAI
        except ImportError:
            logger.warning(
                "embeddings_unavailable backend=api reason=openai_not_installed "
                "hint='uv pip install openai'"
            )
            return None

        if self._client is None:
            async with self._client_init_lock:
                if self._client is None:  # double-checked under the lock
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
