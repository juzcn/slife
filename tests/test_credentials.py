"""Tests for slife.tools.credentials — credential check tool."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from slife.tools.credentials import (
    _mask_value,
    _find_json5_refs,
    _simplify_path,
    CredentialCheckTool,
)


class TestMaskValue:
    """Tests for _mask_value helper."""

    def test_long_value_shows_first_4_and_last_4(self):
        result = _mask_value("sk-abcdefghijklmnop")
        assert result == "sk-a…mnop"

    def test_short_value_returns_stars(self):
        result = _mask_value("short")
        assert result == "***"

    def test_8_char_value_returns_stars(self):
        """Edge case: exactly 8 chars still returns *** (len <= 8)."""
        result = _mask_value("12345678")
        assert result == "***"

    def test_empty_value(self):
        result = _mask_value("")
        assert result == "***"


class TestFindJson5Refs:
    """Tests for _find_json5_refs."""

    def test_finds_in_env_section(self):
        raw = {"env": {"MY_KEY": "${MY_KEY}"}}
        refs = _find_json5_refs(raw, "MY_KEY")
        assert "env" in refs

    def test_finds_in_provider_api_key(self):
        raw = {
            "models": {
                "providers": {
                    "deepseek": {"api_key": "${DEEPSEEK_API_KEY}"},
                },
            },
        }
        refs = _find_json5_refs(raw, "DEEPSEEK_API_KEY")
        assert "models/providers/deepseek" in refs

    def test_finds_in_mcp_server_env(self):
        raw = {
            "mcp": {
                "servers": {
                    "github": {
                        "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
                    },
                },
            },
        }
        refs = _find_json5_refs(raw, "GITHUB_TOKEN")
        assert "mcp/servers/github" in refs

    def test_finds_in_mcp_args(self):
        raw = {
            "mcp": {
                "servers": {
                    "github": {
                        "args": ["--header", "Authorization: Bearer ${GITHUB_TOKEN}"],
                    },
                },
            },
        }
        refs = _find_json5_refs(raw, "GITHUB_TOKEN")
        assert any("mcp/servers/github" in r for r in refs)

    def test_multiple_locations(self):
        raw = {
            "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
            "mcp": {
                "servers": {
                    "github": {
                        "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
                    },
                },
            },
        }
        refs = _find_json5_refs(raw, "GITHUB_TOKEN")
        assert len(refs) >= 2

    def test_not_found(self):
        raw = {"env": {"OTHER_KEY": "${OTHER_KEY}"}}
        refs = _find_json5_refs(raw, "MISSING_KEY")
        assert refs == []

    def test_empty_config(self):
        refs = _find_json5_refs({}, "ANY_KEY")
        assert refs == []


class TestSimplifyPath:
    """Tests for _simplify_path."""

    def test_strips_trailing_env(self):
        assert _simplify_path("mcp/servers/github/env") == "mcp/servers/github"

    def test_preserves_path_without_env(self):
        assert _simplify_path("models/providers/deepseek") == "models/providers/deepseek"

    def test_simple_path(self):
        assert _simplify_path("env") == "env"


class TestCredentialCheckTool:
    """Tests for CredentialCheckTool."""

    def test_tool_definition(self):
        tool = CredentialCheckTool(config_path=Path("test.json5"))
        assert tool.name == "credential_check"
        assert "key" in tool.parameters["properties"]

    @pytest.mark.asyncio
    async def test_execute_found_in_shell(self, monkeypatch):
        """Shell env var is reported as set."""
        monkeypatch.setenv("TEST_API_KEY", "sk-shell-value-12345")
        with patch("slife.tools.credentials.read_config", return_value={}):
            with patch("credstore.get_credential", return_value=None) as mock_get:
                tool = CredentialCheckTool(config_path=Path("test.json5"))
                result = await tool.execute(key="TEST_API_KEY")
                assert "[shell]" in result
                assert "✓ set" in result
                assert "sk-s…2345" in result
                # keyring is still checked (we want full status)
                mock_get.assert_called_once_with("TEST_API_KEY")

    @pytest.mark.asyncio
    async def test_execute_found_in_keyring(self, monkeypatch):
        """Keyring is reported as stored when env is not set."""
        monkeypatch.delenv("MY_KEY", raising=False)
        with patch("slife.tools.credentials.read_config", return_value={}):
            with patch("credstore.get_credential", return_value="my-secret-key-value") as mock_get:
                tool = CredentialCheckTool(config_path=Path("test.json5"))
                result = await tool.execute(key="MY_KEY")
                assert "[credstore]" in result
                assert "✓ stored" in result
                assert "my-s…alue" in result
                mock_get.assert_called_once_with("MY_KEY")

    @pytest.mark.asyncio
    async def test_execute_found_in_json5(self, monkeypatch):
        """slife.json5 reference is reported."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        config = {
            "mcp": {
                "servers": {
                    "github": {
                        "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
                    },
                },
            },
        }
        with patch("slife.tools.credentials.read_config", return_value=config):
            with patch("credstore.get_credential", return_value=None):
                tool = CredentialCheckTool(config_path=Path("test.json5"))
                result = await tool.execute(key="GITHUB_TOKEN")
                assert "[slife.json5]" in result
                assert "✓ referenced" in result
                assert "mcp/servers/github" in result

    @pytest.mark.asyncio
    async def test_execute_not_found_anywhere(self, monkeypatch):
        """Returns not-found status for all sources."""
        monkeypatch.delenv("MISSING_KEY", raising=False)
        with patch("slife.tools.credentials.read_config", return_value={}):
            with patch("credstore.get_credential", return_value=None):
                tool = CredentialCheckTool(config_path=Path("test.json5"))
                result = await tool.execute(key="MISSING_KEY")
                assert "✗ not set" in result
                assert "✗ not referenced" in result
                assert "✗ not stored" in result

    @pytest.mark.asyncio
    async def test_execute_found_in_all_sources(self, monkeypatch):
        """When key exists everywhere, all sources report ✓."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-1234")
        config = {
            "env": {"DEEPSEEK_API_KEY": "${DEEPSEEK_API_KEY}"},
            "models": {
                "providers": {
                    "deepseek": {"api_key": "${DEEPSEEK_API_KEY}"},
                },
            },
        }
        with patch("slife.tools.credentials.read_config", return_value=config):
            with patch("credstore.get_credential", return_value="sk-deepseek-5678"):
                tool = CredentialCheckTool(config_path=Path("test.json5"))
                result = await tool.execute(key="DEEPSEEK_API_KEY")
                assert "[shell]" in result and "✓ set" in result
                assert "[slife.json5]" in result and "✓ referenced" in result
                assert "[credstore]" in result and "✓ stored" in result
                # Shell and credstore may have different values
                assert "sk-d…1234" in result
                assert "sk-d…5678" in result

    @pytest.mark.asyncio
    async def test_execute_shell_not_set_but_json5_and_keyring_ok(self, monkeypatch):
        """Shell missing, but json5 + keyring are configured."""
        monkeypatch.delenv("SERPER_API_KEY", raising=False)
        config = {
            "mcp": {
                "servers": {
                    "serper": {
                        "env": {"SERPER_API_KEY": "${SERPER_API_KEY}"},
                    },
                },
            },
        }
        with patch("slife.tools.credentials.read_config", return_value=config):
            with patch("credstore.get_credential", return_value="sk-serper-key-abc"):
                tool = CredentialCheckTool(config_path=Path("test.json5"))
                result = await tool.execute(key="SERPER_API_KEY")
                assert "[shell]" in result and "✗ not set" in result
                assert "[slife.json5]" in result and "✓ referenced" in result
                assert "[credstore]" in result and "✓ stored" in result

    @pytest.mark.asyncio
    async def test_status_format_structure(self, monkeypatch):
        """Verify the output has the expected structure with all three sources."""
        monkeypatch.delenv("ANY_KEY", raising=False)
        with patch("slife.tools.credentials.read_config", return_value={}):
            with patch("credstore.get_credential", return_value=None):
                tool = CredentialCheckTool(config_path=Path("test.json5"))
                result = await tool.execute(key="ANY_KEY")
                lines = result.split("\n")
                assert lines[0] == "ANY_KEY status:"
                assert any("[shell]" in l for l in lines)
                assert any("[slife.json5]" in l for l in lines)
                assert any("[credstore]" in l for l in lines)
