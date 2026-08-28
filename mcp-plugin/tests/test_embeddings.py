"""EmbeddingClient + embeddings config accessor unit tests."""

import json

import httpx
import pytest
import pytest_asyncio

from mcp_plugin import config as plugin_config
from mcp_plugin.embeddings import EmbeddingClient


def _make_transport(models=None, embeddings_dim=3):
    """MockTransport answering /models + /embeddings (OpenAI-compatible)."""
    models = models or [
        {"id": "bge-m3", "dimension": embeddings_dim, "active": True},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": models})
        if request.url.path.endswith("/embeddings"):
            body = json.loads(request.content or b"{}")
            n = len(body.get("input", []))
            data = [
                {"object": "embedding", "index": i,
                 "embedding": [float(i + 1)] + [0.0] * (embeddings_dim - 1)}
                for i in range(n)
            ]
            return httpx.Response(200, json={"data": data})
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


@pytest_asyncio.fixture
async def client():
    c = EmbeddingClient(
        model="bge-m3", api_key="local", base_url="http://127.0.0.1:8000/v1",
        transport=_make_transport(),
    )
    yield c
    await c.close()


# ── from_plugin_config availability ────────────────────────────────


def test_from_config_absent_not_available(tmp_path):
    plugin_config.set_config_path(tmp_path / "mcp-plugin.json5")
    plugin_config.write_config(tmp_path / "mcp-plugin.json5", {"servers": {}})
    c = EmbeddingClient.from_plugin_config(config_path=str(tmp_path / "mcp-plugin.json5"))
    assert c.available is False


def test_from_config_present_available(tmp_path):
    cfg = {"servers": {}, "embeddings": {"base_url": "http://127.0.0.1:8000/v1", "model": "bge-m3"}}
    path = tmp_path / "mcp-plugin.json5"
    plugin_config.write_config(path, cfg)
    c = EmbeddingClient.from_plugin_config(config_path=str(path))
    assert c.available is True
    assert c.model == "bge-m3"


def test_from_config_placeholder_not_available(tmp_path):
    cfg = {"servers": {}, "embeddings": {"base_url": "${LOCAL_EMBED_URL}"}}
    path = tmp_path / "mcp-plugin.json5"
    plugin_config.write_config(path, cfg)
    c = EmbeddingClient.from_plugin_config(config_path=str(path))
    assert c.available is False


# ── load / discover / embed ────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_pins_model_and_dim(client):
    assert await client.load() is True
    assert client.loaded is True
    assert client.model == "bge-m3"
    assert client.dimension == 3
    assert client.dimension_known is True


@pytest.mark.asyncio
async def test_embed_batch(client):
    assert await client.load()
    vecs = await client.embed(["one", "two"])
    assert vecs is not None and len(vecs) == 2
    assert all(len(v) == 3 for v in vecs)


@pytest.mark.asyncio
async def test_embed_one_failure_returns_none():
    # Unreachable base_url → _call_api returns None.
    c = EmbeddingClient(model="x", base_url="http://127.0.0.1:9/v1")
    assert await c.embed_one("hi") is None
    await c.close()


def test_available_false_without_base_url():
    assert EmbeddingClient().available is False


# ── config accessors ───────────────────────────────────────────────


def test_embeddings_config_roundtrip(tmp_path):
    plugin_config.set_config_path(tmp_path / "mcp-plugin.json5")
    assert plugin_config.get_embeddings_config() is None

    plugin_config.set_embeddings_config({"base_url": "http://127.0.0.1:8000/v1"})
    assert plugin_config.get_embeddings_config() == {"base_url": "http://127.0.0.1:8000/v1"}

    assert plugin_config.remove_embeddings_config() is True
    assert plugin_config.get_embeddings_config() is None
    assert plugin_config.remove_embeddings_config() is False


def test_embeddings_config_does_not_touch_servers(tmp_path):
    plugin_config.set_config_path(tmp_path / "mcp-plugin.json5")
    plugin_config.add_server_entry("svcA", {"command": "npx", "args": ["x"]})
    plugin_config.set_embeddings_config({"base_url": "http://127.0.0.1:8000/v1"})
    raw = plugin_config.read_config(tmp_path / "mcp-plugin.json5")
    assert "svcA" in raw["servers"]
    assert raw["embeddings"]["base_url"] == "http://127.0.0.1:8000/v1"


def test_resolve_server_config_auto_load():
    cfg = plugin_config.resolve_server_config("svcA", {"command": "npx", "auto_load": True})
    assert cfg.auto_load is True
    cfg2 = plugin_config.resolve_server_config("svcB", {"command": "npx"})
    assert cfg2.auto_load is False
