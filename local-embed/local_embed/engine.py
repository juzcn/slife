"""Embedding engine — one process, many models, ONE active.

Owns one or more local embedding models (GGUF via llama-cpp-python, HF
transformers via sentence-transformers), each loaded lazily on first use
and cached.  Because local-embed is a standalone process, this is the only
place a model is ever materialised in the whole process tree — every
consumer (slife's memdb + memfiles) calls it over HTTP instead of loading
its own copy.

Exactly one model is *active* at a time — the one requests land on unless
they name another explicitly.  Switching active model (``set_active``)
loads it on demand and reports its real dimension, so a vector table is
always sized from the width the server actually produces.

Thread-safety: llama-cpp ``create_embedding`` and
``SentenceTransformer.encode`` are NOT safe for concurrent calls — a burst
of concurrent requests would crash llama.cpp natively.  Encode runs on a
daemon thread (``run_daemon``) and is serialised with a single
``threading.Lock`` across every model, so a bulk reindex batch and an
interactive search interleave single embeds instead of running
concurrently.

Dimension: the real output width is only known once the model is loaded
(``n_embd`` / ``get_sentence_embedding_dimension``).  A guessed dimension
must never be served — the server reports the real width so a vec0 table
is created with the correct size.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from contextlib import contextmanager, redirect_stdout
from typing import Any

from local_embed.threads import run_daemon

logger = logging.getLogger(__name__)

# Known embedding dimensions and token limits by model family.
_KNOWN_MODELS: dict[str, tuple[int, int]] = {
    "text-embedding-3-small": (1536, 8191),
    "text-embedding-3-large": (3072, 8191),
    "text-embedding-ada-002":  (1536, 8191),
    "bge-m3":                  (1024, 8192),
    "bge-large":               (1024, 512),
    "nomic-embed-text":        (768,  8192),
}

#: Provisional dim/token-limit for an unknown model — corrected once the
#: backend reports its real width (see ``ensure_loaded``).
_DEFAULT_DIM = 1024
_DEFAULT_MAX_TOKENS = 8192


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


# Optional backend classes — resolved LAZILY, once, on the first backend
# availability check (never at module import).  `sentence_transformers`
# drags in torch/transformers (~5s of imports) and `llama_cpp` is heavy
# too; importing either at module load delays the plugin's port signal past
# the host's spawn window even when the backend is never used.  So module
# import stays fast: these names start ``None`` and are filled in by
# :func:`_resolve_backend` on demand.
#
# Tests patch ``local_embed.engine._Llama`` / ``_SentenceTransformer``
# directly — those module attributes MUST keep existing as plain names so
# ``patch()`` replaces them.  A test patch (a non-None value) also counts
# as "resolved" and is never overwritten by a real import.
_Llama: Any | None = None
_SentenceTransformer: Any | None = None

#: Backend -> (module attribute to fill, lazy import callable).
_BACKEND_IMPORTS: dict[str, tuple[str, Any]] = {}


def _resolve_backend(backend: str) -> None:
    """Import one optional backend into its module attribute, once.

    Runs the first time a backend's availability is queried (and again if
    the module attribute was patched back to None between calls).  A
    patched, non-None attribute is left untouched — tests use that to
    substitute a fake backend without triggering any real import.
    """
    import importlib

    spec = _BACKEND_IMPORTS.get(backend)
    if spec is None:
        return
    attr_name, _ = spec
    current = globals().get(attr_name)
    if current is not None:
        return  # already resolved (or test-patched)
    try:
        module = importlib.import_module(spec[1])
        globals()[attr_name] = getattr(module, "Llama" if backend == "gguf" else "SentenceTransformer")
    except (ImportError, AttributeError):
        globals()[attr_name] = None  # dependency missing — backend unavailable


def _init_backend_imports() -> None:
    """Register the (lazy) backend imports.  Called once at module bottom."""
    _BACKEND_IMPORTS.clear()
    _BACKEND_IMPORTS["gguf"] = ("_Llama", "llama_cpp")
    _BACKEND_IMPORTS["transformer"] = ("_SentenceTransformer", "sentence_transformers")


@contextmanager
def _guarded_stdout():
    """Redirect a closed stdout to devnull for the duration.

    The plugin contract closes stdout after the port signal, but a
    transformer model load (sentence-transformers/tqdm/torch) writes
    progress bars to ``sys.stdout`` — a write to the closed file raises
    ``ValueError: I/O operation on closed file`` and fails the load.
    Wrapping the constructor with this keeps the load working when the
    host has already read the port signal and closed our stdout.
    """
    if sys.stdout is None or sys.stdout.closed:
        with open(os.devnull, "w", encoding="utf-8") as devnull, redirect_stdout(devnull):
            yield
    else:
        yield


def check_backend_runtime(backend: str) -> bool:
    """Whether the Python package a backend needs is already importable.

    ``available`` must reflect *runtime* usability — not just whether a
    GGUF file exists on disk.  Without this, a missing dependency is
    silently treated as "backend ready" until the first embed fails.

    Deliberately does NOT trigger a lazy import: it is called for EVERY
    configured model at ``Engine.__init__`` (the ``model_configured`` log),
    and resolving an inactive backend there would pay its import cost even
    when only the active model is ever used.  The import happens in
    :meth:`Engine._load_spec`, i.e. only when a model is actually loaded.
    """
    if backend == "gguf":
        return _Llama is not None
    if backend == "transformer":
        return _SentenceTransformer is not None
    return False


def resolve_backend_runtime(backend: str) -> bool:
    """Resolve the backend's lazy import (once) and report real availability.

    :func:`check_backend_runtime` deliberately does NOT import — it is called
    for every configured model at ``Engine.__init__``, which must stay
    handshake-fast.  A standalone entry point (the CLI) that wants a truthful
    answer *before* serving should call this instead: it triggers the one-time
    import and then reports whether the package is really usable.
    """
    _resolve_backend(backend)
    return check_backend_runtime(backend)


class ModelSpec:
    """One configured embedding model — name key + backend/weights."""

    __slots__ = (
        "backend", "device", "dim", "dim_known", "gguf_path",
        "max_tokens", "model", "name",
    )

    def __init__(
        self,
        name: str,
        *,
        backend: str = "gguf",
        model: str | None = None,
        gguf_path: str | None = None,
        device: str = "",
        max_tokens: int = 0,
    ):
        self.name = name
        self.backend = backend
        self.model = model or name
        self.gguf_path = gguf_path
        self.device = device
        self.max_tokens = max_tokens or _guess_max_tokens(self.model)
        self.dim = _guess_dim(self.model)
        self.dim_known = _known_model(self.model) is not None

    def runtime_available(self) -> bool:
        """Whether this model's backend dependency is importable."""
        return check_backend_runtime(self.backend)


class Engine:
    """The embedding engine — many models, one active, lazy-loaded.

    Usage::

        engine = Engine(specs=[ModelSpec("bge-m3", backend="gguf",
                                         gguf_path="/p/model.gguf")],
                        active="bge-m3")
        await engine.ensure_loaded()          # materialises the active model
        vecs = await engine.embed(["text"])   # list[list[float]]
    """

    def __init__(
        self,
        *,
        specs: list[ModelSpec] | None = None,
        active: str | None = None,
        # Single-model convenience (backwards compatible):
        backend: str = "gguf",
        model: str = "bge-m3",
        gguf_path: str | None = None,
        device: str = "",
        max_tokens: int = 0,
    ):
        if specs:
            self._specs: dict[str, ModelSpec] = {s.name: s for s in specs}
        else:
            self._specs = {
                model: ModelSpec(
                    model, backend=backend, model=model,
                    gguf_path=gguf_path, device=device, max_tokens=max_tokens,
                )
            }
        self._active = active if active in self._specs else next(iter(self._specs), "")
        # name -> loaded client (Llama | SentenceTransformer)
        self._clients: dict[str, Any] = {}
        # name -> real dimension once loaded
        self._dims: dict[str, int] = {}
        # name -> load failures (load attempted and failed)
        self._failed: set[str] = set()
        # name -> in-flight load future (concurrent load() calls share one)
        self._loading: dict[str, asyncio.Future] = {}
        # Serialises model inference across ALL models — see the module doc.
        self._encode_lock = threading.Lock()

        # No backend is resolved here — construction must be handshake-fast
        # (a transformer active model would otherwise import torch and delay
        # the port signal past the host's spawn window).  The active model's
        # backend is resolved asynchronously by the server's post-handshake
        # warm-up (``warm_after_handshake``) or on the first embed; inactive
        # models stay unresolved until they are loaded.
        for spec in self._specs.values():
            logger.info(
                "model_configured name=%s backend=%s model=%s dim=%d dim_known=%s "
                "available=%s",
                spec.name, spec.backend, spec.model, spec.dim, spec.dim_known,
                spec.runtime_available(),
            )

    # ── active model (public) ─────────────────────────────────────────

    @property
    def active_model(self) -> str:
        return self._active

    @property
    def models(self) -> list[str]:
        return list(self._specs)

    def model_spec(self, name: str | None = None) -> ModelSpec:
        """Return the spec for *name* (default active); raises on unknown."""
        name = name or self._active
        try:
            return self._specs[name]
        except KeyError:
            raise KeyError(f"unknown model: {name}") from None

    @property
    def backend(self) -> str:
        return self.model_spec().backend

    @property
    def model(self) -> str:
        return self.model_spec().model

    @property
    def available(self) -> bool:
        """Whether the active model's backend is usable (not failed)."""
        spec = self.model_spec()
        return spec.runtime_available() and self._active not in self._failed

    @property
    def loaded(self) -> bool:
        """Whether the active model is actually materialised in memory."""
        return self._active in self._clients

    def is_loaded(self, name: str) -> bool:
        return name in self._clients

    @property
    def dimension(self) -> int:
        """Active model's embedding dimension (real once loaded)."""
        spec = self.model_spec()
        return self._dims.get(self._active, spec.dim)

    @property
    def dimension_known(self) -> bool:
        """Whether ``dimension`` is authoritative or a provisional guess."""
        spec = self.model_spec()
        return spec.dim_known or self._active in self._dims

    @property
    def max_tokens(self) -> int:
        return self.model_spec().max_tokens

    # ── loading ───────────────────────────────────────────────────────

    async def ensure_loaded(self, name: str | None = None) -> int:
        """Load the model (default active) if needed; return its real dim.

        Idempotent — concurrent callers share one in-flight load per model.
        """
        name = name or self._active
        spec = self.model_spec(name)
        if name in self._clients:
            return self._dims.get(name, spec.dim)
        fut = self._loading.get(name)
        if fut is not None:
            return await fut

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._loading[name] = fut
        try:
            await self._load_spec(spec)
        except asyncio.CancelledError:
            # Never leave _loading pointing at a future that won't resolve.
            if not fut.done():
                fut.cancel()
            self._loading.pop(name, None)
            raise
        except Exception as e:  # noqa: BLE001 — a failed load must degrade, not crash
            logger.warning("load_failed name=%s backend=%s err=%s", name, spec.backend, e)
            self._failed.add(name)
            self._clients.pop(name, None)
            if not fut.done():
                fut.set_result(spec.dim)
        else:
            if not fut.done():
                fut.set_result(self._dims.get(name, spec.dim))
        finally:
            self._loading.pop(name, None)
        return self._dims.get(name, spec.dim)

    async def set_active(self, name: str) -> int:
        """Switch the active model (loading it on demand); return its dim."""
        self.model_spec(name)  # raises on unknown
        self._active = name
        return await self.ensure_loaded(name)

    # ── embedding ────────────────────────────────────────────────────

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Embed a list of texts with *model* (default active).

        Returns one vector per non-empty input; empty/whitespace inputs get
        a zero vector of the model's dim (row alignment).  Raises when the
        backend is unavailable — the server turns that into a 503.
        """
        name = model if (model and model in self._specs) else self._active
        spec = self.model_spec(name)
        # Resolving here (not in runtime_available) means the backend's
        # heavy import happens exactly once, only when that model is about
        # to be used — never at engine construction for inactive models.
        _resolve_backend(spec.backend)
        if not spec.runtime_available() or name in self._failed:
            raise RuntimeError(
                f"embedding backend unavailable: {spec.backend} "
                "(dependency missing or load failed)"
            )
        if not texts:
            return []
        if name not in self._clients:
            await self.ensure_loaded(name)
        client = self._clients.get(name)
        if client is None:
            raise RuntimeError(f"embedding backend failed to load: {spec.backend}")

        valid = [t for t in texts if t.strip()]
        if not valid:
            return [[0.0] * spec.dim for _ in texts]

        dim = self._dims.get(name, spec.dim)
        if spec.backend == "gguf":
            vecs = await self._encode_gguf(client, valid)
        else:
            vecs = await self._encode_transformer(client, valid)

        # Restore row alignment for empty inputs (zero vector).
        result: list[list[float]] = []
        it = iter(vecs)
        for t in texts:
            result.append(next(it) if t.strip() else [0.0] * dim)
        return result

    # ── internal ─────────────────────────────────────────────────────

    @staticmethod
    def _read_model_dim(client: Any) -> int:
        """Read a loaded llama-cpp model's embedding width defensively.

        ``n_embd`` is a bound *method* on llama_cpp 0.3.34 — ``int(n_embd)``
        raises ``TypeError`` and would fail every GGUF load.  Accept the
        property value or invoke the method, then convert; anything else
        degrades to 0 so the caller keeps its guessed dimension.
        """
        raw = getattr(client, "n_embd", 0)
        if callable(raw):
            try:
                raw = raw()
            except Exception:  # noqa: BLE001 — defensive: any probe failure degrades to dim 0
                raw = 0
        try:
            return int(raw or 0)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    async def _load_spec(self, spec: ModelSpec) -> None:
        """Materialise one model into ``_clients[spec.name]``.

        The backend constructors are synchronous and can block on first load
        (download + warm-up); they run on a daemon thread so a hung load can
        never hang shutdown (threads.py convention).  The real width is read
        after load and corrects the guessed dimension.
        """
        if spec.backend == "gguf":
            _resolve_backend("gguf")
            if _Llama is None:
                logger.warning(
                    "backend_unavailable name=%s backend=gguf "
                    "reason=llama_cpp_not_installed "
                    "hint='uv pip install local-embed[gguf]'",
                    spec.name,
                )
                self._failed.add(spec.name)
                return

            gguf_path = spec.gguf_path
            assert gguf_path is not None  # guaranteed by backend == "gguf"
            logger.info("loading_gguf name=%s path=%s dim=%d", spec.name, gguf_path, spec.dim)
            Llama = _Llama  # narrow: non-None after the guard above

            def _load_gguf():
                with _guarded_stdout():
                    return Llama(
                        model_path=gguf_path,
                        embedding=True,
                        n_ctx=8192,
                        verbose=False,
                    )

            client = await run_daemon(
                _load_gguf,
                name=f"gguf-load-{spec.name}",
            )
            actual_dim = self._read_model_dim(client)
            self._clients[spec.name] = client
            if actual_dim and actual_dim != spec.dim:
                logger.info(
                    "dim_override name=%s backend=gguf guessed=%d actual=%d",
                    spec.name, spec.dim, actual_dim,
                )
                spec.dim = actual_dim
                spec.dim_known = True
            self._dims[spec.name] = actual_dim or spec.dim
            logger.info("gguf_loaded name=%s model=%s dim=%d", spec.name, spec.model, spec.dim)
        else:
            _resolve_backend("transformer")
            if _SentenceTransformer is None:
                logger.warning(
                    "backend_unavailable name=%s backend=transformer "
                    "reason=sentence_transformers_not_installed "
                    "hint='uv pip install local-embed[transformer]'",
                    spec.name,
                )
                self._failed.add(spec.name)
                return

            logger.info(
                "loading_transformer name=%s model=%s device=%s",
                spec.name, spec.model, spec.device or "auto",
            )
            device = spec.device or None  # None = auto-detect
            SentenceTransformer = _SentenceTransformer  # narrow: non-None

            def _load_transformer():
                with _guarded_stdout():
                    return SentenceTransformer(spec.model, device=device)

            client = await run_daemon(
                _load_transformer,
                name=f"transformer-load-{spec.name}",
            )
            actual_dim = client.get_sentence_embedding_dimension()
            self._clients[spec.name] = client
            if actual_dim and actual_dim != spec.dim:
                logger.info(
                    "dim_override name=%s backend=transformer guessed=%d actual=%d",
                    spec.name, spec.dim, actual_dim,
                )
                spec.dim = actual_dim
                spec.dim_known = True
            self._dims[spec.name] = actual_dim or spec.dim
            logger.info(
                "transformer_loaded name=%s model=%s dim=%d",
                spec.name, spec.model, spec.dim,
            )

    async def _encode_gguf(self, client: Any, texts: list[str]) -> list[list[float]]:
        """Embed via llama-cpp; runs on a daemon thread, serialised."""

        def _encode() -> list[list[float]]:
            out: list[list[float]] = []
            for text in texts:
                with self._encode_lock:
                    result = client.create_embedding(text)
                out.append(result["data"][0]["embedding"])
            return out

        return await run_daemon(_encode, name="gguf-embed")

    async def _encode_transformer(self, client: Any, texts: list[str]) -> list[list[float]]:
        """Embed via sentence-transformers; daemon thread, serialised."""

        def _encode() -> Any:
            with self._encode_lock:
                return client.encode(
                    texts,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )

        embeddings = await run_daemon(_encode, name="transformer-encode")
        return [emb.tolist() for emb in embeddings]


#: Register the lazy backend imports once module load completes.  Module
#: import stays fast (no torch/llama_cpp), and the import of an optional
#: backend is deferred until that backend's model is actually loaded.
_init_backend_imports()
