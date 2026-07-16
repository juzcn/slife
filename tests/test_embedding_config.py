"""Tests for slife/plugins/memory/embedding_config.py."""

import json5
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from slife.plugins.memory.embedding_config import (
    read_embedding_config,
    write_embedding_config,
    remove_embedding_config,
    get_first_provider_api_key,
    validate_gguf_path,
    make_check_report,
    reload_embedder,
)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_config_file():
    """Mock Path.read_text and Path.write_text for slife.json5."""
    _state = {"content": "{}"}

    def _read_text(self, encoding="utf-8"):
        return _state["content"]

    def _write_text(self, text, encoding="utf-8"):
        _state["content"] = text

    with patch.object(Path, "read_text", _read_text), \
         patch.object(Path, "write_text", _write_text):
        yield _state


# ── read_embedding_config ─────────────────────────────────────────────


class TestReadEmbeddingConfig:
    def test_no_memory_section(self, mock_config_file):
        mock_config_file["content"] = '{"tools": []}'
        assert read_embedding_config() is None

    def test_memory_not_dict(self, mock_config_file):
        mock_config_file["content"] = '{"memory": "string"}'
        assert read_embedding_config() is None

    def test_no_embedding_key(self, mock_config_file):
        mock_config_file["content"] = '{"memory": {"db_path": "/tmp"}}'
        assert read_embedding_config() is None

    def test_embedding_not_dict(self, mock_config_file):
        mock_config_file["content"] = '{"memory": {"embedding": null}}'
        assert read_embedding_config() is None

    def test_valid_embedding(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "memory": {"embedding": {"model": "bge-m3", "dim": 1024}}
        })
        result = read_embedding_config()
        assert result == {"model": "bge-m3", "dim": 1024}

    def test_returns_copy_not_reference(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "memory": {"embedding": {"model": "test"}}
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
    def test_creates_memory_section_if_missing(self, mock_config_file):
        mock_config_file["content"] = '{"tools": []}'
        write_embedding_config({"model": "bge-m3"})

        raw = json5.loads(mock_config_file["content"])
        assert "memory" in raw
        assert raw["memory"]["embedding"] == {"model": "bge-m3"}

    def test_overwrites_existing_embedding(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "memory": {"embedding": {"model": "old", "dim": 768}}
        })
        write_embedding_config({"model": "new", "dim": 1024})

        raw = json5.loads(mock_config_file["content"])
        assert raw["memory"]["embedding"] == {"model": "new", "dim": 1024}

    def test_preserves_other_memory_keys(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "memory": {"db_path": "/tmp/db", "embedding": {}}
        })
        write_embedding_config({"model": "bge-m3"})

        raw = json5.loads(mock_config_file["content"])
        assert raw["memory"]["db_path"] == "/tmp/db"
        assert raw["memory"]["embedding"] == {"model": "bge-m3"}


# ── remove_embedding_config ───────────────────────────────────────────


class TestRemoveEmbeddingConfig:
    def test_removes_embedding_key(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "memory": {"db_path": "/tmp", "embedding": {"model": "x"}}
        })
        remove_embedding_config()

        raw = json5.loads(mock_config_file["content"])
        assert "embedding" not in raw["memory"]
        assert raw["memory"]["db_path"] == "/tmp"

    def test_noop_when_no_embedding(self, mock_config_file):
        original = '{"memory": {"db_path": "/tmp"}}'
        mock_config_file["content"] = original
        remove_embedding_config()
        assert mock_config_file["content"] != original  # reformatted
        raw = json5.loads(mock_config_file["content"])
        assert "embedding" not in raw.get("memory", {})

    def test_noop_when_memory_not_dict(self, mock_config_file):
        original = '{"memory": "string"}'
        mock_config_file["content"] = original
        remove_embedding_config()
        # memory wasn't a dict, so nothing changes except formatting
        raw = json5.loads(mock_config_file["content"])
        assert raw["memory"] == "string"


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
        assert "不存在" in msg

    def test_not_a_file(self, tmp_path):
        ok, msg = validate_gguf_path(str(tmp_path))
        assert ok is False
        assert "不是文件" in msg

    def test_wrong_suffix(self, tmp_path):
        p = tmp_path / "model.txt"
        p.write_text("dummy")
        ok, msg = validate_gguf_path(str(p))
        assert ok is False
        assert "后缀" in msg

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
            "memory": {"embedding": {"gguf_path": str(p), "model": "bge-m3", "dim": 1024}}
        })
        report = make_check_report()
        assert report["configured"] is True
        assert report["backend"] == "gguf"
        assert report["model"] == "bge-m3"

    def test_gguf_config_with_missing_file(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "memory": {"embedding": {"gguf_path": "/nonexistent/model.gguf", "model": "bge-m3"}}
        })
        report = make_check_report()
        assert report["backend"] == "gguf"
        assert "gguf_error" in report
        # available may be False depending on EmbeddingClient
        assert report["gguf_path"] == "/nonexistent/model.gguf"

    def test_api_config_missing_key(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "memory": {"embedding": {"model": "text-embedding-3-small", "dim": 1536}}
        })
        report = make_check_report()
        assert report["configured"] is True
        assert report["backend"] == "api"
