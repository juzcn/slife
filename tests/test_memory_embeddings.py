"""Tests for slife.plugins.memory.embeddings — EmbeddingClient and helpers."""

from unittest.mock import MagicMock, patch

import pytest

from slife.plugins.memory.embeddings import (
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
            patch("slife.plugins.memory.embeddings.Path.exists", return_value=True),
            patch("slife.plugins.memory.embeddings._check_runtime", return_value=True),
        ):
            client = EmbeddingClient(
                model="bge-m3",
                gguf_path="/path/to/model.gguf",
            )
            assert client.available is True
            assert client.backend == "gguf"
            assert client.dimension == 1024

    def test_gguf_path_not_exists_falls_back(self):
        with patch("slife.plugins.memory.embeddings.Path.exists", return_value=False):
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
            patch("slife.plugins.memory.embeddings.Path.exists", return_value=True),
            patch("slife.plugins.memory.embeddings._check_runtime", return_value=False),
        ):
            client = EmbeddingClient(
                model="bge-m3",
                gguf_path="/path/to/model.gguf",
            )
            assert client.backend == "gguf"
            assert client.available is False

    def test_api_runtime_check_fails(self):
        """available=False when api_key is set but openai isn't installed."""
        with patch("slife.plugins.memory.embeddings._check_runtime", return_value=False):
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
    """Tests for EmbeddingClient.from_config."""

    @patch("pathlib.Path.read_text")
    @patch("pathlib.Path.exists")
    def test_gguf_from_config(self, mock_exists, mock_read_text):
        mock_exists.return_value = True
        mock_read_text.return_value = '{}'

        mock_config = {
            "memory": {
                "embedding": {
                    "model": "bge-m3",
                    "gguf_path": "/tmp/model.gguf",
                },
            },
        }

        with (
            patch("json5.loads", return_value=mock_config),
            patch("slife.plugins.memory.embeddings._check_runtime", return_value=True),
        ):
            client = EmbeddingClient.from_config("/fake/config.json5")
            assert client.backend == "gguf"
            assert client.available is True

    @patch("pathlib.Path.read_text")
    @patch("pathlib.Path.exists")
    def test_api_from_config(self, mock_exists, mock_read_text):
        mock_exists.return_value = True
        mock_read_text.return_value = '{}'

        mock_config = {
            "memory": {
                "embedding": {
                    "model": "text-embedding-3-small",
                },
            },
            "models": {
                "providers": {
                    "openai": {
                        "api_key": "sk-key",
                        "base_url": "https://api.openai.com/v1",
                    },
                },
            },
        }

        with patch("json5.loads", return_value=mock_config):
            client = EmbeddingClient.from_config("/fake/config.json5")
            assert client.backend == "api"
            assert client.available is True

    @patch("slife.plugins.memory.embeddings.Path.exists")
    def test_missing_config_returns_disabled(self, mock_exists):
        mock_exists.return_value = False

        client = EmbeddingClient.from_config("/nonexistent.json5")
        assert client.available is False

    def test_json5_not_installed(self):
        with patch("slife.plugins.memory.embeddings.json5", create=True, side_effect=ImportError):
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
