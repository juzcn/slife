"""Tests for Slife.tools._config_io — shared config file read/write helpers."""

import json5
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

from slife.tools._config_io import (
    now_iso,
    with_fetched_at,
    read_config,
    write_config,
    _ConfigPathMixin,
)


# ── now_iso ─────────────────────────────────────────────────────────────────


class TestNowIso:
    """Tests for now_iso."""

    def test_returns_iso_format(self):
        result = now_iso()
        assert "T" in result
        assert "+" in result or "Z" in result

    def test_returns_different_values_on_subsequent_calls(self):
        import time
        result1 = now_iso()
        time.sleep(0.001)
        result2 = now_iso()
        # Should differ by at least the sleep
        assert result1 != result2


# ── with_fetched_at ─────────────────────────────────────────────────────────


class TestWithFetchedAt:
    """Tests for with_fetched_at."""

    def test_adds_timestamp_to_dict(self):
        source = {"name": "myserver", "command": "python"}
        result = with_fetched_at(source)
        assert result["name"] == "myserver"
        assert result["command"] == "python"
        assert "fetched_at" in result
        assert "T" in result["fetched_at"]

    def test_original_dict_not_mutated(self):
        source = {"name": "original"}
        result = with_fetched_at(source)
        assert "fetched_at" not in source
        assert "fetched_at" in result

    def test_none_returns_none(self):
        assert with_fetched_at(None) is None

    def test_empty_dict_returns_none(self):
        assert with_fetched_at({}) is None

    def test_existing_fetched_at_preserved(self):
        """setdefault preserves an existing fetched_at key."""
        source = {"name": "test", "fetched_at": "old_value"}
        result = with_fetched_at(source)
        # setdefault won't override existing keys
        assert result["fetched_at"] == "old_value"


# ── read_config ─────────────────────────────────────────────────────────────


class TestReadConfig:
    """Tests for read_config."""

    def test_reads_valid_json5(self, tmp_path):
        path = tmp_path / "config.json5"
        path.write_text('{"key": "value", "num": 42}', encoding="utf-8")
        result = read_config(path)
        assert result == {"key": "value", "num": 42}

    def test_file_not_found_returns_empty(self, tmp_path):
        path = tmp_path / "nonexistent.json5"
        result = read_config(path)
        assert result == {}

    def test_parse_error_returns_empty(self, tmp_path):
        path = tmp_path / "broken.json5"
        path.write_text("{invalid json5!!!", encoding="utf-8")
        result = read_config(path)
        assert result == {}


# ── write_config ────────────────────────────────────────────────────────────


class TestWriteConfig:
    """Tests for write_config."""

    def test_writes_json5_with_indent(self, tmp_path):
        path = tmp_path / "output.json5"
        data = {"key": "value", "list": [1, 2, 3]}
        write_config(path, data)
        result = json5.loads(path.read_text(encoding="utf-8"))
        assert result == data


# ── _ConfigPathMixin ────────────────────────────────────────────────────────


class TestConfigPathMixin:
    """Tests for _ConfigPathMixin."""

    def test_default_path_is_slife_json5(self):
        mixin = _ConfigPathMixin()
        assert mixin._config_path == Path("slife.json5")

    def test_custom_path(self):
        mixin = _ConfigPathMixin(config_path=Path("/custom/path.json5"))
        assert mixin._config_path == Path("/custom/path.json5")

    def test_from_config_with_config(self, sample_config):
        """from_config extracts path from Config._path."""
        sample_config._path = Path("/my/config.json5")
        instance = _ConfigPathMixin.from_config({}, sample_config)
        assert instance._config_path == Path("/my/config.json5")

    def test_from_config_without_config(self):
        """from_config falls back to default when config is None."""
        instance = _ConfigPathMixin.from_config({}, None)
        assert instance._config_path == Path("slife.json5")

    def test_from_config_without_path(self, sample_config):
        """from_config falls back to default when config._path is None."""
        sample_config._path = None
        instance = _ConfigPathMixin.from_config({}, sample_config)
        assert instance._config_path == Path("slife.json5")
