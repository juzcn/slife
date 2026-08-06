"""CLI tool management — register external CLI commands for discovery.

cli_add_tool:          register a CLI so the LLM can discover it next turn
cli_check_installed:   check whether a CLI command is installed on the system
cli_remove_tool:       remove a registered CLI
cli_list_tools:        list all registered CLI tools

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


class CliCheckInstalled(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    """Check whether CLI commands are registered in slife.json5.

    Looks up command names in the cli_tools config section — this tells
    you whether slife already knows about a CLI (its command, description,
    install method) without running anything on the system.

    Use before re-installing, before calling cli_add_tool, or when the
    user asks "do I have X set up?".  Does NOT run the actual command.
    """

    name = "cli_check_installed"
    category = "CLI"
    description = "Check whether CLI commands are registered in slife.json5. Does NOT run shell commands."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "commands": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Command names, e.g. ['npm', 'git', 'uv'].",
            },
        },
        "required": ["commands"],
    }

    async def execute(self, **kwargs) -> str:
        commands: list[str] = kwargs["commands"]

        ctx = getattr(self, "_ctx", None)
        config = ctx.config if ctx is not None else None
        if config is not None and config._path is not None:
            cli_tools = config.cli_tools
        else:
            raw = read_config(self._config_path)
            cli_tools = raw.get(_CLI_TOOLS_KEY, {})
            if not isinstance(cli_tools, dict):
                cli_tools = {}

        lines = []
        found = 0
        for cmd in commands:
            entry = cli_tools.get(cmd)
            if isinstance(entry, dict):
                found += 1
                src_str = format_source_info(entry.get("source"))  # type: ignore[arg-type]
                source_info = f"  source: {src_str}" if src_str else ""
                install_info = ""
                if entry.get("install"):
                    install_info = f"\n  install: {entry['install']}"
                line = (
                    f"● {cmd} — {entry.get('command', cmd)}"
                    f"{install_info}"
                    f"{source_info}"
                )
            else:
                line = f"○ {cmd} — not registered in config"
            lines.append(line)

        summary = f"{found}/{len(commands)} registered"
        return summary + "\n" + "\n".join(lines)


class CliAddTool(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    """Register a CLI tool so the LLM can discover it in future turns.

    Does NOT execute the CLI — just records its existence, what it does,
    and how to install it.

    Call this after you've verified the CLI works (--help succeeded).
    """

    name = "cli_add_tool"
    category = "CLI"
    _subagent_skip = True
    description = "Register an external CLI in slife.json5. Does not execute — records for future discovery."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short name (e.g. 'gh', 'yldp')."},
            "command": {"type": "string", "description": "Shell invocation (e.g. 'gh', 'python -m mytool')."},
            "description": {"type": "string", "description": "What it does, subcommands, usage. Write from --help output."},
            "install": {"type": "string", "description": "Install command (e.g. 'npm i -g yldp'). Omit if pre-installed."},
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
            config.save_cli_tool(
                name=name, command=command, description=description,
                install=install, source=source,
            )
        else:
            raw = read_config(self._config_path)
            cli_tools = _cli_section(raw)
            is_update = name in cli_tools
            entry: dict = {"command": command, "description": description}
            if install:
                entry["install"] = install
            if source:
                entry["source"] = source
            cli_tools[name] = entry
            write_config(self._config_path, raw)

        action = "Updated" if is_update else "Registered"
        logger.info("cli_%s name=%s", "updated" if is_update else "added", name)
        return f"[OK] {action} CLI tool '{name}'.\n  {description}"


class CliRemoveTool(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    """Remove a registered CLI tool from slife.json5."""

    name = "cli_remove_tool"
    category = "CLI"
    _subagent_skip = True
    description = "Remove a CLI registration from slife.json5. Does not uninstall the command."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "CLI name, from cli_list_tools."},
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

    name = "cli_list_tools"
    category = "CLI"
    description = "List registered CLI tools with descriptions, commands, and install instructions."
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
# cli_set_tool
# ═══════════════════════════════════════════════════════════════════════


class CliSetTool(_ConfigPathMixin, Tool):
    name = "cli_set_tool"
    category = "CLI"
    _subagent_skip = True
    description = "Enable or disable a registered CLI tool. Takes effect after restart."

    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "CLI tool name, from cli_list_tools."},
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
        logger.info("cli_set_tool name=%s enabled=%s", name, enabled)
        return f"[OK] CLI tool '{name}' {state}. Restart for the change to take effect."
