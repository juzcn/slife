"""Tests for local_embed.engine — Engine/ModelSpec load/dim/encode behaviour.

The real llama-cpp / sentence-transformers models are NOT loaded in tests;
the backend clients are mocked so we exercise the engine's own logic (lazy
load, dim override, thread-serialisation, row alignment, multi-model).
"""

import asyncio

import pytest

pytestmark = pytest.mark.unit

from unittest.mock import AsyncMock, MagicMock, patch

from local_embed.engine import (
    EmbeddingInputEmpty,
    EmbeddingInputTooLong,
    Engine,
    ModelSpec,
    _guess_dim,
    check_backend_runtime,
)


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
        assert e.model_spec("bge-m3").gguf_path == "/x.gguf"
        assert e.is_loaded("bge-m3") is False

    def test_multi_model_peers(self):
        """All configured models are peers — no active one."""
        specs = [
            ModelSpec("bge-m3", backend="gguf", gguf_path="/x.gguf"),
            ModelSpec("nomic", backend="transformer", model="nomic-ai/nomic-embed-text-v1.5"),
        ]
        e = Engine(specs=specs)
        assert e.models == ["bge-m3", "nomic"]
        assert e.model_spec("nomic").model == "nomic-ai/nomic-embed-text-v1.5"
        assert e.model_spec("bge-m3").max_tokens == 8192

    def test_custom_max_tokens(self):
        e = Engine(backend="gguf", model="bge-m3", gguf_path="/x.gguf", max_tokens=1000)
        assert e.model_spec("bge-m3").max_tokens == 1000

    def test_available_for_per_model(self):
        with patch("local_embed.engine._Llama", MagicMock()):
            e = Engine(
                specs=[
                    ModelSpec("a", backend="gguf", gguf_path="/a.gguf"),
                    ModelSpec("b", backend="gguf", gguf_path="/b.gguf"),
                ],
            )
            assert e.available_for("a") is True
            e._failed.add("b")
            assert e.available_for("b") is False
            assert e.available_for("a") is True


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
            dim = await e.ensure_loaded("my-embed")
            assert dim == 768
            assert e.model_spec("my-embed").dim == 768
            assert e.model_spec("my-embed").dim_known is True
            assert e.is_loaded("my-embed") is True
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
            results = await asyncio.gather(
                e.ensure_loaded("bge-m3"), e.ensure_loaded("bge-m3"),
            )
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
            await e.ensure_loaded("bge-m3")
            assert e.available_for("bge-m3") is False


# ── Engine embed ──────────────────────────────────────────────────────────


class TestEmbed:
    @pytest.mark.asyncio
    async def test_embed_rejects_empty_input(self):
        """OpenAI forbids empty-string input — a blank input raises
        EmbeddingInputEmpty (a 400 on the wire) before any load/encode;
        there is no zero-vector row alignment."""
        e = Engine(backend="gguf", model="bge-m3", gguf_path="/model.gguf")
        with pytest.raises(EmbeddingInputEmpty, match="empty"):
            await e.embed(["hello", ""], "bge-m3")
        with pytest.raises(EmbeddingInputEmpty):
            await e.embed(["   "], "bge-m3")

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
            vecs = await e.embed(["a", "b"], "BAAI/bge-m3")
            assert len(vecs) == 2
            assert len(vecs[0]) == 768

    @pytest.mark.asyncio
    async def test_embed_unavailable_raises(self):
        e = Engine(backend="gguf", model="bge-m3", gguf_path="/x.gguf")
        e._failed.add("bge-m3")  # simulate a failed load
        with pytest.raises(RuntimeError):
            await e.embed(["text"], "bge-m3")

    @pytest.mark.asyncio
    async def test_embed_empty_list(self):
        """An empty batch short-circuits to [] — even with an unavailable
        backend (parameter validation wins; nothing to embed)."""
        e = Engine(backend="gguf", model="bge-m3", gguf_path="/x.gguf")
        e._failed.add("bge-m3")
        assert await e.embed([], "bge-m3") == []

    @pytest.mark.asyncio
    async def test_embed_requires_model(self):
        """Every request names the model — no active fallback to pick one."""
        e = Engine(backend="gguf", model="bge-m3", gguf_path="/x.gguf")
        with pytest.raises(ValueError, match="model is required"):
            await e.embed(["x"], "")
        with pytest.raises(ValueError, match="model is required"):
            await e.embed(["x"], None)  # type: ignore[arg-type]


class TestEmbedInputTooLong:
    """Over-limit inputs must be rejected, not silently truncated by the
    local backends (llama.cpp n_ctx / sentence-transformers max_seq_length)."""

    @pytest.mark.asyncio
    async def test_gguf_oversized_input_rejected(self):
        with (
            patch("local_embed.engine._Llama", MagicMock()),
            patch("local_embed.engine.run_daemon", new_callable=AsyncMock) as mock_run,
        ):
            client = MagicMock()
            client.n_embd = MagicMock(return_value=1024)
            client.create_embedding = MagicMock(
                return_value={"data": [{"embedding": [0.1] * 1024}]}
            )
            client.tokenize = MagicMock(return_value=list(range(9000)))
            mock_run.side_effect = lambda fn, name="daemon": (
                client if name.startswith("gguf-load") else fn()
            )

            e = Engine(backend="gguf", model="bge-m3", gguf_path="/model.gguf")
            with pytest.raises(EmbeddingInputTooLong) as ei:
                await e.embed(["x" * 100], "bge-m3")
            assert "9000 tokens" in str(ei.value)
            assert "8192" in str(ei.value)
            # Rejected BEFORE encoding — nothing was truncated into a vector.
            client.create_embedding.assert_not_called()

    @pytest.mark.asyncio
    async def test_gguf_within_limit_passes(self):
        with (
            patch("local_embed.engine._Llama", MagicMock()),
            patch("local_embed.engine.run_daemon", new_callable=AsyncMock) as mock_run,
        ):
            client = MagicMock()
            client.n_embd = MagicMock(return_value=1024)
            client.create_embedding = MagicMock(
                return_value={"data": [{"embedding": [0.1] * 1024}]}
            )
            client.tokenize = MagicMock(return_value=list(range(100)))
            mock_run.side_effect = lambda fn, name="daemon": (
                client if name.startswith("gguf-load") else fn()
            )

            e = Engine(backend="gguf", model="bge-m3", gguf_path="/model.gguf")
            vecs = await e.embed(["a short input"], "bge-m3")
            assert len(vecs) == 1 and len(vecs[0]) == 1024
            client.create_embedding.assert_called_once()

    @pytest.mark.asyncio
    async def test_transformer_oversized_rejected_via_char_floor(self):
        """Transformer has no cheap tokenizer here — the conservative
        1-char/token floor still rejects what would be truncated."""
        with (
            patch("local_embed.engine._SentenceTransformer", MagicMock()),
            patch("local_embed.engine.run_daemon", new_callable=AsyncMock) as mock_run,
        ):
            client = MagicMock()
            client.get_sentence_embedding_dimension = MagicMock(return_value=768)
            client.encode = MagicMock(return_value=[])
            mock_run.side_effect = lambda fn, name="daemon": (
                client if name.startswith("transformer-load") else fn()
            )

            e = Engine(backend="transformer", model="BAAI/bge-m3")
            with pytest.raises(EmbeddingInputTooLong):
                await e.embed(["x" * 9000], "BAAI/bge-m3")
            client.encode.assert_not_called()


# ── Per-model autoload ───────────────────────────────────────────────────


class TestLoadAutoload:
    @pytest.mark.asyncio
    async def test_loads_only_flagged_models(self):
        """autoload is PER MODEL — only the flagged model is eager-loaded;
        every unflagged peer stays lazy (its weights are never touched)."""
        with (
            patch("local_embed.engine._Llama", MagicMock()),
            patch("local_embed.engine.run_daemon", new_callable=AsyncMock) as mock_run,
        ):
            def _client(dim):
                c = MagicMock()
                c.n_embd = MagicMock(return_value=dim)
                return c

            warm = _client(1024)
            cold = _client(768)
            mock_run.side_effect = lambda fn, name="daemon": (
                warm if name.startswith("gguf-load-warm") else cold
            )

            e = Engine(
                specs=[
                    ModelSpec("warm", backend="gguf", gguf_path="/w.gguf",
                              autoload=True),
                    ModelSpec("cold", backend="gguf", gguf_path="/c.gguf"),
                ],
            )
            assert e.model_spec("warm").autoload is True
            assert e.model_spec("cold").autoload is False

            await e.load_autoload()

            assert e.is_loaded("warm") is True
            assert e.is_loaded("cold") is False

    def test_autoload_defaults_false(self):
        spec = ModelSpec("m", backend="gguf", gguf_path="/x.gguf")
        assert spec.autoload is False


class TestIsLoading:
    @pytest.mark.asyncio
    async def test_true_while_load_in_flight(self):
        with patch("local_embed.engine._Llama", MagicMock()):
            e = Engine(backend="gguf", model="bge-m3", gguf_path="/x.gguf")
            pending = asyncio.get_running_loop().create_future()

            async def _block_load(fn="load", name="daemon"):
                await pending                  # the load never finishes
                return object()

            with patch(
                "local_embed.engine.run_daemon",
                new_callable=AsyncMock,
                side_effect=_block_load,
            ):
                assert e.is_loading("bge-m3") is False
                task = asyncio.create_task(e.ensure_loaded("bge-m3"))
                await asyncio.sleep(0)              # let the load start
                assert e.is_loading("bge-m3") is True
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            # cancelled load cleaned up — no longer "loading"
            assert e.is_loading("bge-m3") is False


# ── Multi-model peers ────────────────────────────────────────────────────


class TestMultiModel:
    @pytest.mark.asyncio
    async def test_named_model_loads_on_demand(self):
        """Two peers load independently — the named model materialises on
        first use, the other stays untouched (no active model to switch)."""
        with (
            patch("local_embed.engine._Llama", MagicMock()),
            patch("local_embed.engine._SentenceTransformer", MagicMock()),
            patch("local_embed.engine.run_daemon", new_callable=AsyncMock) as mock_run,
        ):
            gguf_client = MagicMock()
            gguf_client.n_embd = MagicMock(return_value=1024)
            tf_client = MagicMock()
            tf_client.get_sentence_embedding_dimension = MagicMock(return_value=768)
            emb = MagicMock()
            emb.tolist.return_value = [0.5] * 768
            tf_client.encode = MagicMock(return_value=[emb])

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
            )
            # Every call names the model — no active state, nothing switched.
            dim = await e.ensure_loaded("nomic")
            assert dim == 768
            assert e.is_loaded("nomic") is True
            assert e.is_loaded("bge-m3") is False   # the peer stays unloaded
            vecs = await e.embed(["hi"], "nomic")
            assert len(vecs[0]) == 768

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
            )
            vecs = await e.embed(["hello"], "other")
            assert len(vecs[0]) == 1024

    @pytest.mark.asyncio
    async def test_embed_unknown_model_raises(self):
        """An unknown model must raise — never silently fall back to
        another model (which would return the wrong vectors labeled with
        the requested name)."""
        with patch("local_embed.engine._Llama", MagicMock()):
            e = Engine(
                specs=[ModelSpec("bge-m3", backend="gguf", gguf_path="/x.gguf")],
            )
            with pytest.raises(KeyError, match="unknown model"):
                await e.embed(["hello"], "typo")
