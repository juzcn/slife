"""Tests for credstore._config — cryptfile path resolution."""

import importlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

from credstore._config import get_cryptfile_path

# _config.py conditionally defines is_slife_dev; Pylance can't resolve the
# symbol across the try/except.  Use getattr so it works at runtime and
# the static checker doesn't complain.
is_slife_dev = getattr(
    __import__("credstore._config", fromlist=["is_slife_dev"]),
    "is_slife_dev",
)


class TestIsSlifeDev:
    """Tests for is_slife_dev."""

    def test_is_slife_project(self):
        import tomllib
        with patch.object(Path, "read_text", return_value='[project]\nname = "slife"\n'), \
             patch.object(tomllib, "loads", return_value={"project": {"name": "slife"}}):
            assert is_slife_dev() is True

    def test_not_slife_project(self):
        import tomllib
        with patch.object(Path, "read_text", return_value='[project]\nname = "other"\n'), \
             patch.object(tomllib, "loads", return_value={"project": {"name": "other"}}):
            assert is_slife_dev() is False

    def test_missing_pyproject_returns_false(self):
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            assert is_slife_dev() is False

    def test_malformed_toml_returns_false(self):
        import tomllib
        with patch.object(Path, "read_text", return_value="not valid toml"), \
             patch.object(tomllib, "loads", side_effect=ValueError("bad toml")):
            assert is_slife_dev() is False

    def test_missing_project_section_returns_false(self):
        import tomllib
        with patch.object(Path, "read_text", return_value='[tool]\nkey = "val"\n'), \
             patch.object(tomllib, "loads", return_value={"tool": {"key": "val"}}):
            assert is_slife_dev() is False


class TestGetCryptfilePath:
    """Tests for get_cryptfile_path resolution."""

    def test_env_var_highest_priority(self):
        with patch.dict(os.environ, {"CREDSTORE_FILE": "/env/path/credentials.crypt"}):
            result = get_cryptfile_path()
            assert result.replace("\\", "/") == "/env/path/credentials.crypt"

    def test_dev_default(self, monkeypatch):
        monkeypatch.delenv("CREDSTORE_FILE", raising=False)
        with patch("credstore._config.is_slife_dev", return_value=True):
            result = get_cryptfile_path()
            assert result.endswith("credentials.crypt")

    def test_production_default(self, monkeypatch):
        monkeypatch.delenv("CREDSTORE_FILE", raising=False)
        monkeypatch.setenv("USERPROFILE", "C:\\Users\\testuser")
        with patch("credstore._config.is_slife_dev", return_value=False):
            result = get_cryptfile_path()
            assert ".credstore" in result
            assert result.endswith("credentials.crypt")


# ── Fallback is_slife_dev (standalone credstore) ──────────────────────────


@pytest.mark.slow
class TestIsSlifeDevFallback:
    """Tests for the fallback is_slife_dev when slife.paths is unavailable.

    credstore._config normally imports is_slife_dev from slife.paths.
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

        Returns the reloaded module so tests can call its is_slife_dev.
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
        """When tomllib is also unavailable, is_slife_dev() returns False."""
        cfg = self._reload_without_slife(block_tomllib=True)
        assert getattr(cfg, "is_slife_dev")() is False

    def test_missing_pyproject_returns_false(self):
        """When pyproject.toml does not exist."""
        cfg = self._reload_without_slife()
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            assert getattr(cfg, "is_slife_dev")() is False

    def test_not_slife_project_name(self):
        """When pyproject.toml has a different project name."""
        cfg = self._reload_without_slife()
        import tomllib
        with patch.object(Path, "read_text", return_value='[project]\nname = "other"\n'), \
             patch.object(tomllib, "loads", return_value={"project": {"name": "other"}}):
            assert getattr(cfg, "is_slife_dev")() is False

    def test_malformed_toml(self):
        """When pyproject.toml cannot be parsed."""
        cfg = self._reload_without_slife()
        with patch.object(Path, "read_text", return_value="not valid toml"):
            assert getattr(cfg, "is_slife_dev")() is False

    def test_is_slife_project_via_fallback(self):
        """When pyproject.toml has project.name == 'slife' (via fallback)."""
        cfg = self._reload_without_slife()
        import tomllib
        with patch.object(Path, "read_text", return_value='[project]\nname = "slife"\n'), \
             patch.object(tomllib, "loads", return_value={"project": {"name": "slife"}}):
            assert getattr(cfg, "is_slife_dev")() is True
