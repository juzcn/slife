"""Tests for slife/plugins/memdb/embedding_config.py."""

import pytest; pytestmark = pytest.mark.unit


import json5
from pathlib import Path
from unittest.mock import patch

import pytest

from slife.plugins.memdb.embedding_config import (
    read_embedding_config,
    write_embedding_config,
    get_active_endpoint,
    make_check_report,
    _active_endpoint,
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
    """Redirect embedding_config to a throwaway config file."""
    import slife.plugins.memdb.embedding_config as embedding_config

    cfg_file = tmp_path / "slife.json5"
    cfg_file.write_text("{}", encoding="utf-8")
    with patch.object(embedding_config, "_CONFIG_PATH", cfg_file):
        yield _ConfigFile(cfg_file)


def _emb_config(*, providers=None, active="", enabled=True) -> dict:
    """Build an embeddings section dict for tests."""
    cfg: dict = {"enabled": enabled}
    if providers is not None:
        cfg["providers"] = providers
    if active:
        cfg["active_model"] = active
    return cfg


# ── read_embedding_config ─────────────────────────────────────────────


class TestReadEmbeddingConfig:
    def test_no_embeddings_section(self, mock_config_file):
        mock_config_file["content"] = '{"tools": []}'
        assert read_embedding_config() is None

    def test_embeddings_not_dict(self, mock_config_file):
        mock_config_file["content"] = '{"embeddings": "string"}'
        assert read_embedding_config() is None

    def test_valid_embeddings(self, mock_config_file):
        cfg = _emb_config(providers={"p1": {"base_url": "x"}}, active="p1")
        mock_config_file["content"] = json5.dumps({"embeddings": cfg})
        result = read_embedding_config()
        assert result == cfg

    def test_returns_copy_not_reference(self, mock_config_file):
        cfg = _emb_config(providers={"p1": {"base_url": "x"}}, active="p1")
        mock_config_file["content"] = json5.dumps({"embeddings": cfg})
        result = read_embedding_config()
        assert result is not None
        result["enabled"] = False
        # Re-read should get original
        result2 = read_embedding_config()
        assert result2["enabled"] is True

    def test_file_not_found(self, mock_config_file):
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            assert read_embedding_config() is None

    def test_parse_error(self, mock_config_file):
        with patch.object(Path, "read_text", return_value="not valid json5 {{{"):
            assert read_embedding_config() is None


# ── write_embedding_config ────────────────────────────────────────────


class TestWriteEmbeddingConfig:
    def test_creates_embeddings_section_if_missing(self, mock_config_file):
        mock_config_file["content"] = '{"tools": []}'
        cfg = _emb_config(providers={"p1": {"base_url": "x"}}, active="p1")
        write_embedding_config(cfg)

        raw = json5.loads(mock_config_file["content"])
        assert "embeddings" in raw
        assert raw["embeddings"] == cfg

    def test_overwrites_existing_embeddings(self, mock_config_file):
        mock_config_file["content"] = json5.dumps({
            "embeddings": {"providers": {"old": {}}, "active_model": "old"},
        })
        cfg = _emb_config(providers={"new": {"base_url": "y"}}, active="new")
        write_embedding_config(cfg)

        raw = json5.loads(mock_config_file["content"])
        assert raw["embeddings"] == cfg


# ── _active_endpoint / get_active_endpoint ────────────────────────────


class TestActiveEndpoint:
    def test_no_section(self, mock_config_file):
        mock_config_file["content"] = "{}"
        ep = get_active_endpoint()
        assert ep["provider"] == ""
        assert ep["base_url"] == ""
        assert ep["model"] == ""

    def test_bare_provider(self, mock_config_file):
        cfg = _emb_config(
            providers={"p1": {"base_url": "http://x/v1", "api_key": "k"}},
            active="p1",
        )
        mock_config_file["content"] = json5.dumps({"embeddings": cfg})
        ep = get_active_endpoint()
        assert ep["provider"] == "p1"
        assert ep["base_url"] == "http://x/v1"
        assert ep["api_key"] == "k"
        assert ep["model"] == ""

    def test_provider_with_model(self, mock_config_file):
        cfg = _emb_config(
            providers={"p1": {
                "base_url": "http://x/v1", "api_key": "k",
                "models": [{"model": "m1", "dim": 768}, {"model": "m2"}],
            }},
            active="p1/m1",
        )
        mock_config_file["content"] = json5.dumps({"embeddings": cfg})
        ep = get_active_endpoint()
        assert ep["provider"] == "p1"
        assert ep["model"] == "m1"
        assert ep["dim"] == 768

    def test_provider_model_without_dim(self, mock_config_file):
        cfg = _emb_config(
            providers={"p1": {"base_url": "http://x/v1",
                              "models": [{"model": "m1"}]}},
            active="p1/m1",
        )
        mock_config_file["content"] = json5.dumps({"embeddings": cfg})
        ep = get_active_endpoint()
        assert ep["model"] == "m1"
        assert ep["dim"] == 0

    def test_active_provider_missing_falls_back_first(self, mock_config_file):
        cfg = _emb_config(
            providers={"p1": {"base_url": "http://x/v1"}, "p2": {"base_url": "http://y/v1"}},
            active="nonexistent",
        )
        mock_config_file["content"] = json5.dumps({"embeddings": cfg})
        ep = get_active_endpoint()
        assert ep["provider"] == "p1"

    def test_no_active_uses_first_provider(self, mock_config_file):
        cfg = _emb_config(
            providers={"p1": {"base_url": "http://x/v1"}, "p2": {"base_url": "http://y/v1"}},
        )
        mock_config_file["content"] = json5.dumps({"embeddings": cfg})
        ep = get_active_endpoint()
        assert ep["provider"] == "p1"


# ── make_check_report ─────────────────────────────────────────────────


class TestMakeCheckReport:
    def test_no_config(self, mock_config_file):
        mock_config_file["content"] = "{}"
        report = make_check_report()
        assert report["configured"] is False
        assert report["provider"] == ""
        assert report["available"] is False

    def test_provider_no_base_url(self, mock_config_file):
        cfg = _emb_config(providers={"p1": {}}, active="p1")
        mock_config_file["content"] = json5.dumps({"embeddings": cfg})
        report = make_check_report()
        assert report["configured"] is True
        assert report["available"] is False
        assert "no base_url" in report["hint"]

    def test_configured_available(self, mock_config_file):
        cfg = _emb_config(
            providers={"p1": {"base_url": "http://127.0.0.1:8000/v1", "api_key": "local"}},
            active="p1",
        )
        mock_config_file["content"] = json5.dumps({"embeddings": cfg})
        with patch(
            "slife.plugins.memdb.embeddings.EmbeddingClient"
        ) as MockClient:
            mock_client = MockClient.from_config.return_value
            mock_client.available = True
            mock_client.dimension = 1024
            mock_client._model = "bge-m3"
            report = make_check_report()
            assert report["available"] is True
            assert report["provider"] == "p1"
            assert report["dimension"] == 1024
