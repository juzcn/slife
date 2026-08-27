"""Tests for slife.tools.rest_api — RestApiSetTool et al.

REST API definitions now live in mcp-plugin.json5 (owned by mcp-plugin) as
ordinary ``npx anyapi-mcp-server`` server entries tagged
``source.type == "rest_api"``.  These tests exercise the tools against a
throwaway config file located via ``$MCP_PLUGIN_FILE``.
"""

import pytest; pytestmark = pytest.mark.unit

import json5
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mcp_plugin import config as mcp_plugin_config
from slife.tools.rest_api import (
    RestApiSetTool,
    RestApiRemoveTool,
    RestApiListTool,
    RestApiSetEnabledTool,
    get_rest_apis_summary,
)


# ── Helpers ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mcp_config_path(tmp_path, monkeypatch):
    """Point mcp-plugin.config at a throwaway mcp-plugin.json5 per test."""
    path = tmp_path / "mcp-plugin.json5"
    monkeypatch.setenv("MCP_PLUGIN_FILE", str(path))
    # Pin the resolver BEFORE the test runs — a stale _CURRENT_PATH from a
    # previous test would otherwise make reads/writes land in the wrong file.
    mcp_plugin_config.set_config_path(str(path))
    yield path


def _entry(spec_url: str, base_url: str, *, api_key: str = "", description: str = "",
           enabled: bool = True) -> dict:
    args = [
        "-y", "anyapi-mcp-server",
        "--name", "x", "--spec", spec_url, "--base-url", base_url,
    ]
    if api_key:
        args += ["--header", f"Authorization: Bearer ${{{api_key}}}"]
    entry: dict = {"command": "npx", "args": args, "source": {"type": "rest_api"}}
    if description:
        entry["description"] = description
    if not enabled:
        entry["enabled"] = False
    return entry


def _write_config(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json5.dumps({"servers": servers}, indent=2, trailing_commas=False, ensure_ascii=False),
        encoding="utf-8",
    )


def _entries_from_file(path: Path) -> dict:
    raw = json5.loads(path.read_text(encoding="utf-8"))
    return raw.get("servers", {})


# ── get_rest_apis_summary ─────────────────────────────────────────────────


class TestGetRestApisSummary:
    """Tests for get_rest_apis_summary()."""

    def test_no_rest_apis(self, mcp_config_path):
        _write_config(mcp_config_path, {})
        result = get_rest_apis_summary(mcp_config_path)
        assert "No REST APIs registered" in result

    def test_single_api(self, mcp_config_path):
        _write_config(mcp_config_path, {
            "github": _entry("https://api.github.com/openapi.json",
                             "https://api.github.com", description="GitHub API"),
        })
        result = get_rest_apis_summary(mcp_config_path)
        assert "github" in result
        assert "GitHub API" in result
        assert "api.github.com" in result

    def test_multiple_apis(self, mcp_config_path):
        _write_config(mcp_config_path, {
            "github": _entry("https://api.github.com/openapi.json",
                             "https://api.github.com", description="GitHub API"),
            "slack": _entry("https://api.slack.com/openapi.json",
                            "https://slack.com/api", description="Slack API",
                            api_key="SLACK_TOKEN"),
        })
        result = get_rest_apis_summary(mcp_config_path)
        assert "github" in result
        assert "slack" in result
        assert "SLACK_TOKEN" in result  # api_key shown as ${...}

    def test_rest_apis_not_a_dict(self, mcp_config_path):
        _write_config(mcp_config_path, {"bad": "not a dict"})
        result = get_rest_apis_summary(mcp_config_path)
        assert "No REST APIs registered" in result

    def test_skips_non_dict_entries(self, mcp_config_path):
        _write_config(mcp_config_path, {
            "valid": _entry("https://spec.example.com/x", "https://example.com", description="d"),
            "invalid": "not a dict",
        })
        result = get_rest_apis_summary(mcp_config_path)
        assert "valid" in result
        assert "invalid" not in result

    def test_with_source_info(self, mcp_config_path):
        entry = _entry("https://example.com/openapi.json", "https://example.com",
                       description="My Service")
        entry["source"] = {"type": "github", "url": "https://github.com/x/y"}
        _write_config(mcp_config_path, {"myservice": entry})
        result = get_rest_apis_summary(mcp_config_path)
        assert "myservice" in result
        assert "github" in result


# ── RestApiSetTool ────────────────────────────────────────────────────────


class TestRestApiSetTool:
    """Tests for RestApiSetTool."""

    @pytest.mark.asyncio
    async def test_metadata(self):
        tool = RestApiSetTool()
        assert tool.name == "rest_api_set"
        assert "name" in tool.parameters["required"]
        assert "spec_url" in tool.parameters["required"]
        assert "base_url" in tool.parameters["required"]

    @pytest.mark.asyncio
    async def test_add_new_api(self, mcp_config_path):
        """Adding a new REST API writes an anyapi server entry and succeeds."""
        tool = RestApiSetTool(config_path=mcp_config_path)
        result = await tool.execute(
            name="github",
            spec_url="https://api.github.com/openapi.json",
            base_url="https://api.github.com",
            description="GitHub REST API",
        )
        assert "[OK]" in result
        assert "github" in result

        servers = _entries_from_file(mcp_config_path)
        assert "github" in servers
        entry = servers["github"]
        assert entry["command"] == "npx"
        assert "anyapi-mcp-server" in entry["args"]
        assert entry["source"]["type"] == "rest_api"

    @pytest.mark.asyncio
    async def test_add_with_api_key(self, mcp_config_path):
        """Adding with api_key stores the credential reference, not the secret."""
        tool = RestApiSetTool(config_path=mcp_config_path)
        result = await tool.execute(
            name="protected",
            spec_url="https://example.com/openapi.json",
            base_url="https://example.com",
            api_key="MY_TOKEN",
        )
        assert "[OK]" in result

        servers = _entries_from_file(mcp_config_path)
        header = servers["protected"]["args"][-1]
        assert header == "Authorization: Bearer ${MY_TOKEN}"
        assert "MY_TOKEN" not in json5.dumps(servers["protected"]).replace("MY_TOKEN}", "")

    @pytest.mark.asyncio
    async def test_add_duplicate(self, mcp_config_path):
        """Adding an already-registered API updates it (upsert)."""
        _write_config(mcp_config_path, {
            "github": _entry("https://api.github.com/spec", "https://api.github.com"),
        })
        tool = RestApiSetTool(config_path=mcp_config_path)
        result = await tool.execute(
            name="github",
            spec_url="https://api.github.com/spec",
            base_url="https://api.github.com",
        )
        assert "Updated" in result

    @pytest.mark.asyncio
    async def test_add_rejects_non_http_scheme(self, mcp_config_path):
        """REVIEW S2: file:// (and other non-http) specs are rejected."""
        tool = RestApiSetTool(config_path=mcp_config_path)
        with pytest.raises(ValueError, match="URL with a host"):
            await tool.execute(
                name="evil",
                spec_url="file:///etc/passwd",
                base_url="https://api.example.com",
            )

    @pytest.mark.asyncio
    async def test_add_rejects_url_without_host(self, mcp_config_path):
        """REVIEW S2: a bare string with no http(s) host is rejected."""
        tool = RestApiSetTool(config_path=mcp_config_path)
        with pytest.raises(ValueError, match="URL with a host"):
            await tool.execute(
                name="bare",
                spec_url="https://api.example.com/spec",
                base_url="not-a-url",
            )

    @pytest.mark.asyncio
    async def test_add_with_mcp_client(self, mcp_config_path):
        """When MCP client is available, it's called after config save."""
        mock_client = MagicMock()
        mock_client.call_tool = MagicMock()
        mock_client.call_tool.return_value = "MCP connected"

        try:
            from slife.tools.context import ToolContext
            tool = RestApiSetTool(config_path=mcp_config_path)
            tool._ctx = ToolContext(mcp_client=mock_client, config=None)
            result = await tool.execute(
                name="github",
                spec_url="https://api.github.com/openapi.json",
                base_url="https://api.github.com",
            )
            assert "[OK]" in result
            assert mock_client.call_tool.called
        finally:
            tool._ctx = None


# ── RestApiRemoveTool ─────────────────────────────────────────────────────


class TestRestApiRemoveTool:
    """Tests for RestApiRemoveTool."""

    @pytest.mark.asyncio
    async def test_remove_existing(self, mcp_config_path):
        """Removing a registered API removes its server entry."""
        _write_config(mcp_config_path, {
            "github": _entry("https://u.example.com/spec", "https://u.example.com"),
        })
        tool = RestApiRemoveTool(config_path=mcp_config_path)
        result = await tool.execute(name="github")
        assert "[OK]" in result

        servers = _entries_from_file(mcp_config_path)
        assert "github" not in servers

    @pytest.mark.asyncio
    async def test_remove_not_registered(self, mcp_config_path):
        """Removing a non-existent API returns error."""
        tool = RestApiRemoveTool(config_path=mcp_config_path)
        result = await tool.execute(name="nonexistent")
        assert "not registered" in result.lower()

    @pytest.mark.asyncio
    async def test_remove_with_mcp_client(self, mcp_config_path):
        """When MCP client is available, calls mcp_remove."""
        _write_config(mcp_config_path, {
            "github": _entry("https://u.example.com/spec", "https://u.example.com"),
        })
        mock_client = MagicMock()
        mock_client.call_tool = MagicMock()
        mock_client.call_tool.return_value = "disconnected"

        try:
            from slife.tools.context import ToolContext
            tool = RestApiRemoveTool(config_path=mcp_config_path)
            tool._ctx = ToolContext(mcp_client=mock_client, config=None)
            result = await tool.execute(name="github")
            assert "[OK]" in result
            assert mock_client.call_tool.called
        finally:
            tool._ctx = None


# ── RestApiListTool ───────────────────────────────────────────────────────


class TestRestApiListTool:
    """Tests for RestApiListTool."""

    @pytest.mark.asyncio
    async def test_list_empty(self, mcp_config_path):
        """Listing with no APIs returns appropriate message."""
        tool = RestApiListTool(config_path=mcp_config_path)
        result = await tool.execute()
        assert "No REST APIs registered" in result

    @pytest.mark.asyncio
    async def test_list_with_apis(self, mcp_config_path):
        """Lists all registered APIs with details."""
        _write_config(mcp_config_path, {
            "github": _entry("https://api.github.com/openapi.json",
                             "https://api.github.com", description="GitHub API"),
        })
        tool = RestApiListTool(config_path=mcp_config_path)
        result = await tool.execute()
        assert "github" in result
        assert "GitHub API" in result


# ── RestApiSetEnabledTool ────────────────────────────────────────────────


class TestRestApiSetEnabledTool:
    """Tests for RestApiSetEnabledTool."""

    @pytest.mark.asyncio
    async def test_metadata(self):
        tool = RestApiSetEnabledTool()
        assert tool.name == "rest_api_set_enabled"
        assert tool.category == "REST API"
        assert "name" in tool.parameters["required"]
        assert "enabled" in tool.parameters["required"]

    @pytest.mark.asyncio
    async def test_not_found(self, mcp_config_path):
        """Setting a non-existent API returns error."""
        tool = RestApiSetEnabledTool(config_path=mcp_config_path)
        result = await tool.execute(name="nonexistent", enabled=True)
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_enable_api(self, mcp_config_path):
        """Enabling an API clears the disabled flag."""
        _write_config(mcp_config_path, {
            "github": _entry("https://u.example.com/spec", "https://u.example.com",
                             enabled=False),
        })
        tool = RestApiSetEnabledTool(config_path=mcp_config_path)
        result = await tool.execute(name="github", enabled=True)
        assert "[OK]" in result
        assert "enabled" in result.lower()

        servers = _entries_from_file(mcp_config_path)
        assert "enabled" not in servers["github"]  # enabled is the default

    @pytest.mark.asyncio
    async def test_disable_api(self, mcp_config_path):
        """Disabling an API sets enabled=false in config."""
        _write_config(mcp_config_path, {
            "github": _entry("https://u.example.com/spec", "https://u.example.com"),
        })
        tool = RestApiSetEnabledTool(config_path=mcp_config_path)
        result = await tool.execute(name="github", enabled=False)
        assert "[OK]" in result
        assert "disabled" in result.lower()

        servers = _entries_from_file(mcp_config_path)
        assert servers["github"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_set_with_mcp_client(self, mcp_config_path):
        """When MCP client is available, calls mcp_set."""
        _write_config(mcp_config_path, {
            "github": _entry("https://u.example.com/spec", "https://u.example.com"),
        })
        mock_client = MagicMock()
        mock_client.call_tool = MagicMock()
        mock_client.call_tool.return_value = "enabled"

        try:
            from slife.tools.context import ToolContext
            tool = RestApiSetEnabledTool(config_path=mcp_config_path)
            tool._ctx = ToolContext(mcp_client=mock_client, config=None)
            result = await tool.execute(name="github", enabled=True)
            assert "[OK]" in result
            assert mock_client.call_tool.called
        finally:
            tool._ctx = None