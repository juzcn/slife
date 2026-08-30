"""Tests for local_embed.engine — Engine/ModelSpec load/dim/encode behaviour.

The real llama-cpp / sentence-transformers models are NOT loaded in tests;
the backend clients are mocked so we exercise the engine's own logic (lazy
load, dim override, thread-serialisation, row alignment, multi-model).
"""

import asyncio
import os

import pytest

pytestmark = pytest.mark.unit

from unittest.mock import AsyncMock, MagicMock, patch

from local_embed.engine import (
    Engine,
    ModelSpec,
    _ensure_hf_model,
    _guess_dim,
    check_backend_runtime,
)


class TestEnsureHfModel:
    """Model pre-download with automatic hf-mirror.com fallback."""

    def test_primary_success_no_fallback(self, monkeypatch):
        monkeypatch.delenv("HF_ENDPOINT", raising=False)
        calls = []

        def fake(repo):
            calls.append(repo)
            return "/cache/models--BAAI--bge-m3"

        with patch("huggingface_hub.snapshot_download", side_effect=fake):
            out = _ensure_hf_model("BAAI/bge-m3")
        assert out == "/cache/models--BAAI--bge-m3"
        assert calls == ["BAAI/bge-m3"]
        assert "HF_ENDPOINT" not in os.environ

    def test_primary_fails_mirror_succeeds(self, monkeypatch):
        monkeypatch.delenv("HF_ENDPOINT", raising=False)
        calls = []

        def fake(repo):
            calls.append(repo)
            if len(calls) == 1:
                raise OSError("blocked")
            return "/cache/mirror/bge-m3"

        with patch("huggingface_hub.snapshot_download", side_effect=fake):
            out = _ensure_hf_model("BAAI/bge-m3")
        assert out == "/cache/mirror/bge-m3"
        assert calls == ["BAAI/bge-m3", "BAAI/bge-m3"]
        assert os.environ["HF_ENDPOINT"] == "https://hf-mirror.com"

    def test_explicit_endpoint_respected_no_fallback(self, monkeypatch):
        monkeypatch.setenv("HF_ENDPOINT", "https://internal.example.com")
        calls = []

        def fake(repo):
            calls.append(repo)
            raise OSError("blocked")

        with patch("huggingface_hub.snapshot_download", side_effect=fake):
            with pytest.raises(OSError):
                _ensure_hf_model("BAAI/bge-m3")
        assert calls == ["BAAI/bge-m3"]
        assert os.environ["HF_ENDPOINT"] == "https://internal.example.com"

    def test_both_fail_raises_runtime_error(self, monkeypatch):
        monkeypatch.delenv("HF_ENDPOINT", raising=False)

        def fake(repo):
            raise OSError("blocked")

        with patch("huggingface_hub.snapshot_download", side_effect=fake):
            with pytest.raises(RuntimeError, match="both failed"):
                _ensure_hf_model("BAAI/bge-m3")
        assert os.environ["HF_ENDPOINT"] == "https://hf-mirror.com"


# ── _guess_dim ────────────────────────────────────────────────────────────


class TestGuessDim:
    def test_known_models(self):
        assert _guess_dim("text-embedding-3-small") == 1536
        assert _guess_dim("text-embedding-3-large") == 3072
        assert _guess_dim("text-embedding-ada-002") == 1536
        assert _guess_dim("bge-m3") == 1024
        assert _guess_dim("bge-large") == 1024
        assert _guess_dim("nomic-embed-text") == 768

    def test_unknown_defaults_to_1024(self):
        assert _guess_dim("my-custom-embedder") == 1024

    def test_case_insensitive(self):
        assert _guess_dim("BGE-M3") == 1024


# ── check_backend_runtime ─────────────────────────────────────────────────


class TestCheckRuntime:
    def test_unknown_backend(self):
        assert check_backend_runtime("nope") is False


# ── Engine init ───────────────────────────────────────────────────────────


class TestEngineInit:
    def test_single_model_convenience(self):
        e = Engine(backend="gguf", model="bge-m3", gguf_path="/x.gguf")
        assert e.models == ["bge-m3"]
        assert e.active_model == "bge-m3"
        assert e.dimension == 1024
        assert e.dimension_known is True
        assert e.loaded is False

    def test_multi_model_active(self):
        specs = [
            ModelSpec("bge-m3", backend="gguf", gguf_path="/x.gguf"),
            ModelSpec("nomic", backend="transformer", model="nomic-ai/nomic-embed-text-v1.5"),
        ]
        e = Engine(specs=specs, active="bge-m3")
        assert e.models == ["bge-m3", "nomic"]
        assert e.active_model == "bge-m3"
        assert e.model_spec("nomic").model == "nomic-ai/nomic-embed-text-v1.5"

    def test_unknown_active_falls_back(self):
        specs = [ModelSpec("bge-m3", backend="gguf", gguf_path="/x.gguf")]
        e = Engine(specs=specs, active="nope")
        assert e.active_model == "bge-m3"

    def test_custom_max_tokens(self):
        e = Engine(backend="gguf", model="bge-m3", gguf_path="/x.gguf", max_tokens=1000)
        assert e.max_tokens == 1000


# ── Engine gguf load ──────────────────────────────────────────────────────


class TestGgufLoad:
    @pytest.mark.asyncio
    async def test_load_corrects_dim(self):
        with (
            patch("local_embed.engine._Llama", MagicMock()),
            patch("local_embed.engine.run_daemon", new_callable=AsyncMock) as mock_run,
        ):
            client = MagicMock()
            # n_embd is a bound METHOD on llama_cpp 0.3.34 (regression guard)
            client.n_embd = MagicMock(return_value=768)
            mock_run.return_value = client

            e = Engine(backend="gguf", model="my-embed", gguf_path="/model.gguf")
            dim = await e.ensure_loaded()
            assert dim == 768
            assert e.dimension == 768
            assert e.dimension_known is True
            assert e.loaded is True
            mock_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_load_shared_across_concurrent_callers(self):
        """Two concurrent ensure_loaded() calls share ONE in-flight load."""
        with (
            patch("local_embed.engine._Llama", MagicMock()),
            patch("local_embed.engine.run_daemon", new_callable=AsyncMock) as mock_run,
        ):
            client = MagicMock()
            client.n_embd = MagicMock(return_value=1024)
            mock_run.return_value = client

            e = Engine(backend="gguf", model="bge-m3", gguf_path="/model.gguf")
            results = await asyncio.gather(e.ensure_loaded(), e.ensure_loaded())
            assert results == [1024, 1024]
            assert mock_run.await_count == 1

    @pytest.mark.asyncio
    async def test_load_failure_marks_unavailable(self):
        with (
            patch("local_embed.engine._Llama", MagicMock()),
            patch(
                "local_embed.engine.run_daemon",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ),
        ):
            e = Engine(backend="gguf", model="bge-m3", gguf_path="/model.gguf")
            await e.ensure_loaded()
            assert e.available is False


# ── Engine embed ──────────────────────────────────────────────────────────


class TestEmbed:
    @pytest.mark.asyncio
    async def test_embed_gguf_rows_aligned(self):
        """Empty/whitespace inputs get zero vectors, keeping row alignment."""
        with (
            patch("local_embed.engine._Llama", MagicMock()),
            patch("local_embed.engine.run_daemon", new_callable=AsyncMock) as mock_run,
        ):
            client = MagicMock()
            client.n_embd = MagicMock(return_value=1024)
            client.create_embedding = MagicMock(
                side_effect=lambda t: {
                    "data": [{"embedding": [float(len(t) + 100 + 0.5)] * 1024}]
                }
            )
            # load → client; encode → run the blocking fn inline
            mock_run.side_effect = lambda fn, name="daemon": (
                client if name.startswith("gguf-load") else fn()
            )

            e = Engine(backend="gguf", model="bge-m3", gguf_path="/model.gguf")
            vecs = await e.embed(["hello", "", "a much longer text"])
            assert len(vecs) == 3
            assert len(vecs[0]) == 1024
            assert vecs[1] == [0.0] * 1024
            assert vecs[0] != vecs[2]  # different texts → different vectors

    @pytest.mark.asyncio
    async def test_embed_transformer(self):
        with (
            patch("local_embed.engine._SentenceTransformer", MagicMock()),
            patch("local_embed.engine.run_daemon", new_callable=AsyncMock) as mock_run,
        ):
            client = MagicMock()
            client.get_sentence_embedding_dimension = MagicMock(return_value=768)
            # encode returns an iterable of numpy-like objects (each .tolist())
            emb1 = MagicMock(); emb1.tolist.return_value = [0.1] * 768
            emb2 = MagicMock(); emb2.tolist.return_value = [0.2] * 768
            client.encode = MagicMock(return_value=[emb1, emb2])

            def _side(fn, name="daemon"):
                if name.startswith("transformer-load"):
                    return client
                return fn()  # encode runs inline
            mock_run.side_effect = _side

            e = Engine(backend="transformer", model="BAAI/bge-m3")
            vecs = await e.embed(["a", "b"])
            assert len(vecs) == 2
            assert len(vecs[0]) == 768

    @pytest.mark.asyncio
    async def test_embed_unavailable_raises(self):
        e = Engine(backend="gguf", model="bge-m3", gguf_path="/x.gguf")
        e._failed.add("bge-m3")  # simulate a failed load
        with pytest.raises(RuntimeError):
            await e.embed(["text"])

    @pytest.mark.asyncio
    async def test_embed_empty_list(self):
        e = Engine(backend="gguf", model="bge-m3", gguf_path="/x.gguf")
        e._failed.add("bge-m3")
        with pytest.raises(RuntimeError):
            await e.embed([])


# ── Multi-model switching ─────────────────────────────────────────────────


class TestMultiModel:
    @pytest.mark.asyncio
    async def test_set_active_switches_and_loads(self):
        with (
            patch("local_embed.engine._Llama", MagicMock()),
            patch("local_embed.engine._SentenceTransformer", MagicMock()),
            patch("local_embed.engine.run_daemon", new_callable=AsyncMock) as mock_run,
        ):
            gguf_client = MagicMock()
            gguf_client.n_embd = MagicMock(return_value=1024)
            tf_client = MagicMock()
            tf_client.get_sentence_embedding_dimension = MagicMock(return_value=768)
            tf_client.encode = MagicMock(return_value=[[0.5] * 768])

            def _side(fn, name="daemon"):
                if name.startswith("gguf-load"):
                    return gguf_client
                if name.startswith("transformer-load"):
                    return tf_client
                return fn()
            mock_run.side_effect = _side

            e = Engine(
                specs=[
                    ModelSpec("bge-m3", backend="gguf", gguf_path="/x.gguf"),
                    ModelSpec("nomic", backend="transformer", model="nomic-ai/nomic-embed-text-v1.5"),
                ],
                active="bge-m3",
            )
            assert e.active_model == "bge-m3"
            dim = await e.set_active("nomic")
            assert e.active_model == "nomic"
            assert dim == 768
            assert e.dimension == 768
            assert e.loaded is True
            assert e.is_loaded("nomic") is True
            # switching back loads the gguf model on demand
            dim = await e.set_active("bge-m3")
            assert dim == 1024
            assert e.is_loaded("bge-m3") is True

    @pytest.mark.asyncio
    async def test_embed_named_model(self):
        with (
            patch("local_embed.engine._Llama", MagicMock()),
            patch("local_embed.engine.run_daemon", new_callable=AsyncMock) as mock_run,
        ):
            gguf_client = MagicMock()
            gguf_client.n_embd = MagicMock(return_value=1024)
            gguf_client.create_embedding = MagicMock(
                side_effect=lambda t: {"data": [{"embedding": [1.0] * 1024}]}
            )
            mock_run.side_effect = lambda fn, name="daemon": (
                gguf_client if name.startswith("gguf-load") else fn()
            )

            e = Engine(
                specs=[
                    ModelSpec("bge-m3", backend="gguf", gguf_path="/x.gguf"),
                    ModelSpec("other", backend="gguf", gguf_path="/y.gguf"),
                ],
                active="bge-m3",
            )
            vecs = await e.embed(["hello"], model="other")
            assert len(vecs[0]) == 1024
