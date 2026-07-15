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


# ── helper: mock read/write ─────────────────────────────────────────────────

def _mock_config(data: dict, monkeypatch):
    """Mock read_config and write_config for a test."""
    import slife.tools.config_env

    raw = dict(data)
    monkeypatch.setattr(
        slife.tools.config_env, "read_config", lambda path: raw,
    )
    written = []
    monkeypatch.setattr(
        slife.tools.config_env,
        "write_config",
        lambda path, r: written.append(dict(r)),
    )
    return raw, written


# ── _env_section ────────────────────────────────────────────────────────────


class TestEnvSection:
    """Tests for _env_section helper."""

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


# ── ConfigEnvSetTool ────────────────────────────────────────────────────────


class TestConfigEnvSetTool:
    """Tests for ConfigEnvSetTool."""

    @pytest.mark.asyncio
    async def test_set_new_var(self, monkeypatch):
        raw, written = _mock_config({}, monkeypatch)
        tool = ConfigEnvSetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="TAVILY_API_KEY", value="sk-abc123")

        assert "TAVILY_API_KEY" in raw["env"]
        assert raw["env"]["TAVILY_API_KEY"] == "sk-abc123"
        assert os.environ["TAVILY_API_KEY"] == "sk-abc123"
        assert "[OK]" in result
        assert "active immediately" in result
        assert len(written) == 1

    @pytest.mark.asyncio
    async def test_set_placeholder_when_no_value(self, monkeypatch):
        raw, written = _mock_config({}, monkeypatch)
        tool = ConfigEnvSetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="MY_API_KEY", value="")

        assert "MY_API_KEY" in raw["env"]
        assert raw["env"]["MY_API_KEY"] == "<YOUR_MY_API_KEY>"
        assert "[OK]" in result
        assert "placeholder" in result

    @pytest.mark.asyncio
    async def test_set_placeholder_strips_brackets(self, monkeypatch):
        raw, written = _mock_config({}, monkeypatch)
        tool = ConfigEnvSetTool(config_path=Path("test.json5"))

        await tool.execute(key="<MY_KEY>", value="")

        assert raw["env"]["<MY_KEY>"] == "<YOUR_MY_KEY>"

    @pytest.mark.asyncio
    async def test_overwrite_existing(self, monkeypatch):
        raw, written = _mock_config({"env": {"OLD": "old_value"}}, monkeypatch)
        tool = ConfigEnvSetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="OLD", value="new_value")

        assert raw["env"]["OLD"] == "new_value"
        assert "[OK]" in result

    @pytest.mark.asyncio
    async def test_inject_into_os_environ(self, monkeypatch):
        raw, written = _mock_config({}, monkeypatch)
        tool = ConfigEnvSetTool(config_path=Path("test.json5"))

        old_val = os.environ.get("INJECT_TEST")
        await tool.execute(key="INJECT_TEST", value="injected!")

        assert os.environ["INJECT_TEST"] == "injected!"

        # Cleanup
        if old_val is not None:
            os.environ["INJECT_TEST"] = old_val
        else:
            os.environ.pop("INJECT_TEST", None)


# ── ConfigEnvGetTool ────────────────────────────────────────────────────────


class TestConfigEnvGetTool:
    """Tests for ConfigEnvGetTool."""

    @pytest.mark.asyncio
    async def test_get_single_key(self, monkeypatch):
        raw, _ = _mock_config({"env": {"MY_KEY": "my_value"}}, monkeypatch)
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="MY_KEY")
        assert "MY_KEY" in result
        assert "my_value" in result

    @pytest.mark.asyncio
    async def test_get_missing_key(self, monkeypatch):
        raw, _ = _mock_config({"env": {}}, monkeypatch)
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="NOT_THERE")
        assert "not set" in result

    @pytest.mark.asyncio
    async def test_get_placeholder_shows_warning(self, monkeypatch):
        raw, _ = _mock_config({"env": {"TODO": "<YOUR_TODO>"}}, monkeypatch)
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute(key="TODO")
        assert "[PLACEHOLDER]" in result

    @pytest.mark.asyncio
    async def test_list_all_vars(self, monkeypatch):
        raw, _ = _mock_config(
            {"env": {"A": "val_a", "B": "val_b"}}, monkeypatch,
        )
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute()
        assert "A" in result
        assert "val_a" in result
        assert "B" in result
        assert "val_b" in result

    @pytest.mark.asyncio
    async def test_list_empty(self, monkeypatch):
        raw, _ = _mock_config({}, monkeypatch)
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute()
        assert "No environment variables" in result

    @pytest.mark.asyncio
    async def test_list_shows_placeholder_markers(self, monkeypatch):
        raw, _ = _mock_config(
            {"env": {"REAL": "value", "TODO": "<YOUR_TODO>"}}, monkeypatch,
        )
        tool = ConfigEnvGetTool(config_path=Path("test.json5"))

        result = await tool.execute()
        assert "[PLACEHOLDER]" in result


# ── ConfigEnvRemoveTool ─────────────────────────────────────────────────────


class TestConfigEnvRemoveTool:
    """Tests for ConfigEnvRemoveTool."""

    @pytest.mark.asyncio
    async def test_remove_existing_var(self, monkeypatch):
        raw, written = _mock_config(
            {"env": {"TO_REMOVE": "bye"}}, monkeypatch,
        )
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
        tool = ConfigEnvRemoveTool(config_path=Path("test.json5"))

        result = await tool.execute(key="NOT_THERE")
        assert "not set" in result
        assert "nothing to remove" in result

    @pytest.mark.asyncio
    async def test_remove_var_not_in_os_environ(self, monkeypatch):
        raw, written = _mock_config(
            {"env": {"ONLY_IN_FILE": "val"}}, monkeypatch,
        )
        tool = ConfigEnvRemoveTool(config_path=Path("test.json5"))

        result = await tool.execute(key="ONLY_IN_FILE")
        assert "ONLY_IN_FILE" not in raw["env"]
        assert "[OK]" in result
