"""EmbeddingClient + config-driven availability unit tests."""

import json

import httpx2
import pytest
import pytest_asyncio

from mcp_plugin import config as plugin_config
from mcp_plugin.embeddings import EmbeddingClient


def _make_transport(models=None, embeddings_dim=3):
    """MockTransport answering /models + /embeddings (OpenAI-compatible)."""
    models = models or [
        {"id": "bge-m3", "dimension": embeddings_dim, "active": True},
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": models})
        if request.url.path.endswith("/embeddings"):
            body = json.loads(request.content or b"{}")
            n = len(body.get("input", []))
            data = [
                {"object": "embedding", "index": i,
                 "embedding": [float(i + 1)] + [0.0] * (embeddings_dim - 1)}
                for i in range(n)
            ]
            return httpx2.Response(200, json={"data": data})
        return httpx2.Response(404, json={"error": "not found"})

    return httpx2.MockTransport(handler)


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

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["authorization"] = request.headers.get("authorization")
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "bge-m3", "dimension": 3, "active": True}]})
        return httpx2.Response(404, json={"error": "not found"})

    cfg = {"servers": {}, "embeddings": {
        "base_url": "http://127.0.0.1:17347/v1",
        "api_key": "${EMBED_API_KEY}",
    }}
    path = tmp_path / "mcp-plugin.json5"
    plugin_config.write_config(path, cfg)
    c = EmbeddingClient.from_plugin_config(config_path=str(path))
    c._transport = httpx2.MockTransport(handler)  # test hook, see EmbeddingClient.__init__
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
    cfg = plugin_config.resolve_server_config("svcA", {"command": "npx", "auto-load": True})
    assert cfg.auto_load is True
    cfg2 = plugin_config.resolve_server_config("svcB", {"command": "npx"})
    assert cfg2.auto_load is False


# ── Host-provided override (initialize clientInfo) precedence ────────────


def test_from_plugin_config_override_wins(tmp_path):
    """A host-passed embedding endpoint (initialize clientInfo) beats json5."""
    cfg = tmp_path / "mcp-plugin.json5"
    cfg.write_text(
        json.dumps({"embeddings": {"base_url": "http://own.example/v1"}}),
        encoding="utf-8",
    )
    c = EmbeddingClient.from_plugin_config(
        config_path=str(cfg),
        override={"base_url": "http://host.example/v1",
                  "model": "bge-m3", "api_key": "k"},
    )
    assert c.available
    assert c.base_url == "http://host.example/v1"
    assert c.model == "bge-m3"
    assert c.api_key == "k"


def test_from_plugin_config_no_override_uses_own_json5(tmp_path):
    cfg = tmp_path / "mcp-plugin.json5"
    cfg.write_text(
        json.dumps({"embeddings": {"base_url": "http://own.example/v1",
                                   "model": "own-model"}}),
        encoding="utf-8",
    )
    c = EmbeddingClient.from_plugin_config(config_path=str(cfg), override=None)
    assert c.available
    assert c.base_url == "http://own.example/v1"
    assert c.model == "own-model"


def test_from_plugin_config_unusable_override_falls_back(tmp_path):
    """A placeholder/empty override base_url is not "passed" — fall back."""
    cfg = tmp_path / "mcp-plugin.json5"
    cfg.write_text(
        json.dumps({"embeddings": {"base_url": "http://own.example/v1"}}),
        encoding="utf-8",
    )
    c = EmbeddingClient.from_plugin_config(
        config_path=str(cfg),
        override={"base_url": "${UNRESOLVED_EMB}"},
    )
    assert c.base_url == "http://own.example/v1"
    assert c.available


def test_from_plugin_config_no_usable_config_disabled(tmp_path):
    cfg = tmp_path / "mcp-plugin.json5"
    cfg.write_text(json.dumps({}), encoding="utf-8")
    c = EmbeddingClient.from_plugin_config(config_path=str(cfg), override=None)
    assert not c.available


def test_extensions_capability_roundtrips():
    """Host extras ride in ``capabilities.extensions`` and survive the SDK
    model round-trip (mcp ≥2.0 — ``clientInfo.other`` was dropped).

    The mcp gateway handshakes its embedding endpoint to the wrapper via the
    standard ``capabilities.extensions`` map (identifier → settings); this
    locks the server's ability to read them back from the initialize params.
    """
    from mcp.types import ClientCapabilities

    caps = ClientCapabilities(extensions={
        "embeddings": {
            "base_url": "http://host.example/v1", "api_key": "", "model": "",
        },
    })
    assert caps.extensions["embeddings"]["base_url"] == "http://host.example/v1"
    dumped = caps.model_dump(by_alias=True)
    assert dumped["extensions"]["embeddings"]["base_url"] == "http://host.example/v1"
    parsed = ClientCapabilities.model_validate(dumped)
    assert parsed.extensions["embeddings"]["base_url"] == "http://host.example/v1"


