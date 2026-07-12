"""CLI tool management — register external CLI commands for discovery.

cli_add_tool:    register a CLI so the LLM can discover it next turn
cli_remove_tool: remove a registered CLI
cli_list_tools:  list all registered CLI tools

Registered CLIs are persisted to slife.json5 → cli_tools: section.
The LLM calls these tools via execute_shell — these tools only manage
the registry, they don't execute commands.

Flow:
  1. User mentions an unknown CLI → LLM runs "cmd --help"
  2. LLM understands the CLI → calls cli_add_tool to remember it
  3. Next session → cli_list_tools shows it, LLM knows what it is
"""

import logging
from pathlib import Path

from slife.tools._config_io import read_config, write_config
from slife.tools.base import Tool

logger = logging.getLogger(__name__)

_CLI_TOOLS_KEY = "cli_tools"


def _cli_section(raw: dict) -> dict:
    """Get or create the cli_tools: section."""
    section = raw.setdefault(_CLI_TOOLS_KEY, {})
    if not isinstance(section, dict):
        logger.warning("Config cli_tools: section is not a dict — resetting.")
        section = {}
        raw[_CLI_TOOLS_KEY] = section
    return section


def get_cli_tools_summary(config_path: Path) -> str:
    """Return a formatted summary of registered CLI tools."""
    raw = read_config(config_path)
    cli_tools = raw.get(_CLI_TOOLS_KEY, {})
    if not isinstance(cli_tools, dict) or not cli_tools:
        return "No CLI tools registered."

    lines = []
    for name, cfg in cli_tools.items():
        if not isinstance(cfg, dict):
            continue
        desc = cfg.get("description", "(no description)")
        command = cfg.get("command", name)
        install = cfg.get("install", "")
        line = f"- **{name}**: {desc}  \n  command: `{command}`"
        if install:
            line += f"  \n  install: `{install}`"
        lines.append(line)

    return "\n".join(lines)


class CliAddTool(Tool):
    """Register a CLI tool so the LLM can discover it in future turns.

    Does NOT execute the CLI — just records its existence, what it does,
    and how to install it. The LLM calls execute_shell to actually run it.

    Call this after you've verified the CLI works (--help succeeded).
    """

    name = "cli_add_tool"
    description = "Register an external CLI command."
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short name for this CLI (e.g. 'yldp', 'gh'). Used as the lookup key.",
            },
            "command": {
                "type": "string",
                "description": "The shell command to invoke (e.g. 'yldp', 'gh', 'python -m mytool').",
            },
            "description": {
                "type": "string",
                "description": "What this CLI does, its main subcommands, and common usage patterns. "
                "Write this based on --help output so the LLM knows how to use it next time.",
            },
            "install": {
                "type": "string",
                "description": "How to install this CLI if it's not already available "
                "(e.g. 'npm install -g yldp', 'pip install yldp'). Omit if already installed globally.",
            },
        },
        "required": ["name", "command", "description"],
    }

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or Path("slife.json5")

    @classmethod
    def from_config(cls, cfg, config):
        path = config._path if config else None
        return cls(config_path=path)

    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]
        command: str = kwargs["command"]
        description: str = kwargs["description"]
        install: str = kwargs.get("install", "")

        raw = read_config(self._config_path)
        cli_tools = _cli_section(raw)

        entry: dict = {"command": command, "description": description}
        if install:
            entry["install"] = install

        is_update = name in cli_tools
        cli_tools[name] = entry
        write_config(self._config_path, raw)

        action = "Updated" if is_update else "Registered"
        logger.info("CLI tool %s: %s", "updated" if is_update else "added", name)
        return f"[OK] {action} CLI tool '{name}'.\n  {description}"


class CliRemoveTool(Tool):
    """Remove a previously registered CLI tool."""

    name = "cli_remove_tool"
    description = "Remove a registered CLI tool."
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the CLI tool to remove (from cli_list_tools).",
            },
        },
        "required": ["name"],
    }

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or Path("slife.json5")

    @classmethod
    def from_config(cls, cfg, config):
        path = config._path if config else None
        return cls(config_path=path)

    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]
        raw = read_config(self._config_path)
        cli_tools = raw.get(_CLI_TOOLS_KEY, {})

        if not isinstance(cli_tools, dict) or name not in cli_tools:
            return f"CLI tool '{name}' is not registered."

        del cli_tools[name]
        write_config(self._config_path, raw)
        logger.info("CLI tool removed: %s", name)
        return f"[OK] Removed CLI tool '{name}'."


class CliListToolsTool(Tool):
    """List all registered CLI tools."""

    name = "cli_list_tools"
    description = "List registered external CLI tools."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or Path("slife.json5")

    @classmethod
    def from_config(cls, cfg, config):
        path = config._path if config else None
        return cls(config_path=path)

    async def execute(self, **kwargs) -> str:
        result = get_cli_tools_summary(self._config_path)
        return result
