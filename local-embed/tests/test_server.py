"""Tests for local_embed.server — MCP tools + OpenAI-compatible HTTP routes.

A real Engine with a mocked model backend replaces the real model; the
FastMCP app is exercised via its ASGI transport (``mcp.http_app``) so we
test the actual route handlers end-to-end without spawning a server.
"""

import pytest

pytestmark = pytest.mark.unit

from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

from local_embed.engine import Engine
from local_embed.server import build_server, mcp


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
            "/v1/embeddings", json={"input": ["a", "b"], "model": "m"}
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

    def test_backend_failure_503(self):
        build_server(_make_engine(available=False))
        with TestClient(mcp.http_app(path="/mcp")) as c:
            resp = c.post("/v1/embeddings", json={"input": "x"})
            assert resp.status_code == 503


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

    def test_model_retrieve(self, client):
        resp = client.get("/v1/models/bge-m3")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "bge-m3"
        assert body["object"] == "model"
        assert body["owned_by"] == "local-embed"
        assert body["dimension"] == 1024
        assert body["dimension_known"] is True

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
