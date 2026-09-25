"""Host-side semantic search for the unified tool catalog (tools.db).

The memdb ``SemanticManager`` (gate + embedder + event-driven drainer) is
the one implementation used by every semantic index in slife; this module
adapts it to the host catalog's store (``CatalogStore``'s drainer
contract + ``meta`` table) and builds its embedder from the HOST's active
embedding endpoint (``get_active_endpoint`` — the top-level ``embeddings``
section of slife.yaml), the way memdb/memfiles do in their own processes.

Exactly ONE process runs the manager: it is the single embedding
maintainer.  Every OTHER process may still query the index, because the
vectors (``tool_embeddings``) and the index's published state (the
``semantic_state`` meta row) both live in this shared db — so a subagent
worker, which runs no drainer, gets a :class:`SemanticReader` over what its
parent embedded.  Owning the index and being able to search it are
different grants; conflating them is what made every subagent's
``tool_search`` keyword-only.

``EmbeddingClient`` is the api-only OpenAI-compatible client that formerly
lived in the mcp plugin — moved here verbatim (httpx2).
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx2

from slife.config import EmbeddingsConfig, _resolve_secret
from slife.env import is_env_ref
from slife.plugins.memdb.embeddings import (  # shared model knowledge
    _guess_dim,
    _guess_max_tokens,
    _known_model,
)
from slife.plugins.memdb.semantic import SemanticManager as _BaseSemanticManager
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

logger = logging.getLogger(__name__)

#: The ``meta`` key the drainer publishes its state under.  The tool-catalog
#: index is SHARED (its vectors live in this same db), so its state has to be
#: readable by a process that runs no drainer — a subagent worker's
#: ``system_health`` asks the db, not a manager object it does not have.
SEMANTIC_STATE_KEY = "semantic_state"


async def read_published_state(store) -> dict:
    """The shared index's state as its owner published it, or ``{"state": "unknown"}``.

    The ONE parser of :data:`SEMANTIC_STATE_KEY` — read by the harness's
    ``__check`` (``slife/mcp/host_server.py``) and by the query-side
    :class:`SemanticReader`, which must agree about whether the index is
    usable.  "unknown" is a producer-side value, not a missing key: it says no
    drainer has ever published here, so a reader never has to guess between
    "no drainer has run" and "this block predates the vocabulary".
    """
    try:
        raw = await store.get_meta(SEMANTIC_STATE_KEY)
    except Exception:
        return {"state": "unknown"}
    if not raw:
        return {"state": "unknown"}
    try:
        published = json.loads(raw)
    except ValueError:
        return {"state": "unknown"}
    return published if isinstance(published, dict) else {"state": "unknown"}


class EmbeddingClient:
    """OpenAI-compatible embeddings client (api backend only).

    Config comes from the host's active endpoint (``get_active_endpoint()``
    — the top-level ``embeddings`` section of slife.yaml).  A usable
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
        # In-flight load future — concurrent load() calls share one probe
        # instead of each discovering the model, and (the part that matters) a
        # caller never reads a half-built client as "unavailable".  Same shape
        # as the memdb client's, which its gate calls from every search.
        self._loading: asyncio.Future | None = None
        self._transport = transport  # test hook (httpx2.MockTransport)

    # ── Construction from the host's active endpoint ───────────────

    @classmethod
    def from_endpoint(cls, ep: dict | None) -> "EmbeddingClient":
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
        # The width follows the same two-step rule as the memdb client: a
        # KNOWN model brings its width with it (bge-m3 -> 1024, no probe
        # needed), and an unknown one stays provisional (`dim_known=False`) so
        # `load()` probes the endpoint for the real number.
        #
        # This call used to pass `dim_known=bool(model)` and no `dim` at all —
        # which broke BOTH halves: a known model's width was never consulted
        # (the catalog reported `dim=0` for the model memdb reported as 1024),
        # and an unknown one was never probed, because claiming to know is
        # exactly what makes `load()` skip `_probe_api_dim`.
        return cls(
            model=model, api_key=api_key, base_url=base_url,
            dim=_guess_dim(model),
            dim_known=_known_model(model) is not None,
            enabled=enabled,
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
        """Release the HTTP client — closing the pool, not waiting for the GC.

        Called by ``SemanticManager`` where the embedder is swapped out (an
        enable / ``reload()``) or dropped (a ``disable()``); a later request
        rebuilds the client lazily.
        """
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
        """Pin the model + real dimension from the endpoint. Returns True when ready.

        Concurrent calls share ONE materialisation: the second caller awaits the
        first's in-flight load rather than starting a second probe, and — the
        part that matters — rather than looking at a not-yet-loaded client and
        concluding "unavailable".  A batch of parallel searches is the normal
        case here (a worker's first turn fans out several ``tool_search``
        calls), and every one of them must wait for the same answer instead of
        the losers of the race silently degrading to keyword-only.  Same shape
        as the memdb client's ``load``, which the semantic gate calls from every
        search and check for the same reason.

        An endpoint that cannot be read — unreachable, or listing nothing —
        reports ``False``: the model and the vector width are the ENDPOINT's
        facts, and a client that claims to be loaded without them is a client
        whose every embed fails, so the caller must degrade to keyword search
        instead of opening a gate on it.
        """
        if not self.available:
            return False
        if self._loaded:
            return True
        if self._loading is not None:
            return await self._loading  # share the in-flight load
        self._loading = asyncio.get_running_loop().create_future()
        ok = False
        try:
            if not await self._discover_model():
                # The endpoint could not be read (unreachable, or it listed
                # nothing): there is no model to embed with and no known
                # width, so reporting "loaded" here handed the gate an
                # embedder that fails on every batch.  The memdb client
                # returns False in exactly this case, with this same line, and
                # the semantic gate reads that verdict the same way — degrade
                # to keyword search instead of opening the gate.
                logger.warning(
                    "embedding_api_unavailable model=%s base_url=%s "
                    "(backend not ready — keyword search only)",
                    self._model, self._base_url,
                )
            else:
                # Probe whenever the width is still unknown — the listed model
                # that carries no ``dimension`` key included, not only the
                # configured-model-not-listed case: a wrong width silently
                # drops every vec0 insert of a different size.
                if not self._dim_known:
                    await self._probe_api_dim()
                self._loaded = True
                ok = True
                logger.info(
                    "embedding_loaded backend=api model=%s dim=%d base_url=%s",
                    self._model, self._dim, self._base_url,
                )
        except Exception as e:
            logger.warning("embedding_load_failed err=%s", e)
        finally:
            # Always resolve the shared future and clear the slot.  A
            # CancelledError (a BaseException, not caught above) must not leave
            # ``_loading`` pointing at a future that never resolves — every
            # later load() would then hang forever.
            if not self._loading.done():
                self._loading.set_result(ok)
            self._loading = None
        return ok

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

    The endpoint comes from the process's own config (``EmbeddingsConfig``),
    not from a fresh read of the yaml: the same section feeds the query-side
    reader in the processes that hold no drainer, and one source cannot
    disagree with itself.  It also makes ``--config other.yaml`` mean the same
    thing here as everywhere else in the process.
    """

    def __init__(self, store, embeddings_config: EmbeddingsConfig):
        super().__init__(store, config_path=None)
        self._embeddings_config = embeddings_config

    # ── hook overrides ──────────────────────────────────────────────

    async def _publish_state(self) -> None:
        """Publish the shared index's state into the catalog's ``meta`` table.

        The state lives in THIS process's manager, but the index it describes
        (``tool_embeddings``, in this same db) is read by every process.  A
        subagent runs no drainer, so before this it could report nothing but
        the pending count — a degraded index read as "maintained by the main
        process" in a worker while the main agent reported the real failure.

        One row, rewritten by every transition (``_set_state`` is the only
        writer of the state, so it is also the only publisher).  A reader that
        finds no row is a reader in a process where no drainer has ever run —
        which is a fact of its own, not a state to guess at.
        """
        await self._store.set_meta(
            SEMANTIC_STATE_KEY,
            json.dumps(self.semantic_facts(), ensure_ascii=False),
        )

    def _new_embedder(self):
        return EmbeddingClient.from_endpoint(
            self._embeddings_config.active_endpoint(),
        )

    def _start_enabled(self) -> bool:
        return bool(self._embeddings_config.enabled)

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
                "section to slife.yaml to enable semantic tool search"
            )
        return (
            "api backend unavailable — base_url is a placeholder or unreachable. "
            "Check the 'embeddings' section in slife.yaml."
        )

    async def reload(self, embeddings_config: EmbeddingsConfig) -> dict:
        """Re-point at a freshly written ``embeddings`` section and rebuild.

        The ``embeddings_*`` tools rewrite slife.yaml and hot-reload every
        index; this process holds its config as a snapshot (a worker holds only
        the snapshot), so the new section is handed over here instead of being
        re-read from a file — reading it again would also resolve a DIFFERENT
        path than the tool wrote when the agent runs under ``--config``.

        ``enable()`` is a full rebuild: it stops the drainer, builds the
        embedder from the new section, runs ``_on_model_selected`` (which drops
        the vectors of a model that is no longer active) and drains again.  A
        section with no usable endpoint leaves the index ``disabled`` with the
        reason, which is what the query surfaces then report.
        """
        self._embeddings_config = embeddings_config
        return await self.enable()

    # ── query surface (shared with SemanticReader) ──────────────────

    async def query_ready(self) -> bool:
        """Whether a query can be embedded against the live index.

        The drainer's own gate (an index that is still filling answers
        "not yet", so a search never returns a partial index as if it were
        complete) plus a usable embedder.  See :class:`SemanticReader` for the
        drainer-less half of this contract.
        """
        e = self._embedder
        return bool(self._semantic_ready and e is not None and e.available)

    async def embed_query(self, text: str) -> list[float] | None:
        """Embed a search query with the live embedder (None when unusable)."""
        e = self._embedder
        if e is None or not e.available:
            return None
        return await e.embed_one(text)


class SemanticReader:
    """Query-side semantic search over the shared tool index — any process.

    The vectors and the index's state live in ``tools.db``, so a process that
    runs no drainer (a subagent worker) can still search what the index's
    owner embedded.  Before this, a worker got nothing at all: it held no
    manager, so ``tool_search`` fell back to keywords for its whole life —
    in the one process whose context is smallest and whose need for
    meaning-based lookup is largest.

    A reader owns nothing and writes nothing.  Its readiness is deliberately
    not "the endpoint answers": it also asks the index's OWNER (through the
    published state) whether the index is complete and which model built it,
    so a reader cannot return a partial index as a result set, nor compare a
    query vector against vectors from a different vector space.  That is the
    same answer the owner's own ``tool_search`` gives, which is the point —
    the index is one shared fact, not a per-process opinion.

    The embedder is built from the config this process inherited and loaded
    lazily, once: a worker that never searches never talks to the endpoint,
    and a dead endpoint costs one bounded probe rather than a stall per call.
    """

    def __init__(
        self,
        store,
        embeddings_config: EmbeddingsConfig,
        transport: httpx2.AsyncBaseTransport | None = None,  # test hook
    ):
        self._store = store
        #: ``embeddings: {enabled: false}`` is the user switching semantic
        #: search off; a reader may no more ignore it than the drainer may.
        self._enabled = bool(embeddings_config.enabled)
        self._embedder = EmbeddingClient.from_endpoint(
            embeddings_config.active_endpoint(),
        )
        if transport is not None:
            self._embedder._transport = transport  # test hook, see EmbeddingClient
        #: A load that finished and FAILED — remembered so a long-lived worker
        #: never probes a dead endpoint once per search.
        self._load_failed = False
        self._reason = ""

    @property
    def reason(self) -> str:
        """Why the last :meth:`query_ready` said no (empty when it said yes)."""
        return self._reason

    async def warmup(self) -> bool:
        """Bring the reader up before the first query needs it.

        The capability a worker inherits should be READY when it starts work,
        not initialized by the first task that needs it: a lazily-built
        semantic client means the first turn runs while it is still coming up,
        and anything sharing that turn is served by a half-built state.  The
        parent has no such window — its embedder is loaded at boot by the
        drainer — so a worker that warms at boot behaves like the process it
        forked from.

        Never raises, and never blocks the caller: the boot path starts it as a
        background task, so a slow or dead endpoint costs the worker nothing at
        spawn.  A failure here is simply recorded as the reason a later query
        will report.
        """
        try:
            return await self._ensure_embedder()
        except Exception as exc:  # a boot task must not surface as an error
            logger.warning("semantic_reader_warmup_failed err=%s", exc)
            return False

    async def query_ready(self) -> bool:
        """Whether a query can be embedded and compared against this index.

        Answered from the endpoint's own state plus the index's published
        facts, in that order — the cheap, process-local check first.
        """
        if not await self._ensure_embedder():
            return False
        state = await read_published_state(self._store)
        if not state.get("semantic_ready"):
            if state.get("state") == "unknown":
                self._degrade(
                    "the shared tool index has no published state — no drainer "
                    "has run in this database; keyword only.",
                )
            else:
                self._degrade(
                    state.get("reason")
                    or "semantic search unavailable — keyword only.",
                )
            return False
        # The index's owner embedded with ITS endpoint.  A reader whose config
        # names a different model would compare query vectors against another
        # vector space — width may match and the numbers would still be
        # meaningless, so identity is the check, not the width.
        published_model = state.get("model", "")
        if published_model and published_model != self._embedder.model:
            self._degrade(
                f"the shared tool index was built with a different embedding "
                f"model ({published_model}, not {self._embedder.model}) — "
                "keyword only until this process is restarted with the current "
                "config.",
            )
            return False
        self._reason = ""
        return True

    async def embed_query(self, text: str) -> list[float] | None:
        """Embed a search query (None when the endpoint cannot answer)."""
        if not await self._ensure_embedder():
            return None
        return await self._embedder.embed_one(text)

    async def _ensure_embedder(self) -> bool:
        """Load the embedder, sharing the work with anyone else asking now.

        Concurrent callers must not disagree.  A batch of parallel searches is
        the ordinary case (a worker's first turn fans out several
        ``tool_search`` calls), and only the caller that WINS the race may
        report anything about the endpoint: the others await the same load.  A
        loader that publishes "attempted, not loaded yet" instead lets every
        loser of the race read a half-built client as "unavailable" and degrade
        to keyword-only — silently, since nothing was even tried.  That was the
        behaviour here, and it made the first turn of every worker return a mix
        of semantic and keyword-only results for the SAME query.

        A load that has FINISHED and failed is remembered (``_load_failed``):
        a worker is long-lived, so retrying a dead endpoint on every search
        would stall each one for the probe budget.  That is also why the flag
        is set after the await rather than before it — "not yet" and "no" are
        different answers, and only "no" is allowed to degrade.

        The probe comes first, and it is the one that decides.  ``load()``
        alone is a shallow success for a model the built-in table knows: the
        width is taken from that table, so it returns True without having
        reached the endpoint at all.  The drainer tolerates that (an endpoint
        that only fails at embed time leaves the index undrained, so its gate
        never opens), but a reader has no such backstop — it would claim ready
        and then answer every query with None, which is the silent degradation
        this whole surface exists to avoid.
        """
        if not self._enabled:
            self._degrade(
                "semantic search is switched off in the config "
                "(embeddings.enabled=false) — keyword only.",
            )
            return False
        e = self._embedder
        if not e.available:
            self._degrade(
                "no embedding endpoint configured — add an 'embeddings' "
                "section to slife.yaml to enable semantic tool search",
            )
            return False
        if e.loaded:
            return True
        if self._load_failed:
            return False            # a finished failure — do not probe again
        try:
            # Bounded at the endpoint-readiness budget: this is a "is it
            # there" question, not a bulk embed.
            ok = await asyncio.wait_for(
                self._probe_then_load(),
                timeout=_timeouts.timeouts.ready.probe_endpoint,
            )
        except Exception as exc:  # timeout, transport, bad payload
            logger.warning("semantic_reader_load_failed err=%s", exc)
            ok = False
        if not ok:
            self._load_failed = True
            self._degrade(
                f"embedding endpoint did not answer ({e.base_url}) — "
                "keyword only.",
            )
            return False
        return True

    def _degrade(self, reason: str) -> None:
        """Record why this process cannot search — and say it ONCE.

        The degradation was previously invisible: no warning, no log line, not
        even the semantic leg's own debug record, so a worker answering "no
        results" for a Chinese query (keyword search has no word segmentation,
        so a degraded query can match nothing at all) looked like an empty
        index rather than a capability that had not come up yet.  Announced on
        the TRANSITION, the way the index's owner announces its own state — one
        line per change, not one per query.
        """
        if reason == self._reason:
            return
        self._reason = reason
        logger.warning("semantic_query_degraded reason=%s", reason)

    async def _probe_then_load(self) -> bool:
        """Ask whether the endpoint is there, then pin the model and width."""
        if not await self._embedder.probe_available():
            return False
        return await self._embedder.load()


__all__ = [
    "EmbeddingClient",
    "SEMANTIC_STATE_KEY",
    "SemanticManager",
    "SemanticReader",
    "read_published_state",
]