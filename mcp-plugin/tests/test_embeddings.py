"""EmbeddingClient + config-driven availability unit tests."""

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
        model="bge-m3", api_key="local", base_url="http://127.0.0.1:17347/v1",
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
    cfg = {"servers": {}, "embeddings": {"base_url": "http://127.0.0.1:17347/v1", "model": "bge-m3"}}
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


def test_from_config_api_key_placeholder_resolves_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBED_API_KEY", "sk-test")
    cfg = {"servers": {}, "embeddings": {
        "base_url": "http://127.0.0.1:17347/v1",
        "api_key": "${EMBED_API_KEY}",
    }}
    path = tmp_path / "mcp-plugin.json5"
    plugin_config.write_config(path, cfg)
    c = EmbeddingClient.from_plugin_config(config_path=str(path))
    assert c.available is True
    assert c.api_key == "sk-test"


def test_from_config_api_key_placeholder_unresolved_is_empty(tmp_path, monkeypatch):
    # Hermetic: no env var, and never fall through to the real keyring lookup.
    monkeypatch.delenv("NO_SUCH_EMBED_KEY_EVER", raising=False)
    monkeypatch.setattr(plugin_config, "_try_credstore_lookup", lambda key: None)
    cfg = {"servers": {}, "embeddings": {
        "base_url": "http://127.0.0.1:17347/v1",
        "api_key": "${NO_SUCH_EMBED_KEY_EVER}",  # not in env, not in credstore
    }}
    path = tmp_path / "mcp-plugin.json5"
    plugin_config.write_config(path, cfg)
    c = EmbeddingClient.from_plugin_config(config_path=str(path))
    assert c.available is True          # base_url real → client available
    assert c.api_key == ""              # placeholder ⇒ no auth header


@pytest.mark.asyncio
async def test_api_key_placeholder_resolved_value_sent_as_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBED_API_KEY", "sk-test")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "bge-m3", "dimension": 3, "active": True}]})
        return httpx.Response(404, json={"error": "not found"})

    cfg = {"servers": {}, "embeddings": {
        "base_url": "http://127.0.0.1:17347/v1",
        "api_key": "${EMBED_API_KEY}",
    }}
    path = tmp_path / "mcp-plugin.json5"
    plugin_config.write_config(path, cfg)
    c = EmbeddingClient.from_plugin_config(config_path=str(path))
    c._transport = httpx.MockTransport(handler)  # test hook, see EmbeddingClient.__init__
    assert await c.load() is True
    assert seen["authorization"] == "Bearer sk-test"
    await c.close()


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


# ── probe_available (fast auto-degradation probe) ───────────────────


@pytest.mark.asyncio
async def test_probe_available_true_when_endpoint_answers(client):
    assert await client.probe_available() is True


@pytest.mark.asyncio
async def test_probe_available_false_when_unavailable():
    c = EmbeddingClient()  # no base_url
    assert await c.probe_available() is False


@pytest.mark.asyncio
async def test_probe_available_false_when_endpoint_unreachable():
    # Point at a port nothing listens on — probe must return False quickly
    # (short timeout, no hang), so build can auto-degrade.
    c = EmbeddingClient(
        model="bge-m3", base_url="http://127.0.0.1:1/v1",
    )
    try:
        assert await c.probe_available(timeout=1.0) is False
    finally:
        await c.close()


def test_resolve_server_config_auto_load():
    cfg = plugin_config.resolve_server_config("svcA", {"command": "npx", "auto_load": True})
    assert cfg.auto_load is True
    cfg2 = plugin_config.resolve_server_config("svcB", {"command": "npx"})
    assert cfg2.auto_load is False


def test_resolve_server_config_healthy():
    # healthy is a build-set probe verdict — default True, parsed from the flag.
    cfg = plugin_config.resolve_server_config("svcA", {"command": "npx"})
    assert cfg.healthy is True
    cfg_bad = plugin_config.resolve_server_config(
        "svcB", {"command": "npx", "healthy": False},
    )
    assert cfg_bad.healthy is False
