"""Embedding client — generates vectors for semantic search.

One backend: an OpenAI-compatible ``/v1/embeddings`` endpoint.  Local models
are NOT loaded in-process — the separate ``local-embed`` daemon serves them
over this same URL standard, so a local model and a remote provider are the
same client with a different ``base_url``.

Configured via slife.yaml → top-level ``embeddings`` section (shared by
memdb + memfiles).

Falls back gracefully when embeddings are unavailable — keyword
search (FTS5) still works fine without vectors.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any
from ruamel.yaml.error import YAMLError
from slife.env import is_env_ref
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

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

#: Bound the OpenAI-compatible API calls.  The SDK defaults to a 600s timeout
#: WITH retries — a blackholed / unreachable endpoint would stall the semantic
#: warm-up (and the drainer) for ~20 minutes.  A short timeout + no retries
#: makes an unavailable backend degrade fast (keyword search still works).
#: The deadline is developer-owned (registry transport.embed_api); retries
#: stay disabled (a count, not a duration).
_API_MAX_RETRIES = 0


def _known_model(model: str) -> tuple[int, int] | None:
    """Best-effort (dimension, max_tokens) for a model family we recognise."""
    for key, pair in _KNOWN_MODELS.items():
        if key in model.lower():
            return pair
    return None


def _guess_dim(model: str) -> int:
    """Guess the embedding dimension from the model name."""
    pair = _known_model(model)
    return pair[0] if pair else _DEFAULT_DIM


def _guess_max_tokens(model: str) -> int:
    """Guess the token limit from the model name."""
    pair = _known_model(model)
    return pair[1] if pair else _DEFAULT_MAX_TOKENS


def _check_runtime() -> bool:
    """Smoke-test that the ``openai`` SDK is importable.

    ``available`` must reflect *runtime* usability — not just whether an
    endpoint is configured.  Without this, a missing dependency is silently
    treated as "backend ready" until the first ``embed()`` call fails.
    """
    try:
        __import__("openai")
        return True
    except ImportError:
        return False


class EmbeddingClient:
    """Generates embeddings from an OpenAI-compatible endpoint.

    Usage::

        # From config
        client = EmbeddingClient.from_config()

        # Or explicit endpoint
        client = EmbeddingClient(api_key="sk-...", model="text-embedding-3-small")

        vectors = await client.embed(["summary text"])
    """

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        api_key: str = "",
        base_url: str = "",
        dim: int = 0,
        dim_known: bool | None = None,
        quiet: bool = False,
        enabled: bool = True,
    ):
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._dim = dim or _guess_dim(model)
        # A configured ``dim`` or a recognised model family makes the width
        # authoritative; a bare guess must be confirmed by probing the
        # backend before the vec0 table is created — a wrong width silently
        # drops every mis-sized embedding.
        if dim_known is not None:
            self._dim_known = dim_known
        else:
            self._dim_known = (dim or 0) > 0 or _known_model(model) is not None
        self._client: Any = None        # AsyncOpenAI
        # Serializes the lazy AsyncOpenAI client creation — two concurrent
        # first-time API embeds would otherwise both create a client and leak
        # one.
        self._client_init_lock = asyncio.Lock()
        self._backend: str = ""         # "api" | ""
        self._available = False

        # Explicitly disabled — skip all backend detection.
        if not enabled:
            self._available = False
            if not quiet:
                logger.info("embeddings_disabled_by_user")
            return

        _log_warn = logger.debug if quiet else logger.warning

        # Resolve backend — an OpenAI-compatible endpoint is the only one
        # slife speaks.  Local models (GGUF / HF) are served by the separate
        # local-embed daemon, which presents this same URL standard.
        if api_key:
            self._backend = "api"
            self._available = _check_runtime()
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
        """Create an EmbeddingClient from slife.yaml config.

        Reads the first-class top-level ``embeddings`` section — the shared
        memdb + memfiles config.  Each provider is one OpenAI-compatible
        endpoint:

          - ``embeddings.providers.<pid>.base_url/api_key`` — the endpoint
            (local-embed is one such endpoint)
          - ``embeddings.providers.<pid>.model`` — the model id POSTed on
            ``/v1/embeddings``
          - ``embeddings.active_model`` — the active provider id.

        The vector dimension is NOT configured — it is auto-detected: known
        model families are guessed (``_KNOWN_MODELS``); anything else is
        probed from the endpoint (``_probe_api_dim`` / ``_discover_model``)
        before the vec0 tables are built.

        When *quiet* is True, unavailability messages are logged at DEBUG
        instead of WARNING — useful for health checks that probe status
        without alarming the user.
        """
        from slife.paths import get_config_path

        _log_warn = logger.debug if quiet else logger.warning

        from slife.tools._yaml_doc import new_yaml

        if config_path is None:
            config_path = str(get_config_path())
        config_path_obj: Path = Path(config_path)
        if not config_path_obj.exists():
            _log_warn("config_not_found path=%s", config_path_obj)
            return cls(api_key="", quiet=quiet)

        try:
            raw = new_yaml().load(config_path_obj.read_text(encoding="utf-8"))
        except (YAMLError, ValueError, OSError) as e:
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

        # Resolve ${VAR} / keyring references — the raw embeddings section
        # ships with api_key: "${SILICONFLOW_API_KEY}" and, unlike the model
        # section (ModelConfig.from_dict) or the mcp_gateway embedding client,
        # this path never resolved it — a placeholder was blanked below → no
        # Authorization header → the provider rejects every embed with an
        # auth error.  Same chain as the model section: env → credstore →
        # literal.
        from slife.config import _resolve_secret
        api_key = _resolve_secret(api_key, accept_keyring_uri=True)

        # Skip still-unresolved ${VAR} placeholders — they are NOT real API
        # keys.  A ref that even credstore can't resolve must not be sent as
        # a Bearer token (the install template ships with one).
        if api_key and is_env_ref(api_key):
            api_key = ""

        if not base_url:
            _log_warn(
                "embeddings_unavailable backend=none reason=no_base_url"
            )

        # The width is never configured.  A recognised model family is a
        # good guess; otherwise it is provisional until the OpenAI endpoint
        # reports it (probe / /v1/models) before vec0 is built.
        _dim_known = _known_model(model) is not None
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
        "loaded" — there is no local model to materialise, so an available
        endpoint is always loaded.
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
        configured — the real width is only known once the endpoint reports
        it (``/v1/models``, or a probe embed).  Callers use this to defer
        creating the vec0 table until the width is real.
        """
        return self._dim_known

    @property
    def backend(self) -> str:
        """Which backend is in use: 'api', or '' when nothing is configured."""
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
            return await self._call_api(valid)
        except Exception as e:
            logger.warning(
                "embedding_failed backend=%s err=%s", self._backend, e,
            )
            return None

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

        Model selection is CONFIGURATION-AUTHORITATIVE: when the active
        provider configures a ``model``, that id is used verbatim and only
        its ``dimension`` (if reported) is picked up.  When the provider
        configures no model, the endpoint's first listed entry wins — a
        standard OpenAI backend has no ``active`` marker, models are peers.
        On success this pins ``self._model`` / ``self._dim``.
        Returns True on success.

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
                        kwargs: dict = {
                            "api_key": getattr(self, "_api_key", ""),
                            "timeout": _timeouts.timeouts.transport.embed_api,
                            "max_retries": _API_MAX_RETRIES,
                        }
                        if base_url:
                            kwargs["base_url"] = base_url
                        self._client = AsyncOpenAI(**kwargs)
                    client = self._client
            models = await client.models.list()
        except Exception as e:
            # Endpoint unreachable — treat as unavailable so the semantic
            # manager degrades (keyword search) instead of running a drainer
            # that fails on every batch.
            self._available = False
            logger.warning(
                "embedding_model_discover_failed base_url=%s err=%s",
                base_url, e,
            )
            return False

        entries = [m for m in (models.data or []) if getattr(m, "id", None)]
        if not entries:
            self._available = False
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
                # The configured model is listed — but its backend may be
                # unavailable (local-embed reports available=false when the
                # dependency isn't installed).  Surface that so the caller
                # degrades instead of 503-looping in the drainer.
                if not getattr(match, "available", True):
                    self._available = False
                    logger.warning(
                        "embedding_model_unavailable model=%s base_url=%s "
                        "(backend dependency missing — keyword search only)",
                        configured, base_url,
                    )
                    return False
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

        # No configured model — the endpoint's first entry (standard OpenAI
        # /v1/models listing; models are peers, no active marker).
        entry = entries[0]
        if not getattr(entry, "available", True):
            self._available = False
            logger.warning(
                "embedding_model_unavailable model=%s base_url=%s "
                "(backend dependency missing — keyword search only)",
                entry.id, base_url,
            )
            return False
        new_model = entry.id
        new_dim = int(getattr(entry, "dimension", 0) or 0)
        if new_model and new_model != self._model:
            logger.info(
                "embedding_model_pinned model=%s (was %s)", new_model, self._model,
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
        """Pin the endpoint's model and real dimension; return True when ready.

        There is no local model to materialise — the endpoint is the model.
        But *which* model it serves (and therefore the vector width) is the
        endpoint's fact, not the config's: discover it from ``/v1/models``
        and pin the width before the vec0 table uses it.  The semantic gate
        calls this from every search/check, so it is idempotent.
        """
        if self._backend != "api":
            return False
        await self._discover_model()
        if not self._available:
            # Endpoint unreachable, no usable model, or the listed model's
            # backend dependency is missing — degrade instead of running a
            # drainer that 503s on every batch.
            logger.warning(
                "embedding_api_unavailable model=%s base_url=%s "
                "(backend not ready — keyword search only)",
                self._model, getattr(self, "_base_url", ""),
            )
            return False
        if not self._dim_known:
            # The endpoint could not pin the real width — this is the
            # configured-model-not-listed case, which _discover_model keeps
            # and reports True, but its guess may be wrong.  A wrong width
            # silently drops every vec0 insert of a different size, so probe
            # it once before the gate opens.
            await self._probe_api_dim()
        return True

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
                    kwargs: dict = {
                        "api_key": self._api_key,
                        "timeout": _timeouts.timeouts.transport.embed_api,
                        "max_retries": _API_MAX_RETRIES,
                    }
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
