"""local-embed FastMCP plugin server — MCP tools + OpenAI-compatible /v1/embeddings.

One process, one port, two protocols (the ``sharefile`` precedent):

- **MCP** on ``/mcp`` — the plugin contract.  Like the sharefile plugin,
  this is a *service provider*, not a tool provider: the only MCP tool is
  the internal ``__embed_status`` (probed by the host's ``check_local_embed``);
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

import json
import logging
import os

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from local_embed.config import DEFAULT_PORT
from local_embed.engine import Engine
from local_embed.logging import silence_noisy_loggers, setup_logging
from local_embed.server_utils import create_plugin_server, warm_after_handshake

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
        "memdb + memfiles).  The only MCP tool is the internal __embed_status, "
        "probed by the host's check_local_embed — the host consumes the model "
        "service over HTTP, never through MCP tools."
    ),
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
        "available": spec.runtime_available() and name not in engine._failed,
        "max_tokens": spec.max_tokens,
    }


@mcp.tool(name="__embed_status", description="Embedding service status: active model, model list, dimensions, loaded. Internal — called by the host's check_local_embed.")
async def __embed_status() -> str:
    """Return the current engine status as a JSON string.

    Internal (``__`` prefix): status probing is the harness's job via
    ``check_local_embed`` — never exposed to the LLM, which has the
    ``embeddings_*`` native tools for config and the health checks for
    status.
    """
    engine = get_engine()
    return json.dumps(
        {
            "active_model": engine.active_model,
            "models": [_model_status(engine, n) for n in engine.models],
        },
        ensure_ascii=False,
    )


# ═══════════════════════════════════════════════════════════════════════
# OpenAI-compatible HTTP routes on the same port
# ═══════════════════════════════════════════════════════════════════════


def _parse_embedding_input(body: dict) -> "list[str] | None":
    """Extract a list of input texts from a request body.

    OpenAI accepts ``input`` as a string or a list of strings.  Returns
    None when the body is missing or has an unsupported shape (caller
    responds 422).
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

    Body: ``{"input": str | [str], "model": str}`` — ``model`` may name any
    configured model (defaults to the active one).  Response is the
    standard shape::

        {"object": "list", "data": [{"object": "embedding", "index": 0,
                                     "embedding": [0.1, …]}], "model": …,
         "usage": {"prompt_tokens": n, "total_tokens": n}}
    """
    engine = get_engine()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "invalid JSON body", "type": "invalid_request_error"}}, status_code=400)

    texts = _parse_embedding_input(body)
    if texts is None:
        return JSONResponse(
            {"error": {"message": "input must be a string or a list of strings", "type": "invalid_request_error"}},
            status_code=422,
        )

    model = body.get("model") or ""
    try:
        vecs = await engine.embed(texts, model=model or None)
    except KeyError as e:
        return JSONResponse(
            {"error": {"message": str(e), "type": "invalid_request_error"}},
            status_code=404,
        )
    except Exception as e:
        logger.warning("embeddings_failed err=%s", e)
        return JSONResponse(
            {"error": {"message": str(e), "type": "server_error"}},
            status_code=503,
        )

    data = [
        {"object": "embedding", "index": i, "embedding": vec}
        for i, vec in enumerate(vecs)
    ]
    prompt_tokens = sum(len(t) // 4 or 1 for t in texts)  # crude estimate
    return JSONResponse(
        {
            "object": "list",
            "data": data,
            "model": model or engine.model,
            "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
        }
    )


def _model_entry(engine: Engine, name: str) -> dict:
    """One OpenAI-shaped model entry (shared by the list + retrieve routes)."""
    spec = engine.model_spec(name)
    return {
        "id": name,
        "object": "model",
        "created": 0,
        "owned_by": "local-embed",
        "active": name == engine.active_model,
        "backend": spec.backend,
        "dimension": spec.dim,
        "dimension_known": spec.dim_known,
        "loaded": engine.is_loaded(name),
        "available": spec.runtime_available() and name not in engine._failed,
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
        return JSONResponse(
            {
                "error": {
                    "message": f"The model '{name}' does not exist.",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "model_not_found",
                }
            },
            status_code=404,
        )
    return JSONResponse(_model_entry(engine, name))


@mcp.custom_route("/v1/models/{name}/activate", methods=["POST"])
async def v1_models_activate(request: Request) -> Response:
    """Switch the active model (loads it on demand)."""
    engine = get_engine()
    name = request.path_params["name"]
    try:
        await engine.set_active(name)
    except KeyError as e:
        return JSONResponse(
            {"error": {"message": str(e), "type": "invalid_request_error"}},
            status_code=404,
        )
    except Exception as e:
        logger.warning("activate_failed name=%s err=%s", name, e)
        return JSONResponse(
            {"error": {"message": str(e), "type": "server_error"}},
            status_code=503,
        )
    return JSONResponse({"active_model": engine.active_model})


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    """Liveness + engine state."""
    engine = get_engine()
    return JSONResponse(
        {
            "status": "ok" if engine.available else "degraded",
            "active_model": engine.active_model,
            "backend": engine.backend,
            "model": engine.model,
            "dimension": engine.dimension,
            "dimension_known": engine.dimension_known,
            "loaded": engine.loaded,
            "models": [
                {"name": n, "loaded": engine.is_loaded(n)}
                for n in engine.models
            ],
        }
    )


# ═══════════════════════════════════════════════════════════════════════
# Entry points
# ═══════════════════════════════════════════════════════════════════════


def build_server(engine: Engine) -> FastMCP:
    """Return the FastMCP server wired to *engine* (module singleton shared).

    The active model is loaded in the BACKGROUND after the MCP handshake
    (``warm_after_handshake``), never in the lifespan — so startup is
    handshake-fast even when the active model's backend is heavy (a
    transformer model imports torch/transformers and can take seconds).
    The first embed blocks on the load if it hasn't finished yet; readiness
    is never gated on it.
    """
    set_engine(engine)

    async def _warmup() -> None:
        try:
            dim = await engine.ensure_loaded()
            logger.info("active_model_warmed name=%s dim=%d", engine.active_model, dim)
        except Exception as e:
            logger.warning("active_model_warm_failed err=%s", e)

    warm_after_handshake(mcp, _warmup, delay=0.1, name="local-embed-warmup")
    return mcp


def _run(mcp_server: FastMCP, *, host: str, port: int) -> int:
    """Serve the FastMCP server on Streamable HTTP; block until shutdown."""
    try:
        mcp_server.run(
            transport="streamable-http",
            host=host,
            port=port,
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
    """Run as a standalone service on an explicit host:port (CLI path)."""
    build_server(engine)
    logger.info("serve_standalone host=%s port=%s backend=%s", host, port, engine.backend)
    return _run(mcp, host=host, port=port)


def main() -> int:
    """Plugin spawn target — ``python -m local_embed.server``.

    Reads ``local_embed.json5`` (env var ``$LOCAL_EMBED_FILE``, else the
    usual precedence) plus ``LOCAL_EMBED_*`` env overrides, builds the
    multi-model engine, binds the configured port (default {DEFAULT_PORT} — a
    STABLE port so a host can point its OpenAI-compatible client's
    ``base_url`` at it), serves MCP + embeddings on it, and blocks until
    shutdown.  The stdout port signal is still emitted for hosts that
    discover the port.
    """
    from local_embed.config import resolve_engine_settings
    from local_embed.server_utils import bind_port, run_plugin_server

    settings = resolve_engine_settings()

    engine = Engine(specs=settings["specs"], active=settings["active"])
    build_server(engine)

    # Bind the configured port (default {DEFAULT_PORT} — a STABLE port so a
    # host can point its OpenAI-compatible client's base_url at it).  Falls
    # back to a free port when taken; the stdout signal reports the actual
    # port either way.
    sock, port = bind_port(settings["host"], int(settings["port"]))
    logger.info(
        "local_embed_start port=%s active=%s models=%s",
        port, engine.active_model, engine.models,
    )
    return run_plugin_server(mcp, sockets=[sock])


if __name__ == "__main__":
    import sys

    sys.exit(main())
