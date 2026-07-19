"""Tests for slife.tools.credentials — credential check tool."""

import os
from unittest.mock import patch

import pytest

from slife.tools.credentials import _mask_value, CredentialCheckTool


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


class TestCredentialCheckTool:
    """Tests for CredentialCheckTool."""

    def test_tool_definition(self):
        tool = CredentialCheckTool()
        assert tool.name == "credential_check"
        assert "key" in tool.parameters["properties"]

    @pytest.mark.asyncio
    async def test_execute_found_in_env(self, monkeypatch):
        """Shell env var takes priority over keyring."""
        monkeypatch.setenv("TEST_API_KEY", "sk-shell-value-12345")
        # get_credential is imported locally inside execute()
        with patch("credstore.get_credential") as mock_get:
            tool = CredentialCheckTool()
            result = await tool.execute(key="TEST_API_KEY")
            assert "[shell]" in result
            assert "sk-s…2345" in result
            mock_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_found_in_keyring(self, monkeypatch):
        """Falls back to keyring when env var is not set."""
        monkeypatch.delenv("MY_KEY", raising=False)
        with patch("credstore.get_credential", return_value="my-secret-key-value") as mock_get:
            tool = CredentialCheckTool()
            result = await tool.execute(key="MY_KEY")
            assert "[credstore]" in result
            assert "my-s…alue" in result
            mock_get.assert_called_once_with("MY_KEY")

    @pytest.mark.asyncio
    async def test_execute_not_found(self, monkeypatch):
        """Returns not-found message when key is nowhere."""
        monkeypatch.delenv("MISSING_KEY", raising=False)
        with patch("credstore.get_credential", return_value=None):
            tool = CredentialCheckTool()
            result = await tool.execute(key="MISSING_KEY")
            assert "not stored" in result
            assert "MISSING_KEY" in result
