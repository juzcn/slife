"""CLI tool management — register external CLI commands for discovery.

cli_set:               register/update a CLI so the LLM can discover it next turn
cli_remove:            remove a registered CLI
cli_list:              list all registered CLI tools

Registered CLIs are persisted to tools.json5 → cli: section (one category
section of the unified tools config — the host reads it at startup into
``Config.cli_tools``).
These tools only manage the registry — they don't execute commands.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from slife.tools._config_io import (
    _ConfigPathMixin,
    config_write_locked,
    format_source_info,
    read_config,
    with_fetched_at,
    write_config,
)
from slife.paths import get_tools_config_path
from slife.tools.base import Tool

if TYPE_CHECKING:
    from slife.config import Config
    from slife.tools.context import ToolContext

logger = logging.getLogger(__name__)

_CLI_TOOLS_KEY = "cli"


class _CliConfigMixin(_ConfigPathMixin):
    """The cli tools' config mixin — targets tools.json5, not slife.json5.

    The ``cli`` section lives in tools.json5 (the ``_ConfigPathMixin``
    default stays with slife.json5 for ``config_env.py``).
    """

    def __init__(self, config_path: Path | None = None):
        super().__init__(config_path or get_tools_config_path())

    @classmethod
    def from_config(cls, cfg: dict, config: "Config | None", ctx: "ToolContext | None" = None):  # pyright: ignore[reportIncompatibleMethodOverride]
        path = config._tools_config_path() if config is not None else get_tools_config_path()
        tool = cls(config_path=path)
        if ctx is not None:
            object.__setattr__(tool, "_ctx", ctx)
        return tool


def _cli_section(raw: dict) -> dict:
    """Get or create the cli: section."""
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


def cli_catalog_rows(cli_tools: dict) -> dict[str, dict]:
    """The catalog rows the ``cli`` section implies — name → {description, schema, enabled}.

    A cli entry has no tool def to store (``schema`` stays NULL): the
    description is what identifies it to ``tool_search``, and ``cli_list``
    carries the command / install detail.  ``enabled`` mirrors the entry's own
    flag.
    """
    rows: dict[str, dict] = {}
    for name, cfg in cli_tools.items():
        if not isinstance(cfg, dict):
            continue
        rows[name] = {
            "description": cfg.get("description", ""),
            "schema": None,
            "enabled": cfg.get("enabled", True) is not False,
        }
    return rows


async def sync_cli_catalog(ctx, cli_tools: dict) -> None:
    """Push the current cli section into the catalog (no catalog → no-op).

    Called at boot and after every cli mutation, so the rows say what the
    config says right now — a removed CLI loses its row immediately.
    """
    from slife.tools.catalog_service import mirror_source_rows

    await mirror_source_rows(ctx, "cli", cli_catalog_rows(cli_tools))


def _live_cli_config(self) -> "Config | None":
    """The live ``Config`` when it is bound to a real path, else None.

    Every cli mutation tool shares the same dual-write shape: mutate the live
    ``Config`` snapshot (its own writer persists it), or fall back to the raw
    tools.json5 file.  This selects the target.
    """
    ctx = getattr(self, "_ctx", None)
    config = ctx.config if ctx is not None else None
    if config is not None and config._path is not None:
        return config
    return None


def _open_raw_cli(self) -> tuple[dict, "Callable[[], None]"]:
    """The raw tools.json5 ``cli`` section plus a writer that commits it.

    Fallback write target when no live Config is bound.  Mutate the returned
    section in place, then call the writer to persist the whole file back.
    """
    raw = read_config(self._config_path)
    # ``_cli_section`` attaches a fresh ``{}`` to *raw* when the key is
    # missing, so a brand-new entry actually lands in the written file.
    return _cli_section(raw), lambda: write_config(self._config_path, raw)


class CliSetTool(_CliConfigMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    """Register or update a CLI tool so the LLM can discover it in future turns.

    Does NOT execute the CLI — just records its existence, what it does,
    and how to install it.

    Call this after you've verified the CLI works (--help succeeded).
    """

    name = "cli_set"
    category = "CLI"
    description = "Register/update an external CLI in tools.json5 for later discovery (does not execute it)."
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

    @config_write_locked
    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]
        command: str = kwargs["command"]
        description: str = kwargs["description"]
        install: str = kwargs.get("install", "")
        source: dict | None = kwargs.get("source")


        source = with_fetched_at(source)
        is_update = False

        ctx = getattr(self, "_ctx", None)
        config = _live_cli_config(self)
        if config is not None:
            # Preserve the enable/disable flag across an update — the
            # "idempotent upsert" contract must not silently re-enable a
            # deliberately-disabled tool.
            old = config.cli_tools.get(name)
            old_enabled = old.get("enabled") if isinstance(old, dict) else None
            config.save_cli_tool(
                name=name, command=command, description=description,
                install=install, source=source, enabled=old_enabled,
            )
            current = config.cli_tools
        else:
            cli_tools, persist = _open_raw_cli(self)
            old = cli_tools.get(name)
            old_enabled = old.get("enabled") if isinstance(old, dict) else None
            entry: dict = {"command": command, "description": description}
            if install:
                entry["install"] = install
            if source:
                entry["source"] = source
            if old_enabled is not None:
                entry["enabled"] = old_enabled
            cli_tools[name] = entry
            persist()
            current = cli_tools

        is_update = name in current
        # The catalog follows the config, so the entry is findable by
        # tool_search before the next restart.
        await sync_cli_catalog(ctx, current)
        action = "Updated" if is_update else "Registered"
        logger.info("cli_%s name=%s", "updated" if is_update else "added", name)
        return f"[OK] {action} CLI tool '{name}'.\n  {description}"


class CliRemoveTool(_CliConfigMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    """Remove a registered CLI tool from tools.json5."""

    name = "cli_remove"
    category = "CLI"
    description = "Remove a CLI registration from tools.json5. Does not uninstall the command."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "CLI name, from cli_list."},
        },
        "required": ["name"],
    }

    @config_write_locked
    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]


        ctx = getattr(self, "_ctx", None)
        config = _live_cli_config(self)
        if config is not None:
            if name not in config.cli_tools:
                return f"CLI tool '{name}' is not registered."
            config.remove_cli_tool(name)
            current = config.cli_tools
        else:
            cli_tools, persist = _open_raw_cli(self)
            if name not in cli_tools:
                return f"CLI tool '{name}' is not registered."
            del cli_tools[name]
            persist()
            current = cli_tools

        await sync_cli_catalog(ctx, current)
        logger.info("cli_removed name=%s", name)
        return f"[OK] Removed CLI tool '{name}'."


class CliListToolsTool(_CliConfigMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
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


class CliSetEnabledTool(_CliConfigMixin, Tool):
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

    @config_write_locked
    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]
        enabled: bool = kwargs["enabled"]


        ctx = getattr(self, "_ctx", None)
        config = _live_cli_config(self)
        if config is not None:
            entries = config.cli_tools
            if name not in entries:
                return f"'{name}' not found in cli config."
            entry = entries[name]
            if not isinstance(entry, dict):
                return f"'{name}' in cli config is malformed."
            config.save_cli_tool(
                name=name,
                command=entry.get("command", ""),
                description=entry.get("description", ""),
                install=entry.get("install", ""),
                source=entry.get("source"),
                enabled=enabled,
            )
        else:
            entries, persist = _open_raw_cli(self)
            if name not in entries:
                return f"'{name}' not found in cli config."
            entry = entries[name]
            if not isinstance(entry, dict):
                return f"'{name}' in cli config is malformed."
            entry["enabled"] = enabled
            persist()

        current = entries
        await sync_cli_catalog(ctx, current)
        state = "enabled" if enabled else "disabled"
        logger.info("cli_set_enabled name=%s enabled=%s", name, enabled)
        return f"[OK] CLI tool '{name}' {state}. Restart for the change to take effect."
