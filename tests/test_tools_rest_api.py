"""Tests for slife.tools.rest_api — RestApiAddTool, RestApiRemoveTool, RestApiListTool, RestApiSet."""

import pytest; pytestmark = pytest.mark.unit


import json5
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from slife.tools.rest_api import (
    RestApiAddTool,
    RestApiRemoveTool,
    RestApiListTool,
    RestApiSet,
    get_rest_apis_summary,
    _rest_api_section,
    set_rest_api_mcp_client,
)


# ── Helpers ───────────────────────────────────────────────────────────────


@pytest.fixture
def temp_config():
    """Create a temporary JSON5 config file for testing."""
    with tempfile.NamedTemporaryFile(suffix=".json5", mode="w", delete=False) as f:
        f.write("{}")
        path = Path(f.name)
    yield path
    # Cleanup
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _write_config(path: Path, data: dict) -> None:
    path.write_text(json5.dumps(data, indent=2, trailing_commas=False, ensure_ascii=False), encoding="utf-8")


# ── _rest_api_section ─────────────────────────────────────────────────────


class TestRestApiSection:
    """Tests for _rest_api_section helper."""

    def test_creates_section_when_missing(self):
        raw = {}
        section = _rest_api_section(raw)
        assert "rest_apis" in raw
        assert section == {}

    def test_returns_existing_section(self):
        raw = {"rest_apis": {"existing": {}}}
        section = _rest_api_section(raw)
        assert section == {"existing": {}}

    def test_replaces_non_dict(self):
        """When rest_apis is not a dict, replaces it with empty dict."""
        raw = {"rest_apis": "not a dict"}
        section = _rest_api_section(raw)
        assert section == {}
        assert raw["rest_apis"] == {}


# ── get_rest_apis_summary ─────────────────────────────────────────────────


class TestGetRestApisSummary:
    """Tests for get_rest_apis_summary()."""

    def test_no_rest_apis(self, temp_config):
        result = get_rest_apis_summary(temp_config)
        assert "No REST APIs registered" in result

    def test_single_api(self, temp_config):
        _write_config(temp_config, {
            "rest_apis": {
                "github": {
                    "spec_url": "https://api.github.com/openapi.json",
                    "base_url": "https://api.github.com",
                    "description": "GitHub API",
                }
            }
        })
        result = get_rest_apis_summary(temp_config)
        assert "github" in result
        assert "GitHub API" in result
        assert "api.github.com" in result

    def test_multiple_apis(self, temp_config):
        _write_config(temp_config, {
            "rest_apis": {
                "github": {
                    "spec_url": "https://api.github.com/openapi.json",
                    "base_url": "https://api.github.com",
                    "description": "GitHub API",
                },
                "slack": {
                    "spec_url": "https://api.slack.com/openapi.json",
                    "base_url": "https://slack.com/api",
                    "description": "Slack API",
                    "api_key": "SLACK_TOKEN",
                },
            }
        })
        result = get_rest_apis_summary(temp_config)
        assert "github" in result
        assert "slack" in result
        assert "SLACK_TOKEN" in result  # api_key shown as ${...}

    def test_rest_apis_not_a_dict(self, temp_config):
        _write_config(temp_config, {"rest_apis": "not a dict"})
        result = get_rest_apis_summary(temp_config)
        assert "No REST APIs registered" in result

    def test_rest_apis_empty_dict(self, temp_config):
        _write_config(temp_config, {"rest_apis": {}})
        result = get_rest_apis_summary(temp_config)
        assert "No REST APIs registered" in result

    def test_skips_non_dict_entries(self, temp_config):
        _write_config(temp_config, {
            "rest_apis": {
                "valid": {"spec_url": "s", "base_url": "b", "description": "d"},
                "invalid": "not a dict",
            }
        })
        result = get_rest_apis_summary(temp_config)
        assert "valid" in result
        assert "invalid" not in result

    def test_with_source_info(self, temp_config):
        _write_config(temp_config, {
            "rest_apis": {
                "myservice": {
                    "spec_url": "https://example.com/openapi.json",
                    "base_url": "https://example.com",
                    "description": "My Service",
                    "source": {"type": "github", "url": "https://github.com/x/y"},
                }
            }
        })
        result = get_rest_apis_summary(temp_config)
        assert "myservice" in result
        assert "github" in result


# ── RestApiAddTool ────────────────────────────────────────────────────────


class TestRestApiAddTool:
    """Tests for RestApiAddTool."""

    @pytest.mark.asyncio
    async def test_metadata(self):
        tool = RestApiAddTool()
        assert tool.name == "rest_api_add"
        assert "name" in tool.parameters["required"]
        assert "spec_url" in tool.parameters["required"]
        assert "base_url" in tool.parameters["required"]

    @pytest.mark.asyncio
    async def test_add_new_api(self, temp_config):
        """Adding a new REST API writes to config and returns success."""
        tool = RestApiAddTool(config_path=temp_config)
        result = await tool.execute(
            name="github",
            spec_url="https://api.github.com/openapi.json",
            base_url="https://api.github.com",
            description="GitHub REST API",
        )
        assert "[OK]" in result
        assert "github" in result

        # Verify config was written
        raw = json5.loads(temp_config.read_text(encoding="utf-8"))
        assert "github" in raw["rest_apis"]
        assert raw["rest_apis"]["github"]["spec_url"] == "https://api.github.com/openapi.json"

    @pytest.mark.asyncio
    async def test_add_with_api_key(self, temp_config):
        """Adding with api_key stores the credential reference."""
        tool = RestApiAddTool(config_path=temp_config)
        result = await tool.execute(
            name="protected",
            spec_url="https://example.com/openapi.json",
            base_url="https://example.com",
            api_key="MY_TOKEN",
        )
        assert "[OK]" in result

        raw = json5.loads(temp_config.read_text(encoding="utf-8"))
        assert raw["rest_apis"]["protected"]["api_key"] == "MY_TOKEN"

    @pytest.mark.asyncio
    async def test_add_duplicate(self, temp_config):
        """Adding an already-registered API updates it (upsert)."""
        _write_config(temp_config, {
            "rest_apis": {
                "github": {"spec_url": "old", "base_url": "old"},
            }
        })
        tool = RestApiAddTool(config_path=temp_config)
        result = await tool.execute(
            name="github",
            spec_url="new",
            base_url="new",
        )
        assert "Updated" in result

    @pytest.mark.asyncio
    async def test_add_with_mcp_client(self, temp_config):
        """When MCP client is available, it's called after config save."""
        mock_client = MagicMock()
        mock_client.call_tool = MagicMock()
        mock_client.call_tool.return_value = "MCP connected"

        original_client = getattr(
            __import__("slife.tools.rest_api", fromlist=["_rest_api_mcp_client"]),
            "_rest_api_mcp_client",
            None,
        )
        try:
            set_rest_api_mcp_client(mock_client)
            tool = RestApiAddTool(config_path=temp_config)
            result = await tool.execute(
                name="github",
                spec_url="https://api.github.com/openapi.json",
                base_url="https://api.github.com",
            )
            assert "[OK]" in result
            assert mock_client.call_tool.called
        finally:
            set_rest_api_mcp_client(original_client)


# ── RestApiRemoveTool ─────────────────────────────────────────────────────


class TestRestApiRemoveTool:
    """Tests for RestApiRemoveTool."""

    @pytest.mark.asyncio
    async def test_remove_existing(self, temp_config):
        """Removing a registered API updates config."""
        _write_config(temp_config, {
            "rest_apis": {
                "github": {"spec_url": "u", "base_url": "b"},
            }
        })
        tool = RestApiRemoveTool(config_path=temp_config)
        result = await tool.execute(name="github")
        assert "[OK]" in result

        raw = json5.loads(temp_config.read_text(encoding="utf-8"))
        assert "github" not in raw["rest_apis"]

    @pytest.mark.asyncio
    async def test_remove_not_registered(self, temp_config):
        """Removing a non-existent API returns error."""
        tool = RestApiRemoveTool(config_path=temp_config)
        result = await tool.execute(name="nonexistent")
        assert "not registered" in result.lower()

    @pytest.mark.asyncio
    async def test_remove_with_mcp_client(self, temp_config):
        """When MCP client is available, calls mcp_remove_server."""
        _write_config(temp_config, {
            "rest_apis": {
                "github": {"spec_url": "u", "base_url": "b"},
            }
        })
        mock_client = MagicMock()
        mock_client.call_tool = MagicMock()
        mock_client.call_tool.return_value = "disconnected"

        original_client = getattr(
            __import__("slife.tools.rest_api", fromlist=["_rest_api_mcp_client"]),
            "_rest_api_mcp_client",
            None,
        )
        try:
            set_rest_api_mcp_client(mock_client)
            tool = RestApiRemoveTool(config_path=temp_config)
            result = await tool.execute(name="github")
            assert "[OK]" in result
            assert mock_client.call_tool.called
        finally:
            set_rest_api_mcp_client(original_client)


# ── RestApiListTool ───────────────────────────────────────────────────────


class TestRestApiListTool:
    """Tests for RestApiListTool."""

    @pytest.mark.asyncio
    async def test_list_empty(self, temp_config):
        """Listing with no APIs returns appropriate message."""
        tool = RestApiListTool(config_path=temp_config)
        result = await tool.execute()
        assert "No REST APIs registered" in result

    @pytest.mark.asyncio
    async def test_list_with_apis(self, temp_config):
        """Lists all registered APIs with details."""
        _write_config(temp_config, {
            "rest_apis": {
                "github": {
                    "spec_url": "https://api.github.com/openapi.json",
                    "base_url": "https://api.github.com",
                    "description": "GitHub API",
                },
            }
        })
        tool = RestApiListTool(config_path=temp_config)
        result = await tool.execute()
        assert "github" in result
        assert "GitHub API" in result


# ── RestApiSet ────────────────────────────────────────────────────────────


class TestRestApiSet:
    """Tests for RestApiSet."""

    @pytest.mark.asyncio
    async def test_metadata(self):
        tool = RestApiSet()
        assert tool.name == "rest_api_set"
        assert tool.category == "REST API"
        assert "name" in tool.parameters["required"]
        assert "enabled" in tool.parameters["required"]

    @pytest.mark.asyncio
    async def test_not_found(self, temp_config):
        """Setting a non-existent API returns error."""
        tool = RestApiSet(config_path=temp_config)
        result = await tool.execute(name="nonexistent", enabled=True)
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_malformed_entry(self, temp_config):
        """Setting a malformed entry returns error."""
        _write_config(temp_config, {
            "rest_apis": {
                "bad": "not a dict",
            }
        })
        tool = RestApiSet(config_path=temp_config)
        result = await tool.execute(name="bad", enabled=True)
        assert "malformed" in result.lower()

    @pytest.mark.asyncio
    async def test_enable_api(self, temp_config):
        """Enabling an API sets enabled=True in config."""
        _write_config(temp_config, {
            "rest_apis": {
                "github": {"spec_url": "u", "base_url": "b"},
            }
        })
        tool = RestApiSet(config_path=temp_config)
        result = await tool.execute(name="github", enabled=True)
        assert "[OK]" in result
        assert "enabled" in result.lower()

        raw = json5.loads(temp_config.read_text(encoding="utf-8"))
        assert raw["rest_apis"]["github"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_disable_api(self, temp_config):
        """Disabling an API sets enabled=False in config."""
        _write_config(temp_config, {
            "rest_apis": {
                "github": {"spec_url": "u", "base_url": "b", "enabled": True},
            }
        })
        tool = RestApiSet(config_path=temp_config)
        result = await tool.execute(name="github", enabled=False)
        assert "[OK]" in result
        assert "disabled" in result.lower()

        raw = json5.loads(temp_config.read_text(encoding="utf-8"))
        assert raw["rest_apis"]["github"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_set_with_mcp_client(self, temp_config):
        """When MCP client is available, calls mcp_set_server."""
        _write_config(temp_config, {
            "rest_apis": {
                "github": {"spec_url": "u", "base_url": "b"},
            }
        })
        mock_client = MagicMock()
        mock_client.call_tool = MagicMock()
        mock_client.call_tool.return_value = "enabled"

        original_client = getattr(
            __import__("slife.tools.rest_api", fromlist=["_rest_api_mcp_client"]),
            "_rest_api_mcp_client",
            None,
        )
        try:
            set_rest_api_mcp_client(mock_client)
            tool = RestApiSet(config_path=temp_config)
            result = await tool.execute(name="github", enabled=True)
            assert "[OK]" in result
            assert mock_client.call_tool.called
        finally:
            set_rest_api_mcp_client(original_client)
