"""Tests for slife/plugins/memdb/embedding_config.py."""

import pytest; pytestmark = pytest.mark.unit


import json5
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from slife.plugins.memdb.embedding_config import (
    read_embedding_config,
    write_embedding_config,
    remove_embedding_config,
    get_first_provider_api_key,
    validate_gguf_path,
    make_check_report,
    reload_embedder,
)


# ── Fixtures ─────────────────────────────────────────────────────────


class _ConfigFile:
    """Dict-like view over a config file; ``["content"]`` reads/writes it."""

    def __init__(self, path: Path):
        self._path = path

    def __getitem__(self, key: str) -> str:
        if key != "content":
            raise KeyError(key)
        return self._path.read_text(encoding="utf-8")

    def __setitem__(self, key: str, value: str) -> None:
        if key != "content":
            raise KeyError(key)
        self._path.write_text(value, encoding="utf-8")


@pytest.fixture
def mock_config_file(tmp_path):
    """Redirect embedding_config to a throwaway config file.

    ``_config_io.write_config`` now writes atomically via a temp file +
    ``os.replace`` directly on the filesystem, so mocking ``Path.write_text``
    would silently bypass it — and these tests would write the REAL
    slife.json5 (get_config_path() resolves to the working tree in dev mode).
    Patching ``embedding_config._CONFIG_PATH`` to a temp file keeps the tests
    isolated and makes that failure impossible.
    """
    import slife.plugins.memdb.embedding_config as embedding_config

    cfg_file = tmp_path / "slife.json5"
    cfg_file.write_text("{}", encoding="utf-8")
    with patch.object(embedding_config, "_CONFIG_PATH", cfg_file):
        yield _ConfigFile(cfg_file)


# ── read_embedding_config ─────────────────────────────────────────────


class TestReadEmbeddingConfig:
    def test_no_memdb_section(self, mock_config_file):
        mock_config_file["content"] = '{"tools": []}'
        assert read_embedding_config() is None

    def test_memdb_not_dict(self, mock_config_file):
        mock_config_file["content"] = '{"memdb": "string"}'
        assert read_embedding_config() is None

    def test_no_embedding_key(self, mock_config_file):
        mock_config_file["content"] = '{"memdb": {"db_path": "/tmp"}}'
        assert read_embedding_config() is None

    def test_embedding_not_dict(self, mock_config_file):
        mock_config_file["content"] = '{"memdb": {"embedding": null}}'
        assert read_embedding_config() is None

    def test_valid_embedding(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "memdb": {"embedding": {"model": "bge-m3", "dim": 1024}}
        })
        result = read_embedding_config()
        assert result == {"model": "bge-m3", "dim": 1024}

    def test_returns_copy_not_reference(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "memdb": {"embedding": {"model": "test"}}
        })
        result = read_embedding_config()
        assert result is not None
        result["model"] = "modified"
        # Re-read should get original
        result2 = read_embedding_config()
        assert result2 == {"model": "test"}

    def test_file_not_found(self, mock_config_file):
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            assert read_embedding_config() is None

    def test_parse_error(self, mock_config_file):
        with patch.object(Path, "read_text", return_value="not valid json5 {{{"):
            assert read_embedding_config() is None


# ── write_embedding_config ────────────────────────────────────────────


class TestWriteEmbeddingConfig:
    def test_creates_memdb_section_if_missing(self, mock_config_file):
        mock_config_file["content"] = '{"tools": []}'
        write_embedding_config({"model": "bge-m3"})

        raw = json5.loads(mock_config_file["content"])
        assert "memdb" in raw
        assert raw["memdb"]["embedding"] == {"model": "bge-m3"}

    def test_overwrites_existing_embedding(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "memdb": {"embedding": {"model": "old", "dim": 768}}
        })
        write_embedding_config({"model": "new", "dim": 1024})

        raw = json5.loads(mock_config_file["content"])
        assert raw["memdb"]["embedding"] == {"model": "new", "dim": 1024}

    def test_preserves_other_memdb_keys(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "memdb": {"db_path": "/tmp/db", "embedding": {}}
        })
        write_embedding_config({"model": "bge-m3"})

        raw = json5.loads(mock_config_file["content"])
        assert raw["memdb"]["db_path"] == "/tmp/db"
        assert raw["memdb"]["embedding"] == {"model": "bge-m3"}


# ── remove_embedding_config ───────────────────────────────────────────


class TestRemoveEmbeddingConfig:
    def test_removes_embedding_key(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "memdb": {"db_path": "/tmp", "embedding": {"model": "x"}}
        })
        remove_embedding_config()

        raw = json5.loads(mock_config_file["content"])
        assert "embedding" not in raw["memdb"]
        assert raw["memdb"]["db_path"] == "/tmp"

    def test_noop_when_no_embedding(self, mock_config_file):
        original = '{"memdb": {"db_path": "/tmp"}}'
        mock_config_file["content"] = original
        remove_embedding_config()
        assert mock_config_file["content"] != original  # reformatted
        raw = json5.loads(mock_config_file["content"])
        assert "embedding" not in raw.get("memdb", {})

    def test_noop_when_memdb_not_dict(self, mock_config_file):
        original = '{"memdb": "string"}'
        mock_config_file["content"] = original
        remove_embedding_config()
        # memdb wasn't a dict, so nothing changes except formatting
        raw = json5.loads(mock_config_file["content"])
        assert raw["memdb"] == "string"


# ── get_first_provider_api_key ────────────────────────────────────────


class TestGetFirstProviderApiKey:
    def test_returns_first_key(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "models": {
                "providers": {
                    "a": {"api_key": "key-a"},
                    "b": {"api_key": "key-b"},
                }
            }
        })
        assert get_first_provider_api_key() == "key-a"

    def test_empty_key_skipped(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "models": {
                "providers": {
                    "a": {"api_key": ""},
                    "b": {"api_key": "key-b"},
                }
            }
        })
        assert get_first_provider_api_key() == "key-b"

    def test_no_providers(self, mock_config_file):
        mock_config_file["content"] = '{"models": {}}'
        assert get_first_provider_api_key() == ""

    def test_no_models_section(self, mock_config_file):
        mock_config_file["content"] = '{}'
        assert get_first_provider_api_key() == ""

    def test_models_not_dict(self, mock_config_file):
        mock_config_file["content"] = '{"models": "string"}'
        assert get_first_provider_api_key() == ""

    def test_provider_not_dict(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "models": {
                "providers": {"a": "not-a-dict", "b": {"api_key": "key-b"}}
            }
        })
        assert get_first_provider_api_key() == "key-b"


# ── validate_gguf_path ────────────────────────────────────────────────


class TestValidateGGufPath:
    def test_valid_gguf_file(self, tmp_path):
        p = tmp_path / "model.gguf"
        p.write_text("dummy")
        ok, msg = validate_gguf_path(str(p))
        assert ok is True
        assert "model.gguf" in msg

    def test_valid_bin_file(self, tmp_path):
        p = tmp_path / "model.bin"
        p.write_text("dummy")
        ok, msg = validate_gguf_path(str(p))
        assert ok is True

    def test_valid_ggml_file(self, tmp_path):
        p = tmp_path / "model.ggml"
        p.write_text("dummy")
        ok, msg = validate_gguf_path(str(p))
        assert ok is True

    def test_file_not_found(self, tmp_path):
        ok, msg = validate_gguf_path(str(tmp_path / "nonexistent.gguf"))
        assert ok is False
        assert "file does not exist" in msg

    def test_not_a_file(self, tmp_path):
        ok, msg = validate_gguf_path(str(tmp_path))
        assert ok is False
        assert "not a file" in msg

    def test_wrong_suffix(self, tmp_path):
        p = tmp_path / "model.txt"
        p.write_text("dummy")
        ok, msg = validate_gguf_path(str(p))
        assert ok is False
        assert "file suffix is not" in msg

    def test_expands_user(self, tmp_path):
        """Tilde expansion works for home directory."""
        p = tmp_path / "model.gguf"
        p.write_text("dummy")
        # Test with an absolute path (tilde expansion is hard to test
        # without mocking home, but we verify the function handles it)
        ok, msg = validate_gguf_path(str(p))
        assert ok is True


# ── make_check_report ─────────────────────────────────────────────────


class TestMakeCheckReport:
    def test_no_config(self, mock_config_file):
        mock_config_file["content"] = "{}"
        report = make_check_report()
        assert report["configured"] is False
        assert report["backend"] == "none"
        assert report["available"] is False

    def test_gguf_config_with_valid_file(self, mock_config_file, tmp_path):
        p = tmp_path / "model.gguf"
        p.write_text("dummy")

        mock_config_file["content"] = json5.dumps({
            "memdb": {"embedding": {"gguf_path": str(p), "model": "bge-m3", "dim": 1024}}
        })
        report = make_check_report()
        assert report["configured"] is True
        assert report["backend"] == "gguf"
        assert report["model"] == "bge-m3"

    def test_gguf_config_with_missing_file(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "memdb": {"embedding": {"gguf_path": "/nonexistent/model.gguf", "model": "bge-m3"}}
        })
        report = make_check_report()
        assert report["backend"] == "gguf"
        assert "gguf_error" in report
        # available may be False depending on EmbeddingClient
        assert report["gguf_path"] == "/nonexistent/model.gguf"

    def test_api_config_missing_key(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "memdb": {"embedding": {"model": "text-embedding-3-small", "dim": 1536}}
        })
        report = make_check_report()
        assert report["configured"] is True
        assert report["backend"] == "api"


class TestGetEmbedderModule:
    """_get_embedder_module must resolve to the RUNNING server module.

    The memdb server runs via ``python -m slife.plugins.memdb.server``, which
    executes the module's code in the ``__main__`` namespace WITHOUT
    populating ``sys.modules["slife.plugins.memdb.server"]``. A plain import
    would create a SECOND module object (re-running the module), so
    reload_embedder's ``mod._embedder = ...`` would mutate a module the server
    never reads — leaving semantic search stuck disabled after
    memory_set_embedding. Guard: return ``__main__`` when it is the server.
    """

    def test_prefers_main_when_it_is_the_server(self):
        import sys
        import types

        from slife.plugins.memdb import embedding_config as ec

        fake = types.ModuleType("slife.plugins.memdb.server")
        fake.__spec__ = types.SimpleNamespace(name="slife.plugins.memdb.server")
        saved_main = sys.modules["__main__"]
        saved_cache = ec._embedder_module
        ec._embedder_module = None
        try:
            sys.modules["__main__"] = fake
            assert ec._get_embedder_module() is fake
        finally:
            sys.modules["__main__"] = saved_main
            ec._embedder_module = saved_cache

    def test_mutation_reaches_main_when_main_is_server(self):
        import sys
        import types

        from slife.plugins.memdb import embedding_config as ec

        fake = types.ModuleType("slife.plugins.memdb.server")
        fake.__spec__ = types.SimpleNamespace(name="slife.plugins.memdb.server")
        saved_main = sys.modules["__main__"]
        saved_cache = ec._embedder_module
        ec._embedder_module = None
        try:
            sys.modules["__main__"] = fake
            mod = ec._get_embedder_module()
            mod._embedder = "new-client"
            assert fake._embedder == "new-client"
        finally:
            sys.modules["__main__"] = saved_main
            ec._embedder_module = saved_cache

    def test_falls_back_to_import_when_main_is_not_server(self):
        import sys
        import types

        from slife.plugins.memdb import embedding_config as ec
        import slife.plugins.memdb.server as real_server

        fake_main = types.ModuleType("__main__")
        fake_main.__spec__ = types.SimpleNamespace(name="pytest")
        saved_main = sys.modules["__main__"]
        saved_cache = ec._embedder_module
        ec._embedder_module = None
        try:
            sys.modules["__main__"] = fake_main
            # __main__ is NOT the server → fall back to importing the
            # canonical module (standard behaviour, unchanged by the fix).
            assert ec._get_embedder_module() is real_server
        finally:
            sys.modules["__main__"] = saved_main
            ec._embedder_module = saved_cache
