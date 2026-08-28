"""Tests for the local-embed HTTP embedding surface (via the Starlette
TestClient transport — the same client shape slife's EmbeddingClient uses).
"""

import pytest

pytestmark = pytest.mark.unit

from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from local_embed.engine import Engine
from local_embed.server import build_server, mcp


def _make_engine(dim: int = 768) -> Engine:
    with (
        patch("local_embed.engine._Llama", MagicMock()),
        patch("local_embed.engine.check_backend_runtime", return_value=True),
    ):
        e = Engine(backend="gguf", model="bge-m3", gguf_path="/x.gguf")

    async def _embed(texts, model=None):
        return [[0.5] * dim for _ in texts]

    e.embed = _embed
    return e


@pytest.fixture(scope="module")
def client():
    build_server(_make_engine())
    return TestClient(mcp.http_app(path="/mcp"))


def test_client_embed_shape(client):
    """An OpenAI-compatible client POST returns the expected data shape."""
    resp = client.post(
        "/v1/embeddings",
        json={"model": "bge-m3", "input": ["hello world", "second"]},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 2
    assert [d["index"] for d in data] == [0, 1]
    assert len(data[0]["embedding"]) == 768
    assert data[0]["object"] == "embedding"


def test_client_single_input(client):
    resp = client.post("/v1/embeddings", json={"model": "bge-m3", "input": "just one"})
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 1


def test_client_usage_reported(client):
    resp = client.post("/v1/embeddings", json={"input": "hello"})
    body = resp.json()
    assert "usage" in body
    assert body["usage"]["total_tokens"] >= 1
