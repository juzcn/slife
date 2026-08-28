"""Tests for slife.plugins.memdb.embeddings — EmbeddingClient and helpers."""

import asyncio

import pytest; pytestmark = pytest.mark.unit


from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.plugins.memdb.embeddings import (
    EmbeddingClient,
    _guess_dim,
)


# ── _guess_dim ──────────────────────────────────────────────────────────────


class TestGuessDim:
    """Tests for _guess_dim."""

    def test_known_models(self):
        assert _guess_dim("text-embedding-3-small") == 1536
        assert _guess_dim("text-embedding-3-large") == 3072
        assert _guess_dim("text-embedding-ada-002") == 1536
        assert _guess_dim("bge-m3") == 1024
        assert _guess_dim("bge-large") == 1024
        assert _guess_dim("nomic-embed-text") == 768

    def test_unknown_model_defaults_to_1024(self):
        assert _guess_dim("my-custom-embedder") == 1024

    def test_case_insensitive(self):
        assert _guess_dim("BGE-M3") == 1024
        assert _guess_dim("Text-Embedding-3-Small") == 1536


# ── EmbeddingClient ─────────────────────────────────────────────────────────


class TestEmbeddingClientInit:
    """Tests for EmbeddingClient initialization."""

    def test_api_backend(self):
        client = EmbeddingClient(
            model="text-embedding-3-small",
            api_key="sk-test-key",
            base_url="https://api.openai.com/v1",
        )
        assert client.available is True
        assert client.backend == "api"
        assert client.dimension == 1536

    def test_gguf_backend(self):
        with (
            patch("slife.plugins.memdb.embeddings.Path.exists", return_value=True),
            patch("slife.plugins.memdb.embeddings._check_runtime", return_value=True),
        ):
            client = EmbeddingClient(
                model="bge-m3",
                gguf_path="/path/to/model.gguf",
            )
            assert client.available is True
            assert client.backend == "gguf"
            assert client.dimension == 1024

    def test_gguf_path_not_exists_falls_back(self):
        with patch("slife.plugins.memdb.embeddings.Path.exists", return_value=False):
            client = EmbeddingClient(
                model="bge-m3",
                gguf_path="/nonexistent/model.gguf",
                api_key="sk-key",
            )
            # Should fall through to api backend since key is provided
            assert client.backend == "api"

    def test_no_backend(self):
        client = EmbeddingClient()
        assert client.available is False
        assert client.backend == ""

    def test_explicit_dim(self):
        client = EmbeddingClient(model="custom", dim=512)
        assert client.dimension == 512

    def test_gguf_runtime_check_fails(self):
        """available=False when GGUF file exists but llama-cpp isn't installed."""
        with (
            patch("slife.plugins.memdb.embeddings.Path.exists", return_value=True),
            patch("slife.plugins.memdb.embeddings._check_runtime", return_value=False),
        ):
            client = EmbeddingClient(
                model="bge-m3",
                gguf_path="/path/to/model.gguf",
            )
            assert client.backend == "gguf"
            assert client.available is False

    def test_api_runtime_check_fails(self):
        """available=False when api_key is set but openai isn't installed."""
        with patch("slife.plugins.memdb.embeddings._check_runtime", return_value=False):
            client = EmbeddingClient(
                model="text-embedding-3-small",
                api_key="sk-test-key",
            )
            assert client.backend == "api"
            assert client.available is False

    def test_properties(self):
        client = EmbeddingClient()
        assert client.backend == ""
        assert client.available is False


class TestEmbeddingClientFromConfig:
    """Tests for EmbeddingClient.from_config (top-level ``embeddings`` section)."""

    @patch("pathlib.Path.read_text")
    @patch("pathlib.Path.exists")
    def test_local_embed_from_config(self, mock_exists, mock_read_text):
        """Top-level embeddings: bare provider (local-embed) → api backend."""
        mock_exists.return_value = True
        mock_read_text.return_value = '{}'

        mock_config = {
            "embeddings": {
                "providers": {
                    "local-embed": {
                        "base_url": "http://127.0.0.1:8000/v1",
                        "api_key": "local",
                    },
                },
                "active_model": "local-embed",
                "enabled": True,
            },
        }

        with patch("json5.loads", return_value=mock_config):
            client = EmbeddingClient.from_config("/fake/config.json5")
            assert client.backend == "api"
            assert client.available is True
            assert client._base_url == "http://127.0.0.1:8000/v1"
            assert client._api_key == "local"

    @patch("pathlib.Path.read_text")
    @patch("pathlib.Path.exists")
    def test_config_model_picked_up(self, mock_exists, mock_read_text):
        """A configured active model is picked up verbatim."""
        mock_exists.return_value = True
        mock_read_text.return_value = '{}'

        mock_config = {
            "embeddings": {
                "providers": {
                    "p1": {
                        "base_url": "http://127.0.0.1:8000/v1",
                        "api_key": "local",
                        "models": [{"model": "bge-m3", "dim": 1024}],
                    },
                },
                "active_model": "p1/bge-m3",
                "enabled": True,
            },
        }

        with patch("json5.loads", return_value=mock_config):
            client = EmbeddingClient.from_config("/fake/config.json5")
            assert client.backend == "api"
            assert client._model == "bge-m3"
            assert client.dimension == 1024
            assert client.dimension_known is True

    @patch("pathlib.Path.read_text")
    @patch("pathlib.Path.exists")
    def test_enabled_false_disables(self, mock_exists, mock_read_text):
        mock_exists.return_value = True
        mock_read_text.return_value = '{}'

        mock_config = {
            "embeddings": {
                "providers": {"p1": {"base_url": "http://x/v1", "api_key": "k"}},
                "active_model": "p1",
                "enabled": False,
            },
        }

        with patch("json5.loads", return_value=mock_config):
            client = EmbeddingClient.from_config("/fake/config.json5")
            assert client.available is False

    @patch("slife.plugins.memdb.embeddings.Path.exists")
    def test_missing_config_returns_disabled(self, mock_exists):
        mock_exists.return_value = False

        client = EmbeddingClient.from_config("/nonexistent.json5")
        assert client.available is False

    @patch("pathlib.Path.read_text")
    @patch("pathlib.Path.exists")
    def test_unknown_model_dim_is_provisional(self, mock_exists, mock_read_text):
        """An unrecognised model carries a provisional dim (1024) until the
        backend reports the real width."""
        mock_exists.return_value = True
        mock_read_text.return_value = '{}'

        mock_config = {
            "embeddings": {
                "providers": {
                    "p1": {
                        "base_url": "http://127.0.0.1:8000/v1",
                        "api_key": "k",
                        "models": [{"model": "my-custom-embedder"}],
                    },
                },
                "active_model": "p1/my-custom-embedder",
                "enabled": True,
            },
        }

        with patch("json5.loads", return_value=mock_config):
            client = EmbeddingClient.from_config("/fake/config.json5")
            assert client.backend == "api"
            assert client.available is True
            assert client.dimension == 1024
            assert client.dimension_known is False

    @patch("pathlib.Path.read_text")
    @patch("pathlib.Path.exists")
    def test_known_model_dim_is_authoritative(self, mock_exists, mock_read_text):
        """A recognised model needs no probe — its width is authoritative."""
        mock_exists.return_value = True
        mock_read_text.return_value = '{}'

        mock_config = {
            "embeddings": {
                "providers": {
                    "p1": {
                        "base_url": "http://127.0.0.1:8000/v1",
                        "api_key": "k",
                        "models": [{"model": "text-embedding-3-small"}],
                    },
                },
                "active_model": "p1/text-embedding-3-small",
                "enabled": True,
            },
        }

        with patch("json5.loads", return_value=mock_config):
            client = EmbeddingClient.from_config("/fake/config.json5")
            assert client.backend == "api"
            assert client.dimension == 1536
            assert client.dimension_known is True

    def test_json5_not_installed(self):
        with patch("slife.plugins.memdb.embeddings.json5", create=True, side_effect=ImportError):
            # This simulates json5 not being available
            pass


class TestEmbeddingClientEmbed:
    """Tests for embed() method."""

    @pytest.mark.asyncio
    async def test_embed_not_available(self):
        client = EmbeddingClient()
        result = await client.embed(["test"])
        assert result is None

    @pytest.mark.asyncio
    async def test_embed_empty_list(self):
        client = EmbeddingClient(api_key="sk-key")
        result = await client.embed([])
        assert result == []

    @pytest.mark.asyncio
    async def test_embed_empty_strings(self):
        client = EmbeddingClient(api_key="sk-key", dim=4)
        result = await client.embed(["", "  "])
        # All empty → returns zero vectors with correct dim
        assert result is not None
        assert len(result) == 2
        assert result[0] == [0.0, 0.0, 0.0, 0.0]


class TestEmbeddingClientEmbedOne:
    """Tests for embed_one() convenience method."""

    @pytest.mark.asyncio
    async def test_embed_one_not_available(self):
        client = EmbeddingClient()
        result = await client.embed_one("test")
        assert result is None

    @pytest.mark.asyncio
    async def test_embed_one_with_result(self):
        client = EmbeddingClient(api_key="sk-key", dim=4)
        with patch.object(client, "embed") as mock_embed:
            mock_embed.return_value = [[0.1, 0.2, 0.3, 0.4]]
            result = await client.embed_one("summary text")
            assert result == [0.1, 0.2, 0.3, 0.4]

    @pytest.mark.asyncio
    async def test_embed_one_none_result(self):
        client = EmbeddingClient(api_key="sk-key")
        with patch.object(client, "embed", return_value=None):
            result = await client.embed_one("test")
            assert result is None


class TestEmbeddingConcurrentEmbed:
    """Concurrent embeds on a shared local model must serialise.

    llama-cpp / sentence-transformers instances are NOT safe for concurrent
    encode calls. A burst of hybrid searches (main agent + subagents share
    one memdb server) calls ``embed_one`` at once; without the per-client
    ``_embed_lock`` the GGUF backend used to crash llama.cpp natively
    (``GGML_ASSERT … tensor buffer not set`` abort)."""

    @pytest.mark.asyncio
    async def test_concurrent_gguf_embeds_serialize(self):
        import asyncio
        import threading
        import time

        client = EmbeddingClient.__new__(EmbeddingClient)
        client._backend = "gguf"
        client._available = True
        client._model = "bge-m3"
        client._dim = 4
        client._client = None
        client._loading = None
        client._embed_lock = threading.Lock()

        class FakeLlama:
            def __init__(self):
                self.cur = 0
                self.peak = 0

            def create_embedding(self, text):
                self.cur += 1
                self.peak = max(self.peak, self.cur)
                time.sleep(0.02)
                self.cur -= 1
                return {"data": [{"embedding": [0.1] * 4}]}

        client._client = FakeLlama()

        results = await asyncio.gather(
            *(client.embed_one(f"concurrent query {i}") for i in range(6))
        )
        assert all(r is not None and len(r) == 4 for r in results)
        # Never two create_embedding calls in flight → no native crash.
        assert client._client.peak == 1

    @pytest.mark.asyncio
    async def test_batch_embed_does_not_hold_lock_between_calls(self):
        """A reindex batch may interleave with a search's single embed —
        the lock is per create_embedding call, not per whole batch."""
        import asyncio
        import threading
        import time

        client = EmbeddingClient.__new__(EmbeddingClient)
        client._backend = "gguf"
        client._available = True
        client._model = "bge-m3"
        client._dim = 4
        client._client = None
        client._loading = None
        client._embed_lock = threading.Lock()

        class FakeLlama:
            def __init__(self):
                self.cur = 0
                self.peak = 0

            def create_embedding(self, text):
                self.cur += 1
                self.peak = max(self.peak, self.cur)
                time.sleep(0.01)
                self.cur -= 1
                return {"data": [{"embedding": [0.1] * 4}]}

        client._client = FakeLlama()

        # batch (reindex-style, 3 texts) racing a single search embed
        batch = asyncio.create_task(client.embed(["a", "b", "c"]))
        single = asyncio.create_task(client.embed_one("s"))
        await asyncio.gather(batch, single)
        assert client._client.peak == 1


class TestEmbeddingLoad:
    """load() must materialise the local model exactly once, even under
    concurrent callers — the semantic gate calls load() from every search,
    so without this a burst of searches would load the GGUF model repeatedly."""

    @pytest.mark.asyncio
    async def test_concurrent_load_shares_one_materialisation(self):
        import asyncio

        client = EmbeddingClient.__new__(EmbeddingClient)
        client._backend = "gguf"
        client._available = True
        client._gguf_path = "/tmp/m.gguf"
        client._model = "bge-m3"
        client._dim = 1024
        client._client = None
        client._loading = None
        calls = 0

        async def _fake_load():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            client._client = object()

        client._load_gguf = _fake_load
        await asyncio.gather(client.load(), client.load())
        assert calls == 1
        assert client._client is not None


class TestDimensionKnown:
    """dimension_known distinguishes authoritative widths from bare guesses."""

    def test_known_model_is_authoritative(self):
        client = EmbeddingClient(model="bge-m3")
        assert client.dimension_known is True

    def test_known_api_model_is_authoritative(self):
        client = EmbeddingClient(model="text-embedding-3-small", api_key="sk-key")
        assert client.dimension_known is True

    def test_unknown_model_is_provisional(self):
        client = EmbeddingClient(model="my-custom-embedder")
        assert client.dimension_known is False
        assert client.dimension == 1024  # the provisional guess

    def test_explicit_dim_is_authoritative(self):
        client = EmbeddingClient(model="my-custom-embedder", dim=768)
        assert client.dimension_known is True
        assert client.dimension == 768


class TestDimProbe:
    """Real width is discovered from the backend, not trusted from a guess."""

    @pytest.mark.asyncio
    async def test_load_gguf_corrects_dim_from_n_embd(self):
        import sys
        import threading
        from types import ModuleType

        client = EmbeddingClient.__new__(EmbeddingClient)
        client._backend = "gguf"
        client._available = True
        client._gguf_path = "/tmp/m.gguf"
        client._model = "my-custom-embedder"
        client._dim = 1024
        client._dim_known = False
        client._client = None
        client._loading = None
        client._embed_lock = threading.Lock()

        class FakeLlama:
            n_embd = 768

        fake_mod = ModuleType("llama_cpp")
        fake_mod.Llama = lambda **kw: FakeLlama()

        async def _fake_run(fn, **_kw):
            return fn()

        with (
            patch.dict(sys.modules, {"llama_cpp": fake_mod}),
            patch(
                "slife.threads.run_daemon",
                new=AsyncMock(side_effect=_fake_run),
            ),
        ):
            ok = await client.load()

        assert ok is True
        assert client.dimension == 768
        assert client.dimension_known is True

    @pytest.mark.asyncio
    async def test_load_api_probes_dim(self):
        """An unknown model's guessed API dim is corrected with a cheap probe."""
        client = EmbeddingClient.__new__(EmbeddingClient)
        client._backend = "api"
        client._available = True
        client._model = "my-custom-embedder"
        client._dim = 1024
        client._dim_known = False
        client._client = None

        with patch.object(
            client, "_call_api", new=AsyncMock(return_value=[[0.1] * 768])
        ):
            ok = await client.load()

        assert ok is True
        assert client.dimension == 768
        assert client.dimension_known is True

    @pytest.mark.asyncio
    async def test_load_api_probe_failure_keeps_guess(self):
        """A failed probe leaves the provisional dim and retries next load."""
        client = EmbeddingClient.__new__(EmbeddingClient)
        client._backend = "api"
        client._available = True
        client._model = "my-custom-embedder"
        client._dim = 1024
        client._dim_known = False
        client._client = None

        with patch.object(
            client, "_call_api", new=AsyncMock(side_effect=Exception("boom"))
        ):
            ok = await client.load()

        assert ok is True
        assert client.dimension == 1024
        assert client.dimension_known is False

    @pytest.mark.asyncio
    async def test_load_api_known_dim_skips_probe(self):
        """A recognised model never pays the probe request."""
        client = EmbeddingClient.__new__(EmbeddingClient)
        client._backend = "api"
        client._available = True
        client._model = "text-embedding-3-small"
        client._dim = 1536
        client._dim_known = True
        client._client = None

        with patch.object(client, "_probe_api_dim", new=AsyncMock()) as probe:
            ok = await client.load()

        assert ok is True
        probe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_load_api_discovers_active_model(self):
        """The model is determined by the endpoint's /v1/models active model."""
        client = EmbeddingClient.__new__(EmbeddingClient)
        client._backend = "api"
        client._available = True
        client._model = ""            # not configured — discovered from server
        client._dim = 1024
        client._dim_known = False
        client._client = None
        client._base_url = "http://127.0.0.1:8000/v1"
        client._api_key = "local"
        client._client_init_lock = asyncio.Lock()

        class _Model:
            def __init__(self, id, active=False, dimension=0):
                self.id = id
                self.active = active
                self.dimension = dimension

        class _ModelsResponse:
            data = [
                _Model("bge-m3", active=True, dimension=1024),
                _Model("other", active=False, dimension=768),
            ]

        fake_client = MagicMock()
        fake_client.models.list = AsyncMock(return_value=_ModelsResponse())
        with patch.object(
            client, "_client", new=fake_client, create=True
        ):
            ok = await client.load()

        assert ok is True
        assert client._model == "bge-m3"      # server's active model
        assert client.dimension == 1024
        assert client.dimension_known is True

    @pytest.mark.asyncio
    async def test_load_api_configured_model_is_authoritative(self):
        """A configured model id wins — even when the endpoint's active differs."""
        client = EmbeddingClient.__new__(EmbeddingClient)
        client._backend = "api"
        client._available = True
        client._model = "text-embedding-3-small"   # configured
        client._dim = 1536
        client._dim_known = True
        client._client = None
        client._base_url = "http://127.0.0.1:8000/v1"
        client._api_key = "local"
        client._client_init_lock = asyncio.Lock()

        class _Model:
            def __init__(self, id, active=False, dimension=0):
                self.id = id
                self.active = active
                self.dimension = dimension

        class _ModelsResponse:
            data = [
                _Model("bge-m3", active=True, dimension=1024),
                _Model("text-embedding-3-small", active=False, dimension=1536),
            ]

        fake_client = MagicMock()
        fake_client.models.list = AsyncMock(return_value=_ModelsResponse())
        with patch.object(client, "_client", new=fake_client, create=True):
            ok = await client.load()

        assert ok is True
        assert client._model == "text-embedding-3-small"  # config wins
        assert client.dimension == 1536
        assert client.dimension_known is True

    @pytest.mark.asyncio
    async def test_load_api_configured_model_not_listed_keeps_id(self):
        """A configured model the endpoint doesn't list keeps its id (probe dim)."""
        client = EmbeddingClient.__new__(EmbeddingClient)
        client._backend = "api"
        client._available = True
        client._model = "some-model"          # configured but not listed
        client._dim = 1024
        client._dim_known = False
        client._client = None
        client._base_url = "http://127.0.0.1:8000/v1"
        client._api_key = "local"
        client._client_init_lock = asyncio.Lock()

        class _Model:
            def __init__(self, id, active=False, dimension=0):
                self.id = id
                self.active = active
                self.dimension = dimension

        class _ModelsResponse:
            data = [_Model("bge-m3", active=True, dimension=1024)]

        fake_client = MagicMock()
        fake_client.models.list = AsyncMock(return_value=_ModelsResponse())
        with patch.object(client, "_client", new=fake_client, create=True):
            ok = await client.load()

        assert ok is True
        assert client._model == "some-model"  # configured id preserved
        # dim stays provisional — no listing to pin it
        assert client.dimension_known is False
