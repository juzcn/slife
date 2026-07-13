"""CLI tool management — register external CLI commands for discovery.

cli_add_tool:          register a CLI so the LLM can discover it next turn
cli_check_installed:   check whether a CLI command is installed on the system
cli_remove_tool:       remove a registered CLI
cli_list_tools:        list all registered CLI tools

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

from slife.tools._config_io import now_iso, read_config, with_fetched_at, write_config
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
        source = cfg.get("source")
        line = f"- **{name}**: {desc}  \n  command: `{command}`"
        if install:
            line += f"  \n  install: `{install}`"
        if isinstance(source, dict):
            parts = []
            if source.get("type"):
                parts.append(source["type"])
            if source.get("url"):
                parts.append(source["url"])
            if source.get("version"):
                parts.append(f"v{source['version']}")
            if parts:
                line += f"  \n  source: {' — '.join(parts)}"
        lines.append(line)

    return "\n".join(lines)


class CliCheckInstalled(Tool):
    """Check whether CLI commands are registered in slife.json5.

    Looks up command names in the cli_tools config section — this tells
    you whether slife already knows about a CLI (its command, description,
    install method) without running anything on the system.

    Use before re-installing, before calling cli_add_tool, or when the
    user asks "do I have X set up?".  Does NOT run the actual command.
    """

    name = "cli_check_installed"
    description = (
        "Check whether CLI commands are already registered in slife.json5. "
        "Returns each command's registration status, its invocation, "
        "description, install instructions, and source if recorded. "
        "Use before installing or registering a CLI to avoid duplicates. "
        "Does NOT run shell commands."
    )
    parameters = {
        "type": "object",
        "properties": {
            "commands": {
                "type": "array",
                "description": "One or more command names to check (e.g. ['npm', 'git', 'uv']).",
                "items": {"type": "string"},
            },
        },
        "required": ["commands"],
    }

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or Path("slife.json5")

    @classmethod
    def from_config(cls, cfg, config):
        path = config._path if config else None
        return cls(config_path=path)

    async def execute(self, **kwargs) -> str:
        commands: list[str] = kwargs["commands"]

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
                source_info = ""
                src = entry.get("source")
                if isinstance(src, dict):
                    parts = []
                    if src.get("type"):
                        parts.append(src["type"])
                    if src.get("url"):
                        parts.append(src["url"])
                    if parts:
                        source_info = f"  source: {' — '.join(parts)}"
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


class CliAddTool(Tool):
    """Register a CLI tool so the LLM can discover it in future turns.

    Does NOT execute the CLI — just records its existence, what it does,
    and how to install it. The LLM calls execute_shell to actually run it.

    Call this after you've verified the CLI works (--help succeeded).
    """

    name = "cli_add_tool"
    description = (
        "Persist an external CLI command to slife.json5 with its name, "
        "invocation, description, and optional install instructions. "
        "Does not execute the CLI — records it for future discovery."
    )
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
            "source": {
                "type": "object",
                "description": "Where this CLI was discovered. Provide so future updates "
                "or source changes are traceable.",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL where the tool was found (repo, docs, package page).",
                    },
                    "type": {
                        "type": "string",
                        "description": "Source type: npm, pypi, github, url, cargo, apt, etc.",
                    },
                    "version": {
                        "type": "string",
                        "description": "Version string at install time (e.g. '1.2.3', 'v2.0.1').",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional note about this source (e.g. 'official npm package').",
                    },
                },
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
        source: dict | None = kwargs.get("source")

        raw = read_config(self._config_path)
        cli_tools = _cli_section(raw)

        entry: dict = {"command": command, "description": description}
        if install:
            entry["install"] = install
        source = with_fetched_at(source)
        if source:
            entry["source"] = source

        is_update = name in cli_tools
        cli_tools[name] = entry
        write_config(self._config_path, raw)

        action = "Updated" if is_update else "Registered"
        logger.info("cli_%s name=%s", "updated" if is_update else "added", name)
        return f"[OK] {action} CLI tool '{name}'.\n  {description}"


class CliRemoveTool(Tool):
    """Remove a previously registered CLI tool."""

    name = "cli_remove_tool"
    description = "Delete a CLI command registration from slife.json5."
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
        logger.info("cli_removed name=%s", name)
        return f"[OK] Removed CLI tool '{name}'."


class CliListToolsTool(Tool):
    """List all registered CLI tools."""

    name = "cli_list_tools"
    description = (
        "List all registered external CLI tools with their "
        "descriptions, commands, and install instructions."
    )
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
