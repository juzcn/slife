"""CLI tool management — register external CLI commands for discovery.

cli_set:               register/update a CLI so the LLM can discover it next turn
cli_remove:            remove a registered CLI
cli_list:              list all registered CLI tools

Registered CLIs are persisted to slife.json5 → cli_tools: section.
These tools only manage the registry — they don't execute commands.
"""

import logging
from pathlib import Path
from typing import ClassVar

from slife.tools._config_io import (
    _ConfigPathMixin,
    format_source_info,
    read_config,
    with_fetched_at,
    write_config,
)
from slife.tools.base import Tool

logger = logging.getLogger(__name__)

_CLI_TOOLS_KEY = "cli_tools"


def _cli_section(raw: dict) -> dict:
    """Get or create the cli_tools: section."""
    section = raw.setdefault(_CLI_TOOLS_KEY, {})
    if not isinstance(section, dict):
        logger.warning("cli_config_not_dict")
        section = {}
        raw[_CLI_TOOLS_KEY] = section
    return section



def _format_cli_tools(cli_tools: dict) -> str:
    """Format a cli_tools dict into a human-readable summary."""
    if not cli_tools:
        return "No CLI tools registered."

    lines = []
    for name, cfg in cli_tools.items():
        if not isinstance(cfg, dict):
            continue
        desc = cfg.get("description", "(no description)")
        command = cfg.get("command", name)
        install = cfg.get("install", "")
        source = cfg.get("source")
        line = f"- **{name}**: {desc}  \n  command: `{command}`"
        if install:
            line += f"  \n  install: `{install}`"
        src_str = format_source_info(source)  # type: ignore[arg-type]
        if src_str:
            line += f"  \n  source: {src_str}"
        lines.append(line)

    return "\n".join(lines)


def get_cli_tools_summary(config_path: Path) -> str:
    """Read cli_tools from file (fallback when Config is not available)."""
    raw = read_config(config_path)
    cli_tools = raw.get(_CLI_TOOLS_KEY, {})
    if not isinstance(cli_tools, dict) or not cli_tools:
        return "No CLI tools registered."
    return _format_cli_tools(cli_tools)


class CliSetTool(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    """Register or update a CLI tool so the LLM can discover it in future turns.

    Does NOT execute the CLI — just records its existence, what it does,
    and how to install it.

    Call this after you've verified the CLI works (--help succeeded).
    """

    name = "cli_set"
    category = "CLI"
    description = "Register/update an external CLI in slife.json5 for later discovery (does not execute it)."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short name (e.g. 'gh')."},
            "command": {"type": "string", "description": "Shell invocation (e.g. 'gh', 'python -m mytool')."},
            "description": {"type": "string", "description": "What it does, from --help output, in the CLI's own language."},
            "install": {"type": "string", "description": "Install command (e.g. 'npm i -g yldp'); omit if pre-installed."},
            "source": {
                "type": "object",
                "description": "Provenance for future updates.",
                "properties": {
                    "url": {"type": "string", "description": "Discovery URL."},
                    "type": {"type": "string", "description": "Source type: npm, pypi, github, url, cargo, apt."},
                    "version": {"type": "string", "description": "Version at install time."},
                    "description": {"type": "string", "description": "Optional note."},
                },
            },
        },
        "required": ["name", "command", "description"],
    }

    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]
        command: str = kwargs["command"]
        description: str = kwargs["description"]
        install: str = kwargs.get("install", "")
        source: dict | None = kwargs.get("source")


        source = with_fetched_at(source)
        is_update = False

        ctx = getattr(self, "_ctx", None); config = ctx.config if ctx is not None else None
        
        if config is not None and config._path is not None:
            is_update = name in config.cli_tools
            old = config.cli_tools.get(name)
            old_enabled = old.get("enabled") if isinstance(old, dict) else None
            config.save_cli_tool(
                name=name, command=command, description=description,
                install=install, source=source, enabled=old_enabled,
            )
        else:
            raw = read_config(self._config_path)
            cli_tools = _cli_section(raw)
            is_update = name in cli_tools
            old = cli_tools.get(name)
            old_enabled = old.get("enabled") if isinstance(old, dict) else None
            entry: dict = {"command": command, "description": description}
            if install:
                entry["install"] = install
            if source:
                entry["source"] = source
            if old_enabled is not None:
                # Preserve the enable/disable flag across an update — the
                # "idempotent upsert" contract must not silently re-enable a
                # deliberately-disabled tool.
                entry["enabled"] = old_enabled
            cli_tools[name] = entry
            write_config(self._config_path, raw)

        action = "Updated" if is_update else "Registered"
        logger.info("cli_%s name=%s", "updated" if is_update else "added", name)
        return f"[OK] {action} CLI tool '{name}'.\n  {description}"


class CliRemoveTool(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    """Remove a registered CLI tool from slife.json5."""

    name = "cli_remove"
    category = "CLI"
    description = "Remove a CLI registration from slife.json5. Does not uninstall the command."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "CLI name, from cli_list."},
        },
        "required": ["name"],
    }

    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]


        ctx = getattr(self, "_ctx", None); config = ctx.config if ctx is not None else None
        

        if config is not None and config._path is not None:
            if name not in config.cli_tools:
                return f"CLI tool '{name}' is not registered."
            config.remove_cli_tool(name)
        else:
            raw = read_config(self._config_path)
            cli_tools = raw.get(_CLI_TOOLS_KEY, {})
            if not isinstance(cli_tools, dict) or name not in cli_tools:
                return f"CLI tool '{name}' is not registered."
            del cli_tools[name]
            write_config(self._config_path, raw)

        logger.info("cli_removed name=%s", name)
        return f"[OK] Removed CLI tool '{name}'."


class CliListToolsTool(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    """List all registered CLI tools."""

    name = "cli_list"
    category = "CLI"
    description = "List registered CLI tools (descriptions, commands, install)."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs) -> str:

        ctx = getattr(self, "_ctx", None); config = ctx.config if ctx is not None else None
        
        if config is not None and config._path is not None and config.cli_tools:
            return _format_cli_tools(config.cli_tools)
        return get_cli_tools_summary(self._config_path)


# ═══════════════════════════════════════════════════════════════════════
# cli_set_enabled
# ═══════════════════════════════════════════════════════════════════════


class CliSetEnabledTool(_ConfigPathMixin, Tool):
    name = "cli_set_enabled"
    category = "CLI"
    description = "Enable or disable a registered CLI tool. Takes effect after restart."

    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "CLI tool name, from cli_list."},
            "enabled": {"type": "boolean", "description": "Enable or disable."},
        },
        "required": ["name", "enabled"],
    }

    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]
        enabled: bool = kwargs["enabled"]


        ctx = getattr(self, "_ctx", None); config = ctx.config if ctx is not None else None
        
        if config is not None and config._path is not None:
            if name not in config.cli_tools:
                return f"'{name}' not found in cli_tools."
            entry = config.cli_tools[name]
            if not isinstance(entry, dict):
                return f"'{name}' in cli_tools is malformed."
            entry["enabled"] = enabled
            config.save_cli_tool(
                name=name,
                command=entry.get("command", ""),
                description=entry.get("description", ""),
                install=entry.get("install", ""),
                source=entry.get("source"),
                enabled=enabled,
            )
        else:
            raw = read_config(self._config_path)
            entries = raw.get("cli_tools", {})
            if not isinstance(entries, dict) or name not in entries:
                return f"'{name}' not found in cli_tools."
            entry = entries[name]
            if not isinstance(entry, dict):
                return f"'{name}' in cli_tools is malformed."
            entry["enabled"] = enabled
            write_config(self._config_path, raw)

        state = "enabled" if enabled else "disabled"
        logger.info("cli_set_enabled name=%s enabled=%s", name, enabled)
        return f"[OK] CLI tool '{name}' {state}. Restart for the change to take effect."
