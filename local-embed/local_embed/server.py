"""local-embed FastMCP plugin server — MCP tools + OpenAI-compatible /v1/embeddings.

One process, one port, two protocols (the ``sharefile`` precedent):

- **MCP** on ``/mcp`` — the plugin contract.  Like the sharefile plugin,
  this is a *service provider*, not a tool provider: the only MCP tool is
  the internal ``__check`` (probed by the host's ``system_health``);
  the host consumes the service over the OpenAI-compatible HTTP routes,
  never through MCP tools.
- **OpenAI-compatible HTTP** on the SAME port via ``@mcp.custom_route``:
  ``POST /v1/embeddings`` (the standard shape: ``{input, model}`` →
  ``{data: [{embedding, index, object}], model, usage}``), ``GET /v1/models``
  and ``GET /health``.  slife's memdb/memfiles ``EmbeddingClient`` (api
  backend) talks to ``http://127.0.0.1:{port}/v1/embeddings`` with a normal
  OpenAI client — no new protocol.

The model itself lives in :class:`local_embed.engine.Engine` and is loaded
lazily on the first embed (never at import / lifespan, so startup is
handshake-fast per the plugin contract; a slow or failed load stays a
request-time error, never a readiness gate).

Spawned by a host via ``python -m local_embed.server``; also serves as the
standalone ``local-embed`` server behind the CLI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from local_embed.config import DEFAULT_PORT
from local_embed.engine import EmbeddingInputTooLong, Engine
from local_embed.logging import silence_noisy_loggers, setup_logging
from local_embed.server_utils import bind_port, create_plugin_server

logger = logging.getLogger(__name__)

setup_logging(
    service_name=os.environ.get("SLIFE_PLUGIN_NAME", "local-embed"),
)
silence_noisy_loggers()


#: The engine is injected by the entry points (``serve_standalone`` /
#: ``main``); kept as a module global so the FastMCP tool handlers and
#: custom routes share one instance.
_engine: Engine | None = None


def set_engine(engine: Engine) -> None:
    """Set the shared engine instance (called once by the entry points)."""
    global _engine
    _engine = engine


def get_engine() -> Engine:
    """Return the shared engine; raises if not configured yet."""
    if _engine is None:
        raise RuntimeError("local-embed engine not configured")
    return _engine


async def _eager_load_autoload() -> None:
    """Background eager-load of the models flagged ``autoload: true``.

    Per-model eager loading: a model whose config entry has ``autoload:
    true`` is loaded in the background shortly after the server starts, so
    the first embed on it is already warm — every unflagged model stays lazy
    (a local model's weights are large and memory-hungry).
    """
    engine = _engine
    if engine is None:
        return  # never armed (no build_server) — nothing to preload
    try:
        await engine.load_autoload()
    except Exception as e:  # noqa: BLE001 — a failed pass must never kill the server
        logger.warning("autoload_failed models=%s err=%s", engine.models, e)


@asynccontextmanager
async def _startup_eager(app):
    """FastMCP lifespan: schedule the per-model eager load as a background task.

    Loading is lazy by default; only models whose spec has ``autoload: true``
    are preloaded.  The task is created but NOT awaited, so the lifespan
    completes immediately — the port signal stays handshake-fast and the
    loads themselves run on daemon threads (``engine.load_autoload`` →
    ``run_daemon``); a heavy model simply warms up in the background while
    the server already serves.
    """
    # Always schedule it; ``Engine.load_autoload`` skips every model whose
    # spec is not flagged, so this is a no-op when nothing sets autoload.
    asyncio.get_running_loop().create_task(_eager_load_autoload())
    yield


# The server is built through ``create_plugin_server`` so the port signal
# (``{"port": N}`` on stdout) actually fires: that helper wraps the FastMCP
# lifespan so the ready callback runs once the app is serving, and the host
# (``MCPWrapperProcess``) reads stdout to discover the port.  A plain
# ``FastMCP(...)`` here never fires ``signal_port`` — the host would time out
# on the port signal and kill the child ("plugin failed to start") even
# though the server is healthy.
mcp, _ = create_plugin_server(
    "local-embed",
    instructions=(
        "local-embed — local embedding service.  Serves OpenAI-compatible "
        "/v1/embeddings + /v1/models for slife's embeddings config (shared by "
        "memdb + memfiles).  The only MCP tool is the internal __check, "
        "probed by the host's check_local_embed — the host consumes the model "
        "service over HTTP, never through MCP tools."
    ),
    lifespan=_startup_eager,
)


# ═══════════════════════════════════════════════════════════════════════
# MCP tools (LLM-visible via the host's `<name>__*` prefix)
# ═══════════════════════════════════════════════════════════════════════


def _model_status(engine: Engine, name: str) -> dict:
    """One model's status dict (spec + load state)."""
    spec = engine.model_spec(name)
    return {
        "name": name,
        "backend": spec.backend,
        "model": spec.model,
        "dimension": spec.dim,
        "dimension_known": spec.dim_known,
        "loaded": engine.is_loaded(name),
        "available": engine.available_for(name),
        "max_tokens": spec.max_tokens,
    }


@mcp.tool(name="__check", description="Embedding service status as JSON: model list, dimensions, loaded. Internal — probed by the host's system_health.")
async def __check() -> str:
    """Return the current engine status as a JSON string.

    Internal (``__`` prefix): status probing is the harness's job via
    ``system_health`` — never exposed to the LLM, which has the
    ``embeddings_*`` native tools for config and the health checks for
    status.
    """
    engine = get_engine()
    return json.dumps(
        {"models": [_model_status(engine, n) for n in engine.models]},
        ensure_ascii=False,
    )


# ═══════════════════════════════════════════════════════════════════════
# OpenAI-compatible HTTP routes on the same port
# ═══════════════════════════════════════════════════════════════════════


def _error(
    message: str,
    *,
    type: str = "invalid_request_error",
    param: "str | None" = None,
    code: "str | None" = None,
    status: int = 400,
) -> JSONResponse:
    """One OpenAI-standard error envelope — every field always present.

    The cloud API's error body always carries ``message`` / ``type`` /
    ``param`` / ``code`` (``param`` and ``code`` are null when not
    applicable).  Status codes follow the OpenAI contract: parameter /
    validation / context-length errors → 400, unknown model → 404, engine
    unavailable → 503, unexpected internal failure → 500.
    """
    return JSONResponse(
        {"error": {"message": message, "type": type, "param": param, "code": code}},
        status_code=status,
    )


def _parse_embedding_input(body: dict) -> "list[str] | None":
    """Extract a list of input texts from a request body.

    OpenAI accepts ``input`` as a string or a list of strings.  Returns
    None when the body is missing or has an unsupported shape (caller
    responds 400 ``invalid_request_error``, param ``input``).
    """
    raw = body.get("input")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        return raw
    return None


@mcp.custom_route("/v1/embeddings", methods=["POST"])
async def v1_embeddings(request: Request) -> Response:
    """OpenAI-compatible embeddings endpoint.

    Body: ``{"input": str | [str], "model": str}`` — ``model`` is required
    and names any configured model, exactly like the cloud API.  Errors
    follow the OpenAI contract::

        - 400 invalid_request_error  — missing/invalid ``model`` or ``input``,
                                        input exceeds the model's context length
        - 404 invalid_request_error  — unknown ``model`` (code model_not_found)
        - 503 server_error           — the model's engine is unavailable
                                        (backend dependency missing / load failed)
        - 500 server_error           — unexpected internal failure

    Response is the standard shape::

        {"object": "list", "data": [{"object": "embedding", "index": 0,
                                     "embedding": [0.1, …]}], "model": …,
         "usage": {"prompt_tokens": n, "total_tokens": n}}
    """
    engine = get_engine()
    try:
        body = await request.json()
    except Exception:
        return _error("We could not parse the JSON body of your request.")

    texts = _parse_embedding_input(body)
    if texts is None:
        return _error(
            "`input` must be a string or an array of strings.",
            param="input",
        )

    model = body.get("model") or ""
    if not model:
        return _error(
            "You must provide a model parameter.",
            param="model",
        )
    try:
        vecs = await engine.embed(texts, model=model)
    except KeyError:
        return _error(
            f"The model '{model}' does not exist or you do not have access to it.",
            code="model_not_found",
            status=404,
        )
    except EmbeddingInputTooLong as e:
        # Input exceeds the model's token limit — reject like a cloud API
        # (400 invalid_request_error; no silent truncation).
        return _error(str(e), param="input", code="context_length_exceeded")
    except RuntimeError as e:
        # The model's engine is unavailable (backend dependency missing,
        # load failed) — 503, the cloud API's "engine unavailable" code.
        logger.warning("embeddings_unavailable err=%s", e)
        return _error(
            str(e),
            type="server_error",
            status=503,
        )
    except Exception as e:
        # Anything else is our own bug — 500, not 503.
        logger.warning("embeddings_failed err=%s", e)
        return _error(
            "The server had an error while processing your request.",
            type="server_error",
            status=500,
        )

    data = [
        {"object": "embedding", "index": i, "embedding": vec}
        for i, vec in enumerate(vecs)
    ]
    prompt_tokens = sum((len(t) // 4) or 1 for t in texts if t.strip())  # crude estimate
    return JSONResponse(
        {
            "object": "list",
            "data": data,
            "model": model,
            "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
        }
    )


def _model_entry(engine: Engine, name: str) -> dict:
    """One OpenAI-shaped model entry (shared by the list + retrieve routes).

    Standard listing shape (``id``/``object``/``created``/``owned_by``) plus
    local-embed's per-model metadata.  No ``active`` marker — models are
    peers, as on any standard OpenAI backend.
    """
    spec = engine.model_spec(name)
    return {
        "id": name,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "local-embed",
        "backend": spec.backend,
        "model": spec.model,
        "dimension": spec.dim,
        "dimension_known": spec.dim_known,
        "loaded": engine.is_loaded(name),
        "available": engine.available_for(name),
        "max_tokens": spec.max_tokens,
    }


@mcp.custom_route("/v1/models", methods=["GET"])
async def v1_models(request: Request) -> Response:
    """OpenAI-compatible model listing — one entry per configured model."""
    engine = get_engine()
    return JSONResponse(
        {"object": "list", "data": [_model_entry(engine, n) for n in engine.models]}
    )


@mcp.custom_route("/v1/models/{name}", methods=["GET"])
async def v1_models_retrieve(request: Request) -> Response:
    """OpenAI-compatible single-model detail — ``GET /v1/models/{id}``.

    Mirrors the OpenAI Models API ``retrieve`` endpoint; an unknown id
    returns 404 with the standard error envelope.
    """
    engine = get_engine()
    name = request.path_params["name"]
    if name not in engine.models:
        return _error(
            f"The model '{name}' does not exist or you do not have access to it.",
            code="model_not_found",
            status=404,
        )
    return JSONResponse(_model_entry(engine, name))


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    """Liveness + engine state, per model (no active model to report)."""
    engine = get_engine()
    models = [_model_status(engine, n) for n in engine.models]
    return JSONResponse(
        {
            "status": "ok" if any(m["available"] for m in models) else "degraded",
            "models": models,
        }
    )


# ═══════════════════════════════════════════════════════════════════════
# Entry points
# ═══════════════════════════════════════════════════════════════════════


def build_server(engine: Engine) -> FastMCP:
    """Return the FastMCP server wired to *engine* (module singleton shared).

    Loading is lazy by default — a local model's weights are large and
    memory-hungry, so nothing is materialised until the first request names
    that model.  A model whose config entry sets ``autoload: true`` is
    eager-loaded by the ``_startup_eager`` lifespan hook right after the
    server starts (see :func:`_eager_load_autoload`), so its first embed is
    already warm; the lifespan itself never waits on a load, no heavy import
    runs in it, and startup stays handshake-fast even when a backend is
    heavy (a transformer model imports torch/transformers and can take
    seconds).  Backend availability is import-checked cheaply
    (``find_spec``) at construction, so ``/v1/models`` and ``/health``
    report real usability before any model has been loaded; the first embed
    blocks on the load if it hasn't finished yet, and readiness is never
    gated on it.
    """
    set_engine(engine)
    return mcp


def _run(mcp_server: FastMCP, *, host: str, port: int, sockets: list | None = None) -> int:
    """Serve the FastMCP server on Streamable HTTP; block until shutdown."""
    try:
        mcp_server.run(
            transport="streamable-http",
            host=host,
            port=port,
            sockets=sockets,
            show_banner=False,
            json_response=True,
            uvicorn_config={"log_config": None},
        )
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error("server_error err=%s", e)
        return 1
    return 0


def serve_standalone(engine: Engine, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> int:
    """Run as a standalone service on an explicit host:port (CLI path).

    Pre-binds the port with :func:`bind_port` (probe + clean error) so a
    port already in use fails with one actionable line — the same behaviour
    the plugin spawn path gets — instead of a raw uvicorn bind error.
    """
    build_server(engine)
    logger.info(
        "serve_standalone host=%s port=%s models=%s",
        host, port, ",".join(engine.models),
    )
    try:
        sock, _ = bind_port(host, port)
    except RuntimeError as e:
        logger.error("local_embed_bind_failed err=%s", e)
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return _run(mcp, host=host, port=port, sockets=[sock])


def main() -> int:
    """Plugin spawn target — ``python -m local_embed.server``.

    Reads ``local_embed.json5`` (env var ``$LOCAL_EMBED_FILE``, else the
    usual precedence) plus ``LOCAL_EMBED_*`` env overrides, builds the
    multi-model engine, binds the configured port (default {DEFAULT_PORT} — a
    STABLE port so a host can point its OpenAI-compatible client's
    ``base_url`` at it), serves MCP + embeddings on it, and blocks until
    shutdown.  The stdout port signal is still emitted for hosts that
    discover the port.

    local-embed is the only plugin that uses a fixed port (every other
    plugin takes an OS-assigned one), so a port already in use is a hard
    error — no fallback.  A second instance would serve nothing (the host's
    embeddings ``base_url`` is static) while doubling the model in memory;
    the host reports the plugin as not loaded and the existing instance
    keeps serving embeddings on the original port.
    """
    from local_embed.config import resolve_engine_settings
    from local_embed.server_utils import run_plugin_server

    settings = resolve_engine_settings()

    engine = Engine(specs=settings["specs"])
    build_server(engine)

    # Bind the configured port (default {DEFAULT_PORT} — a STABLE port so a
    # host can point its OpenAI-compatible client's base_url at it).  A port
    # already being served is a hard error (no fallback) — see bind_port.
    # Fail CLEANLY (one actionable line, not a raw traceback): the host
    # surfaces this as a plugin load failure (加载失败) and the pre-existing
    # instance keeps serving embeddings.
    try:
        sock, port = bind_port(settings["host"], int(settings["port"]))
    except RuntimeError as e:
        logger.error("local_embed_load_failed err=%s", e)
        print(f"local-embed load failed: {e}", file=sys.stderr)
        return 1
    logger.info(
        "local_embed_start port=%s models=%s autoload=%s",
        port, engine.models,
        [n for n in engine.models if engine.model_spec(n).autoload] or "-",
    )
    return run_plugin_server(mcp, sockets=[sock])


if __name__ == "__main__":
    import sys

    sys.exit(main())
