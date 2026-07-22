"""Tests for Slife.tools.cli — CLI tool registration with source tracking."""

import json5
import pytest
from pathlib import Path

from slife.tools.cli import CliAddTool, CliCheckInstalled, CliRemoveTool, CliListToolsTool, get_cli_tools_summary
from slife.tools._config_io import with_fetched_at


# ── CliAddTool ─────────────────────────────────────────────────────────


class TestCliAddToolMetadata:
    """Metadata validation for CliAddTool."""

    def test_name(self):
        assert CliAddTool.name == "cli_add_tool"

    def test_description(self):
        assert "Persist" in CliAddTool.description

    def test_required_params(self):
        required = CliAddTool.parameters.get("required", [])
        assert "name" in required
        assert "command" in required
        assert "description" in required

    def test_source_param_in_schema(self):
        props = CliAddTool.parameters.get("properties", {})
        assert "source" in props
        source_props = props["source"].get("properties", {})
        assert "url" in source_props
        assert "type" in source_props
        assert "version" in source_props


class TestCliAddToolExecute:
    """Execute tests for CliAddTool."""

    @pytest.mark.asyncio
    async def test_add_with_source(self, tmp_path):
        """cli_add_tool stores source dict with auto-generated fetched_at."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({}))

        tool = CliAddTool(config_path=cfg_path)
        result = await tool.execute(
            name="yt-dlp",
            command="yt-dlp",
            description="Video downloader",
            source={"url": "https://github.com/yt-dlp/yt-dlp", "type": "github", "version": "2026.03.01"},
        )

        assert "[OK]" in result
        assert "yt-dlp" in result

        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        entry = raw["cli_tools"]["yt-dlp"]
        assert entry["command"] == "yt-dlp"
        src = entry["source"]
        assert src["url"] == "https://github.com/yt-dlp/yt-dlp"
        assert src["type"] == "github"
        assert src["version"] == "2026.03.01"
        assert "fetched_at" in src  # auto-generated

    @pytest.mark.asyncio
    async def test_add_without_source(self, tmp_path):
        """cli_add_tool without source is backward compatible — no source key."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({}))

        tool = CliAddTool(config_path=cfg_path)
        result = await tool.execute(
            name="npm",
            command="npm",
            description="Node package manager",
        )

        assert "[OK]" in result
        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        assert "source" not in raw["cli_tools"]["npm"]

    @pytest.mark.asyncio
    async def test_add_with_partial_source(self, tmp_path):
        """Only provided source fields are stored (plus fetched_at)."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({}))

        tool = CliAddTool(config_path=cfg_path)
        await tool.execute(
            name="gh",
            command="gh",
            description="GitHub CLI",
            source={"url": "https://cli.github.com/"},
        )

        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        src = raw["cli_tools"]["gh"]["source"]
        assert src["url"] == "https://cli.github.com/"
        assert "type" not in src
        assert "version" not in src
        assert "fetched_at" in src

    @pytest.mark.asyncio
    async def test_add_with_install_and_source(self, tmp_path):
        """Both install instructions and source can coexist."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({}))

        tool = CliAddTool(config_path=cfg_path)
        await tool.execute(
            name="yldp",
            command="yldp",
            description="Download tool",
            install="npm install -g yldp",
            source={"type": "npm", "version": "1.2.3"},
        )

        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        entry = raw["cli_tools"]["yldp"]
        assert entry["install"] == "npm install -g yldp"
        assert entry["source"]["type"] == "npm"
        assert entry["source"]["version"] == "1.2.3"

    @pytest.mark.asyncio
    async def test_update_preserves_source(self, tmp_path):
        """Updating a CLI entry preserves the source field if re-provided."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({}))

        tool = CliAddTool(config_path=cfg_path)
        # First add
        await tool.execute(
            name="foo", command="foo", description="old",
            source={"type": "pypi", "url": "https://pypi.org/project/foo/"},
        )
        # Update
        result = await tool.execute(
            name="foo", command="foo", description="updated",
            source={"type": "github", "url": "https://github.com/foo/foo"},
        )

        assert "Updated" in result
        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        entry = raw["cli_tools"]["foo"]
        assert entry["description"] == "updated"
        assert entry["source"]["type"] == "github"

    @pytest.mark.asyncio
    async def test_source_none_not_stored(self, tmp_path):
        """Explicit None source should not write a source key."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({}))

        tool = CliAddTool(config_path=cfg_path)
        await tool.execute(
            name="cmd", command="cmd", description="desc",
            source=None,
        )

        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        assert "source" not in raw["cli_tools"]["cmd"]


class TestCliAddToolWithExistingConfig:
    """CliAddTool works alongside other config sections."""

    @pytest.mark.asyncio
    async def test_cli_tools_section_created(self, tmp_path):
        """cli_tools section is created if not already present."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({"models": {}, "env": {"KEY": "val"}}))

        tool = CliAddTool(config_path=cfg_path)
        await tool.execute(name="test", command="test", description="A test CLI")

        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        assert "cli_tools" in raw
        assert raw["env"]["KEY"] == "val"  # existing sections preserved


# ── CliRemoveTool ────────────────────────────────────────────────────────


class TestCliRemoveTool:
    """Tests for CliRemoveTool."""

    @pytest.mark.asyncio
    async def test_remove_existing(self, tmp_path):
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "cli_tools": {"test": {"command": "test", "description": "desc"}},
        }))

        tool = CliRemoveTool(config_path=cfg_path)
        result = await tool.execute(name="test")
        assert "[OK]" in result
        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        assert "test" not in raw.get("cli_tools", {})

    @pytest.mark.asyncio
    async def test_remove_nonexistent(self, tmp_path):
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({}))

        tool = CliRemoveTool(config_path=cfg_path)
        result = await tool.execute(name="ghost")
        assert "not registered" in result


# ── CliListToolsTool ──────────────────────────────────────────────────────


class TestCliListToolsTool:
    """Tests for CliListToolsTool."""

    @pytest.mark.asyncio
    async def test_list_empty(self, tmp_path):
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({}))

        tool = CliListToolsTool(config_path=cfg_path)
        result = await tool.execute()
        assert "No CLI tools" in result

    @pytest.mark.asyncio
    async def test_list_with_entries(self, tmp_path):
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "cli_tools": {
                "a": {"command": "a", "description": "Tool A"},
                "b": {"command": "b", "description": "Tool B", "install": "pip install b"},
            },
        }))

        tool = CliListToolsTool(config_path=cfg_path)
        result = await tool.execute()
        assert "Tool A" in result
        assert "Tool B" in result
        assert "pip install b" in result


# ── get_cli_tools_summary ────────────────────────────────────────────────


class TestGetCliToolsSummary:
    """Tests for get_cli_tools_summary display output."""

    def test_displays_source_info(self, tmp_path):
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "cli_tools": {
                "yt-dlp": {
                    "command": "yt-dlp",
                    "description": "Video downloader",
                    "source": {"type": "github", "url": "https://github.com/yt-dlp/yt-dlp", "version": "2026.03.01"},
                },
            },
        }))

        summary = get_cli_tools_summary(cfg_path)
        assert "github" in summary
        assert "https://github.com/yt-dlp/yt-dlp" in summary
        assert "v2026.03.01" in summary

    def test_no_source_info_when_absent(self, tmp_path):
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "cli_tools": {
                "cmd": {"command": "cmd", "description": "A tool"},
            },
        }))

        summary = get_cli_tools_summary(cfg_path)
        assert "source:" not in summary

    def test_source_with_type_only(self, tmp_path):
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "cli_tools": {
                "cmd": {
                    "command": "cmd",
                    "description": "A tool",
                    "source": {"type": "pypi"},
                },
            },
        }))

        summary = get_cli_tools_summary(cfg_path)
        assert "pypi" in summary


# ── CliCheckInstalled ────────────────────────────────────────────────────


class TestCliCheckInstalledMetadata:
    """Metadata validation for CliCheckInstalled."""

    def test_name(self):
        assert CliCheckInstalled.name == "cli_check_installed"

    def test_description(self):
        assert "slife.json5" in CliCheckInstalled.description

    def test_required_params(self):
        required = CliCheckInstalled.parameters.get("required", [])
        assert "commands" in required

    def test_commands_param_is_array_of_strings(self):
        props = CliCheckInstalled.parameters.get("properties", {})
        commands = props.get("commands", {})
        assert commands.get("type") == "array"
        assert commands.get("items", {}).get("type") == "string"


class TestCliCheckInstalledExecute:

    @pytest.mark.asyncio
    async def test_all_registered(self, tmp_path):
        """All checked commands are registered."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "cli_tools": {
                "npm": {"command": "npm", "description": "Node package manager", "install": "Download from nodejs.org"},
                "uv": {"command": "uv", "description": "Python package manager", "source": {"type": "pypi", "url": "https://pypi.org/project/uv/"}},
            },
        }))

        tool = CliCheckInstalled(config_path=cfg_path)
        result = await tool.execute(commands=["npm", "uv"])
        assert "2/2 registered" in result
        assert "● npm" in result
        assert "● uv" in result
        assert "nodejs.org" in result
        assert "pypi" in result

    @pytest.mark.asyncio
    async def test_none_registered(self, tmp_path):
        """No checked commands are registered."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({"cli_tools": {}}))

        tool = CliCheckInstalled(config_path=cfg_path)
        result = await tool.execute(commands=["python", "git"])
        assert "0/2 registered" in result
        assert "○ python" in result
        assert "○ git" in result
        assert "not registered in config" in result

    @pytest.mark.asyncio
    async def test_mixed_registered(self, tmp_path):
        """Some commands are registered, some are not."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "cli_tools": {
                "npm": {"command": "npm", "description": "Node package manager"},
            },
        }))

        tool = CliCheckInstalled(config_path=cfg_path)
        result = await tool.execute(commands=["npm", "git"])
        assert "1/2 registered" in result
        assert "● npm" in result
        assert "○ git" in result

    @pytest.mark.asyncio
    async def test_no_cli_tools_section(self, tmp_path):
        """Config has no cli_tools section at all."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({}))

        tool = CliCheckInstalled(config_path=cfg_path)
        result = await tool.execute(commands=["npm"])
        assert "0/1 registered" in result
        assert "○ npm" in result

    @pytest.mark.asyncio
    async def test_cli_tools_not_dict(self, tmp_path):
        """cli_tools is not a dict — should be handled gracefully."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({"cli_tools": []}))

        tool = CliCheckInstalled(config_path=cfg_path)
        result = await tool.execute(commands=["npm"])
        assert "0/1 registered" in result

    @pytest.mark.asyncio
    async def test_shows_install_and_source_when_present(self, tmp_path):
        """Result shows install instructions and source info when available."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "cli_tools": {
                "gh": {
                    "command": "gh",
                    "description": "GitHub CLI",
                    "install": "winget install GitHub.cli",
                    "source": {"type": "github", "url": "https://cli.github.com/"},
                },
            },
        }))

        tool = CliCheckInstalled(config_path=cfg_path)
        result = await tool.execute(commands=["gh"])
        assert "● gh" in result
        assert "winget install GitHub.cli" in result
        assert "github" in result
        assert "https://cli.github.com/" in result


# ── get_cli_tools_summary edge cases ────────────────────────────────────


class TestGetCliToolsSummaryEdgeCases:
    """Tests for get_cli_tools_summary edge cases."""

    def test_skips_non_dict_entries(self, tmp_path):
        """Entries in cli_tools that are not dicts should be skipped."""
        from slife.tools.cli import get_cli_tools_summary
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "cli_tools": {
                "good_tool": {
                    "command": "echo",
                    "description": "A good tool",
                },
                "bad_tool": "just a string, not a dict",
            },
        }))
        result = get_cli_tools_summary(cfg_path)
        assert "good_tool" in result
        assert "bad_tool" not in result

    def test_non_dict_cli_tools_section(self, tmp_path):
        """When cli_tools is a list, should show default message."""
        from slife.tools.cli import get_cli_tools_summary
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "cli_tools": ["bad", "values"],
        }))
        result = get_cli_tools_summary(cfg_path)
        assert result == "No CLI tools registered."
