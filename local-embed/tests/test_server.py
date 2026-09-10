"""Tests for local_embed.server — MCP tools + OpenAI-compatible HTTP routes.

A real Engine with a mocked model backend replaces the real model; the
FastMCP app is exercised via its ASGI transport (``mcp.http_app``) so we
test the actual route handlers end-to-end without spawning a server.
"""

import socket

import pytest

pytestmark = pytest.mark.unit

from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

from local_embed.engine import EmbeddingInputTooLong, Engine, ModelSpec
from local_embed.server import _eager_load_autoload, build_server, mcp, serve_standalone, set_engine
from local_embed.server_utils import bind_port


class _StubEngine(Engine):
    """An Engine subclass whose backend is fully stubbed (no real model).

    Overrides ``available`` to True and ``embed`` to a deterministic stub
    so the HTTP routes can be exercised without llama-cpp.
    """

    def __init__(self, dim: int = 1024, available: bool = True):
        self._dim_val = dim
        self._avail = available
        # Build with a mocked runtime so construction doesn't warn.
        with (
            patch("local_embed.engine._Llama", MagicMock()),
            patch("local_embed.engine.check_backend_runtime", return_value=True),
        ):
            super().__init__(backend="gguf", model="bge-m3", gguf_path="/x.gguf")

    def available_for(self, name):
        return self._avail

    async def embed(self, texts, model):
        if not self._avail:
            raise RuntimeError("embedding backend unavailable")
        if model not in self.models:
            raise KeyError(f"unknown model: {model}")
        return [[0.5] * self._dim_val for _ in texts]


def _make_engine(dim: int = 1024, available: bool = True) -> Engine:
    return _StubEngine(dim=dim, available=available)


@pytest.fixture
def client():
    """A Starlette TestClient against the FastMCP ASGI app (custom routes)."""
    build_server(_make_engine())
    app = mcp.http_app(path="/mcp")
    return TestClient(app)


# ── /v1/embeddings ───────────────────────────────────────────────────────


class TestV1Embeddings:
    def test_single_string(self, client):
        resp = client.post(
            "/v1/embeddings", json={"input": "hello", "model": "bge-m3"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "list"
        assert len(body["data"]) == 1
        assert body["data"][0]["index"] == 0
        assert body["data"][0]["object"] == "embedding"
        assert len(body["data"][0]["embedding"]) == 1024
        assert body["model"] == "bge-m3"

    def test_list_input(self, client):
        resp = client.post(
            "/v1/embeddings", json={"input": ["a", "b"], "model": "bge-m3"}
        )
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 2

    def test_invalid_input_type(self, client):
        resp = client.post("/v1/embeddings", json={"input": 42})
        assert resp.status_code == 422

    def test_invalid_json(self, client):
        resp = client.post("/v1/embeddings", content="{not json")
        assert resp.status_code == 400

    def test_bad_json(self, client):
        resp = client.post("/v1/embeddings", content=b"", headers={"Content-Type": "application/json"})
        assert resp.status_code in (400, 422)

    def test_missing_model_400(self, client):
        """A request without `model` is malformed — 400 invalid_request_error,
        exactly like the cloud API (no server-side default / active model to
        fall back to)."""
        resp = client.post("/v1/embeddings", json={"input": "x"})
        assert resp.status_code == 400
        err = resp.json()["error"]
        assert err["type"] == "invalid_request_error"
        assert "model parameter" in err["message"]

    def test_unknown_model_404(self, client):
        """An unknown `model` is 404 model_not_found — OpenAI's contract."""
        resp = client.post("/v1/embeddings", json={"input": "x", "model": "typo"})
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["type"] == "invalid_request_error"
        assert err["code"] == "model_not_found"
        assert "typo" in err["message"]

    def test_backend_failure_503(self):
        build_server(_make_engine(available=False))
        with TestClient(mcp.http_app(path="/mcp")) as c:
            resp = c.post("/v1/embeddings", json={"input": "x", "model": "bge-m3"})
            assert resp.status_code == 503

    def test_input_too_long_400(self):
        """An over-limit input surfaces as an OpenAI-style 400
        invalid_request_error — never a silent truncation."""

        class _TooLongEngine(_StubEngine):
            async def embed(self, texts, model=None):
                raise EmbeddingInputTooLong(
                    "input 0 contains 9000 tokens, which exceeds the maximum "
                    "context length of 8192 tokens for model 'bge-m3'"
                )

        build_server(_TooLongEngine(dim=1024, available=True))
        with TestClient(mcp.http_app(path="/mcp")) as c:
            resp = c.post("/v1/embeddings", json={"input": "x" * 100, "model": "bge-m3"})
            assert resp.status_code == 400
            err = resp.json()["error"]
            assert err["type"] == "invalid_request_error"
            assert "8192" in err["message"]

    def test_response_echoes_requested_model(self):
        """The response `model` echoes the addressable config key (the id
        /v1/models reports), not the internal repo id — the repo id is not
        an engine key and would 404 if echoed back."""
        spec = ModelSpec("bge-m3-transformer", backend="transformer", model="BAAI/bge-m3")
        engine = Engine(specs=[spec])

        async def _embed(texts, model):
            return [[0.5] * 1024 for _ in texts]

        engine.embed = _embed
        build_server(engine)
        with TestClient(mcp.http_app(path="/mcp")) as c:
            resp = c.post(
                "/v1/embeddings",
                json={"input": "hello", "model": "bge-m3-transformer"},
            )
            assert resp.status_code == 200
            assert resp.json()["model"] == "bge-m3-transformer"
            # the requested key, not the transformer repo id
            assert resp.json()["model"] != "BAAI/bge-m3"


# ── /v1/models ───────────────────────────────────────────────────────────


class TestV1Models:
    def test_model_listing(self, client):
        resp = client.get("/v1/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "list"
        assert len(body["data"]) == 1
        assert body["data"][0]["id"] == "bge-m3"
        assert body["data"][0]["dimension"] == 1024
        assert body["data"][0]["dimension_known"] is True
        assert body["data"][0]["created"] > 0

    def test_model_retrieve(self, client):
        resp = client.get("/v1/models/bge-m3")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "bge-m3"
        assert body["object"] == "model"
        assert body["owned_by"] == "local-embed"
        assert body["dimension"] == 1024
        assert body["dimension_known"] is True
        assert body["created"] > 0

    def test_model_retrieve_unknown(self, client):
        resp = client.get("/v1/models/nope")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["type"] == "invalid_request_error"
        assert "nope" in body["error"]["message"]


# ── /health ──────────────────────────────────────────────────────────────


class TestHealth:
    def test_health_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["models"][0]["name"] == "bge-m3"
        assert body["models"][0]["dimension"] == 1024

    def test_health_degraded(self):
        build_server(_make_engine(available=False))
        with TestClient(mcp.http_app(path="/mcp")) as c:
            resp = c.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "degraded"


class TestAutoload:
    @pytest.mark.asyncio
    async def test_eager_runner_delegates_to_engine(self):
        """The startup eager runner loads exactly the autoload-flagged models
        via Engine.load_autoload (per-model eager loading)."""
        engine = _make_engine()
        set_engine(engine)
        with patch.object(engine, "load_autoload", new=AsyncMock()) as m:
            await _eager_load_autoload()
        m.assert_awaited_once()


class TestBindPort:
    """bind_port fails loudly when the fixed port is already served.

    local-embed is the only plugin on a fixed port; a second instance must
    not shadow the first (its host-side base_url is static), so a live
    listener on the port is a hard error — no fallback.
    """

    def test_served_port_raises_no_fallback(self):
        # Reserve a live listener, then try to bind the same port.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen()
        try:
            with pytest.raises(RuntimeError, match="already being served"):
                bind_port("127.0.0.1", port)
        finally:
            s.close()

    def test_free_port_binds(self):
        sock, _ = bind_port("127.0.0.1", 0)
        try:
            assert sock.getsockname()[1] != 0  # really bound to a real port
        finally:
            sock.close()

    def test_serve_standalone_port_conflict_clean(self, capsys):
        """A taken port in standalone mode returns 1 with one actionable
        line, not a raw uvicorn traceback (mirrors the plugin spawn path)."""
        with patch(
            "local_embed.server.bind_port",
            side_effect=RuntimeError("cannot bind 127.0.0.1:17347 — already in use"),
        ):
            code = serve_standalone(_make_engine())
        assert code == 1
        assert "already in use" in capsys.readouterr().err
