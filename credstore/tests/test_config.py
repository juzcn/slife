"""Tests for credstore._config — config file loading and path resolution."""

import importlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from credstore._config import load_config, get_cryptfile_path, _is_slife_dev


class TestLoadConfig:
    """Tests for load_config."""

    def test_no_config_files_returns_empty(self):
        with patch("credstore._config._DEFAULT_CONFIG_FILES", []):
            result = load_config()
            assert result == {}

    def test_file_not_found_returns_empty(self):
        with patch.object(Path, "exists", return_value=False):
            result = load_config()
            assert result == {}

    def test_loads_valid_json5(self, tmp_path):
        config_file = tmp_path / "credstore.json5"
        config_file.write_text('{"cryptfile_path": "/tmp/secrets.crypt"}', encoding="utf-8")

        with patch("credstore._config._DEFAULT_CONFIG_FILES", [config_file]):
            result = load_config()
            assert result["cryptfile_path"] == "/tmp/secrets.crypt"

    def test_parse_error_returns_empty(self, tmp_path):
        config_file = tmp_path / "credstore.json5"
        config_file.write_text("{invalid json5", encoding="utf-8")

        with patch("credstore._config._DEFAULT_CONFIG_FILES", [config_file]):
            result = load_config()
            assert result == {}

    def test_non_dict_json_returns_empty(self, tmp_path):
        config_file = tmp_path / "credstore.json5"
        config_file.write_text('"just a string"', encoding="utf-8")

        with patch("credstore._config._DEFAULT_CONFIG_FILES", [config_file]):
            result = load_config()
            assert result == {}

    def test_first_found_file_wins(self, tmp_path):
        first = tmp_path / "credstore.json5"
        first.write_text('{"key": "first"}', encoding="utf-8")
        second = tmp_path / "second.json5"
        second.write_text('{"key": "second"}', encoding="utf-8")

        with patch("credstore._config._DEFAULT_CONFIG_FILES", [first, second]):
            result = load_config()
            assert result["key"] == "first"

    def test_falls_through_to_second_file(self, tmp_path):
        first = tmp_path / "missing.json5"
        second = tmp_path / "present.json5"
        second.write_text('{"key": "second"}', encoding="utf-8")

        with patch("credstore._config._DEFAULT_CONFIG_FILES", [first, second]):
            result = load_config()
            assert result["key"] == "second"


class TestIsSlifeDev:
    """Tests for _is_slife_dev."""

    def test_is_slife_project(self):
        import tomllib
        with patch.object(Path, "read_text", return_value='[project]\nname = "slife"\n'), \
             patch.object(tomllib, "loads", return_value={"project": {"name": "slife"}}):
            assert _is_slife_dev() is True

    def test_not_slife_project(self):
        import tomllib
        with patch.object(Path, "read_text", return_value='[project]\nname = "other"\n'), \
             patch.object(tomllib, "loads", return_value={"project": {"name": "other"}}):
            assert _is_slife_dev() is False

    def test_missing_pyproject_returns_false(self):
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            assert _is_slife_dev() is False

    def test_malformed_toml_returns_false(self):
        import tomllib
        with patch.object(Path, "read_text", return_value="not valid toml"), \
             patch.object(tomllib, "loads", side_effect=ValueError("bad toml")):
            assert _is_slife_dev() is False

    def test_missing_project_section_returns_false(self):
        import tomllib
        with patch.object(Path, "read_text", return_value='[tool]\nkey = "val"\n'), \
             patch.object(tomllib, "loads", return_value={"tool": {"key": "val"}}):
            assert _is_slife_dev() is False


class TestGetCryptfilePath:
    """Tests for get_cryptfile_path resolution."""

    def test_env_var_highest_priority(self):
        with patch.dict(os.environ, {"CREDSTORE_FILE": "/env/path/credentials.crypt"}):
            result = get_cryptfile_path()
            assert result.replace("\\", "/") == "/env/path/credentials.crypt"

    def test_config_file_second_priority(self):
        cfg_path = os.path.join("cfg", "path", "secrets.crypt")
        with patch.dict(os.environ, {}, clear=True):
            with patch("credstore._config.load_config", return_value={"cryptfile_path": cfg_path}):
                result = get_cryptfile_path()
                assert "cfg" in result
                assert result.endswith("secrets.crypt")

    def test_config_path_expands_user(self, monkeypatch):
        monkeypatch.delenv("CREDSTORE_FILE", raising=False)
        monkeypatch.setenv("USERPROFILE", "C:\\Users\\testuser")
        with patch("credstore._config.load_config", return_value={"cryptfile_path": "~/my/secrets.crypt"}):
            result = get_cryptfile_path()
            assert "~" not in result
            assert "testuser" in result

    def test_dev_default(self, monkeypatch):
        monkeypatch.delenv("CREDSTORE_FILE", raising=False)
        with patch("credstore._config.load_config", return_value={}):
            with patch("credstore._config._is_slife_dev", return_value=True):
                result = get_cryptfile_path()
                assert result.endswith("credentials.crypt")

    def test_production_default(self, monkeypatch):
        monkeypatch.delenv("CREDSTORE_FILE", raising=False)
        monkeypatch.setenv("USERPROFILE", "C:\\Users\\testuser")
        with patch("credstore._config.load_config", return_value={}):
            with patch("credstore._config._is_slife_dev", return_value=False):
                result = get_cryptfile_path()
                assert ".credstore" in result
                assert result.endswith("credentials.crypt")


# ── Fallback _is_slife_dev (standalone credstore) ──────────────────────────


class TestIsSlifeDevFallback:
    """Tests for the fallback _is_slife_dev when slife.paths is unavailable.

    credstore._config normally imports _is_slife_dev from slife.paths.
    These tests force that import to fail so the standalone fallback
    implementation (lines 23-36 of _config.py) is exercised.
    """

    @pytest.fixture(autouse=True)
    def _restore_config(self):
        """Reload credstore._config to its original state after each test."""
        yield
        import credstore._config as cfg
        importlib.reload(cfg)

    @staticmethod
    def _reload_without_slife(*, block_tomllib=False):
        """Reload credstore._config with slife.paths import blocked.

        Returns the reloaded module so tests can call its _is_slife_dev.
        """
        import builtins
        import credstore._config as cfg

        _orig = builtins.__import__

        def _block(name, globals=None, locals=None, fromlist=(), level=0):
            if name in ("slife", "slife.paths"):
                raise ImportError(f"No module named '{name}'")
            if block_tomllib and name == "tomllib":
                raise ImportError(f"No module named 'tomllib'")
            return _orig(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=_block):
            importlib.reload(cfg)
        return cfg

    def test_no_tomllib_returns_false(self):
        """When tomllib is also unavailable, _is_slife_dev() returns False."""
        cfg = self._reload_without_slife(block_tomllib=True)
        assert cfg._is_slife_dev() is False

    def test_missing_pyproject_returns_false(self):
        """When pyproject.toml does not exist."""
        cfg = self._reload_without_slife()
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            assert cfg._is_slife_dev() is False

    def test_not_slife_project_name(self):
        """When pyproject.toml has a different project name."""
        cfg = self._reload_without_slife()
        import tomllib
        with patch.object(Path, "read_text", return_value='[project]\nname = "other"\n'), \
             patch.object(tomllib, "loads", return_value={"project": {"name": "other"}}):
            assert cfg._is_slife_dev() is False

    def test_malformed_toml(self):
        """When pyproject.toml cannot be parsed."""
        cfg = self._reload_without_slife()
        with patch.object(Path, "read_text", return_value="not valid toml"):
            assert cfg._is_slife_dev() is False

    def test_is_slife_project_via_fallback(self):
        """When pyproject.toml has project.name == 'slife' (via fallback)."""
        cfg = self._reload_without_slife()
        import tomllib
        with patch.object(Path, "read_text", return_value='[project]\nname = "slife"\n'), \
             patch.object(tomllib, "loads", return_value={"project": {"name": "slife"}}):
            assert cfg._is_slife_dev() is True
