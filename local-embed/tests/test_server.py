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
from local_embed.server import build_server, mcp, serve_standalone
from local_embed.server_utils import bind_port


class _StubEngine(Engine):
    """An Engine subclass whose backend is fully stubbed (no real model).

    Overrides ``available`` to True and ``embed`` to a deterministic stub
    so the HTTP routes can be exercised without llama-cpp.
    """

    def __init__(self, dim: int = 1024, available: bool = True):
        self._dim_val = dim
        self._avail = available
        # Build with a mocked runtime so construction doesn't warn; the
        # subclass overrides available anyway.
        with (
            patch("local_embed.engine._Llama", MagicMock()),
            patch("local_embed.engine.check_backend_runtime", return_value=True),
        ):
            super().__init__(backend="gguf", model="bge-m3", gguf_path="/x.gguf")

    @property
    def available(self) -> bool:
        return self._avail

    async def embed(self, texts, model=None):
        if not self._avail:
            raise RuntimeError("embedding backend unavailable")
        if model and model not in self.models:
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

    def test_unknown_model_404(self, client):
        resp = client.post("/v1/embeddings", json={"input": "x", "model": "typo"})
        assert resp.status_code == 404
        assert resp.json()["error"]["type"] == "invalid_request_error"

    def test_backend_failure_503(self):
        build_server(_make_engine(available=False))
        with TestClient(mcp.http_app(path="/mcp")) as c:
            resp = c.post("/v1/embeddings", json={"input": "x"})
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

    def test_default_model_echoes_key_not_repo_id(self):
        """When the client omits `model`, the response echoes the addressable
        config key (the id /v1/models reports), not the internal repo id —
        the repo id is not an engine key and would 404 if echoed back."""
        spec = ModelSpec("bge-m3-transformer", backend="transformer", model="BAAI/bge-m3")
        engine = Engine(specs=[spec], active="bge-m3-transformer")

        async def _embed(texts, model=None):
            return [[0.5] * 1024 for _ in texts]

        engine.embed = _embed
        build_server(engine)
        with TestClient(mcp.http_app(path="/mcp")) as c:
            resp = c.post("/v1/embeddings", json={"input": "hello"})
            assert resp.status_code == 200
            assert resp.json()["model"] == "bge-m3-transformer"


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
        assert body["active_model"] == "bge-m3"
        assert body["dimension"] == 1024

    def test_health_degraded(self):
        build_server(_make_engine(available=False))
        with TestClient(mcp.http_app(path="/mcp")) as c:
            resp = c.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "degraded"


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
