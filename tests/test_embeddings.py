"""EmbeddingClient + config-driven availability unit tests."""

import json

import httpx2
import pytest
import pytest_asyncio

from slife.plugins.mcp_gateway import config as plugin_config
from slife.tools.semantic import EmbeddingClient


def _make_transport(models=None, embeddings_dim=3):
    """MockTransport answering /models + /embeddings (OpenAI-compatible)."""
    models = models or [
        {"id": "bge-m3", "dimension": embeddings_dim},
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


# ── from_endpoint availability (the host's active embeddings endpoint) ───


def _override(**kw) -> dict:
    return {
        "base_url": "http://127.0.0.1:17347/v1",
        "model": "bge-m3",
        "api_key": "local",
        **kw,
    }


def test_from_endpoint_absent_not_available():
    c = EmbeddingClient.from_endpoint(None)
    assert c.available is False


def test_from_endpoint_empty_dict_not_available():
    c = EmbeddingClient.from_endpoint({})
    assert c.available is False


def test_from_endpoint_available():
    c = EmbeddingClient.from_endpoint(_override())
    assert c.available is True
    assert c.model == "bge-m3"


def test_from_endpoint_placeholder_not_available():
    c = EmbeddingClient.from_endpoint(_override(base_url="${LOCAL_EMBED_URL}"))
    assert c.available is False


def test_from_endpoint_api_key_placeholder_resolves_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBED_API_KEY", "sk-test")
    c = EmbeddingClient.from_endpoint(_override(api_key="${EMBED_API_KEY}"))
    assert c.available is True
    assert c.api_key == "sk-test"


def test_from_endpoint_api_key_placeholder_unresolved_is_empty(tmp_path, monkeypatch):
    # Hermetic: no env var, and never fall through to the real keyring lookup.
    monkeypatch.delenv("NO_SUCH_EMBED_KEY_EVER", raising=False)
    monkeypatch.setattr("slife.config._try_credstore_lookup", lambda key: None)
    c = EmbeddingClient.from_endpoint(
        _override(api_key="${NO_SUCH_EMBED_KEY_EVER}"),
    )
    assert c.available is True          # base_url real → client available
    assert c.api_key == ""              # placeholder ⇒ no auth header


@pytest.mark.asyncio
async def test_api_key_placeholder_resolved_value_sent_as_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBED_API_KEY", "sk-test")
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["authorization"] = request.headers.get("authorization")
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "bge-m3", "dimension": 3}]})
        return httpx2.Response(404, json={"error": "not found"})

    c = EmbeddingClient.from_endpoint(_override(api_key="${EMBED_API_KEY}"))
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


def test_max_tokens_constructor_explicit_wins():
    assert EmbeddingClient(model="bge-m3", max_tokens=512).max_tokens == 512


@pytest.mark.asyncio
async def test_max_tokens_guessed_when_list_missing_it(client):
    # The endpoint's model entry carries no max_tokens → best-effort
    # per-family guess (the drainer's chunk ceiling).
    assert await client.load() is True
    assert client.max_tokens == 8192  # memdb's bge-m3 guess


@pytest.mark.asyncio
async def test_max_tokens_captured_from_models_list():
    c = EmbeddingClient(
        model="bge-m3", api_key="local", base_url="http://127.0.0.1:17347/v1",
        transport=_make_transport(
            models=[{"id": "bge-m3", "dimension": 3, "max_tokens": 2048}],
        ),
    )
    assert await c.load() is True
    assert c.max_tokens == 2048  # endpoint report wins over the guess
    await c.close()


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
        assert await c.probe_available(timeout=1.0) is False  # noqa-timeout
    finally:
        await c.close()


def test_resolve_server_config_auto_load():
    cfg = plugin_config.resolve_server_config("svcA", {"command": "npx", "autoload": True})
    assert cfg.auto_load is True
    cfg2 = plugin_config.resolve_server_config("svcB", {"command": "npx"})
    assert cfg2.auto_load is False


# ── from_endpoint precedence (the only embedding source now) ─────────────


def test_from_endpoint_override_wins(tmp_path):
    """The host's active endpoint configures the client."""
    c = EmbeddingClient.from_endpoint(
        {"base_url": "http://host.example/v1",
         "model": "bge-m3", "api_key": "k"},
    )
    assert c.available
    assert c.base_url == "http://host.example/v1"
    assert c.model == "bge-m3"
    assert c.api_key == "k"


def test_from_endpoint_placeholder_disabled(tmp_path):
    """A placeholder base_url is never "passed" — disabled."""
    c = EmbeddingClient.from_endpoint({"base_url": "${UNRESOLVED_EMB}"})
    assert not c.available


