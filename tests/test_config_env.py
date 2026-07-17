"""Tests for slife.tools.config_env — env var management tools."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from slife.tools.config_env import (
    _env_section,
    _PLACEHOLDER_PREFIX,
    ConfigEnvSetTool,
    ConfigEnvGetTool,
    ConfigEnvRemoveTool,
)


# ── helpers ──────────────────────────────────────────────────


def _mock_config(data: dict, monkeypatch):
    """Mock read_config and write_config for a test."""
    import slife.tools.config_env

    raw = dict(data)
    monkeypatch.setattr(slife.tools.config_env, "read_config", lambda path: raw)
    written = []
    monkeypatch.setattr(
        slife.tools.config_env, "write_config",
        lambda path, r: written.append(dict(r)),
    )
    return raw, written


def _mock_credstore(monkeypatch):
    """Mock credstore to an in-memory dict."""
    data = {}

    def _get(key):
        return data.get(key)

    def _delete(key):
        return data.pop(key, None) is not None

    # Patch the credstore package — config_env imports from it locally
    import credstore
    monkeypatch.setattr(credstore, "get_credential", _get)
    monkeypatch.setattr(credstore, "delete_credential", _delete)

    # Also patch config's helper
    import slife.config
    monkeypatch.setattr(slife.config, "_try_credstore_lookup", _get)

    return data


# ── _env_section ─────────────────────────────────────────────


class TestEnvSection:
    def test_creates_env_section_if_missing(self):
        raw = {}
        env = _env_section(raw)
        assert env == {}
        assert "env" in raw

    def test_returns_existing_env(self):
        existing = {"A": "1"}
        raw = {"env": existing}
        env = _env_section(raw)
        assert env is existing

    def test_converts_non_dict_to_dict(self):
        raw = {"env": "not a dict"}
        env = _env_section(raw)
        assert env == {}
        assert raw["env"] == {}


# ── ConfigEnvSetTool ─────────────────────────────────────────


class TestConfigEnvSetTool:
    @pytest.mark.asyncio
    async def test_secret_key_with_value_rejected(self, monkeypatch):
        """API_KEY with plaintext value → REJECTED, directs to CLI."""
        raw, written = _mock_config({}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvSetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="TAVILY_API_KEY", value="sk-abc123")

        # Must reject — never accept secret values
        assert "[REJECTED]" in result
        assert "credstore set" in result
        assert "sk-abc123" not in result  # value never exposed
        assert len(written) == 0  # nothing written to config

    @pytest.mark.asyncio
    async def test_secret_key_no_value_writes_ref(self, monkeypatch):
        """Secret key without value → writes ${VAR} ref, directs to CLI."""
        raw, written = _mock_config({}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvSetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="MY_API_KEY")

        assert raw["env"]["MY_API_KEY"] == "${MY_API_KEY}"
        assert "credstore set" in result
        assert len(written) == 1

    @pytest.mark.asyncio
    async def test_non_secret_key_writes_value(self, monkeypatch):
        """Non-secret keys still write directly."""
        raw, written = _mock_config({}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvSetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="EDITOR", value="vim")

        assert raw["env"]["EDITOR"] == "vim"
        assert os.environ["EDITOR"] == "vim"
        assert "[OK]" in result

    @pytest.mark.asyncio
    async def test_non_secret_placeholder(self, monkeypatch):
        raw, written = _mock_config({}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvSetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="MY_SETTING")

        assert raw["env"]["MY_SETTING"] == "<YOUR_MY_SETTING>"
        assert "placeholder" in result

    @pytest.mark.asyncio
    async def test_overwrite_existing(self, monkeypatch):
        raw, written = _mock_config({"env": {"EDITOR": "nano"}}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvSetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="EDITOR", value="vim")
        assert raw["env"]["EDITOR"] == "vim"


# ── ConfigEnvGetTool ─────────────────────────────────────────


class TestConfigEnvGetTool:
    @pytest.mark.asyncio
    async def test_get_from_config_fallback(self, monkeypatch):
        """When not in environ or credstore, falls back to config value."""
        raw, _ = _mock_config({"env": {"MY_SETTING": "config_val"}}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="MY_SETTING")
        assert "MY_SETTING" in result
        assert "config_val" in result
        assert "[slife.json5]" in result

    @pytest.mark.asyncio
    async def test_get_from_credstore(self, monkeypatch):
        """When in credstore, show it with masking."""
        raw, _ = _mock_config({"env": {"API_KEY": "${API_KEY}"}}, monkeypatch)
        cred = _mock_credstore(monkeypatch)
        cred["API_KEY"] = "sk-secret-key-long"

        tool = ConfigEnvGetTool(config_path=Path("test.json5"))
        result = await tool.execute(key="API_KEY")

        assert "API_KEY" in result
        assert "[credstore" in result
        # Value must be masked
        assert "sk-secret-key-long" not in result

    @pytest.mark.asyncio
    async def test_get_from_shell_takes_priority(self, monkeypatch):
        """os.environ takes priority over credstore and config."""
        raw, _ = _mock_config({"env": {"MY_VAR": "from_config"}}, monkeypatch)
        cred = _mock_credstore(monkeypatch)
        cred["MY_VAR"] = "from_credstore"
        os.environ["MY_VAR"] = "from_shell"

        try:
            tool = ConfigEnvGetTool(config_path=Path("test.json5"))
            result = await tool.execute(key="MY_VAR")
            assert "[shell" in result
            assert "active" in result
        finally:
            os.environ.pop("MY_VAR", None)

    @pytest.mark.asyncio
    async def test_get_missing_key(self, monkeypatch):
        raw, _ = _mock_config({"env": {}}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="NOT_THERE")
        assert "not set" in result

    @pytest.mark.asyncio
    async def test_list_all_vars(self, monkeypatch):
        raw, _ = _mock_config({"env": {"A": "val_a", "B": "val_b"}}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute()
        assert "A" in result
        assert "val_a" in result
        assert "B" in result
        assert "val_b" in result

    @pytest.mark.asyncio
    async def test_list_from_credstore(self, monkeypatch):
        raw, _ = _mock_config({"env": {"API_KEY": "${API_KEY}"}}, monkeypatch)
        cred = _mock_credstore(monkeypatch)
        cred["API_KEY"] = "sk-secret-12345678"

        tool = ConfigEnvGetTool(config_path=Path("test.json5"))
        result = await tool.execute(key="API_KEY")

        assert "[credstore" in result
        # Must be masked
        assert "sk-secret-12345678" not in result

    @pytest.mark.asyncio
    async def test_list_empty(self, monkeypatch):
        raw, _ = _mock_config({}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))
        result = await tool.execute()
        assert "No environment variables" in result


# ── ConfigEnvRemoveTool ──────────────────────────────────────


class TestConfigEnvRemoveTool:
    @pytest.mark.asyncio
    async def test_remove_existing_var(self, monkeypatch):
        raw, written = _mock_config({"env": {"TO_REMOVE": "bye"}}, monkeypatch)
        _mock_credstore(monkeypatch)
        os.environ["TO_REMOVE"] = "bye"
        tool = ConfigEnvRemoveTool(config_path=Path("test.json5"))

        result = await tool.execute(key="TO_REMOVE")
        assert "TO_REMOVE" not in raw["env"]
        assert "TO_REMOVE" not in os.environ
        assert "[OK]" in result
        assert len(written) == 1

    @pytest.mark.asyncio
    async def test_remove_missing_var(self, monkeypatch):
        raw, written = _mock_config({"env": {}}, monkeypatch)
        _mock_credstore(monkeypatch)
        tool = ConfigEnvRemoveTool(config_path=Path("test.json5"))

        result = await tool.execute(key="NOT_THERE")
        assert "nothing to remove" in result

    @pytest.mark.asyncio
    async def test_remove_from_credstore(self, monkeypatch):
        raw, _ = _mock_config({"env": {"SECRET_KEY": "${SECRET_KEY}"}}, monkeypatch)
        cred = _mock_credstore(monkeypatch)
        cred["SECRET_KEY"] = "some-secret"
        tool = ConfigEnvRemoveTool(config_path=Path("test.json5"))

        result = await tool.execute(key="SECRET_KEY")
        assert "[OK]" in result
        assert "SECRET_KEY" not in cred

    @pytest.mark.asyncio
    async def test_remove_from_os_environ_only(self, monkeypatch):
        raw, _ = _mock_config({"env": {}}, monkeypatch)
        _mock_credstore(monkeypatch)
        os.environ["ONLY_IN_ENV"] = "temp"
        tool = ConfigEnvRemoveTool(config_path=Path("test.json5"))

        result = await tool.execute(key="ONLY_IN_ENV")
        assert "[OK]" in result
        assert "ONLY_IN_ENV" not in os.environ
