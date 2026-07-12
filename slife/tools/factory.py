"""Config-driven tool loading.

Maps JSON5 tool entries to Tool instances. Add new tool types here.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from slife.tools.base import Tool
from slife.tools.registry import ToolRegistry
from slife.tools.shell_command import GetShellCommandTool
from slife.tools.shell import ShellTool
from slife.tools.skill import ListSkillsTool, UseSkillTool

if TYPE_CHECKING:
    from slife.config import Config

logger = logging.getLogger(__name__)

# Map of tool type string → factory function
# Each builder receives (cfg_dict, config) where config may be None.
_TOOL_BUILDERS = {
    "platform": lambda cfg, config: GetShellCommandTool(),
    "shell": lambda cfg, config: ShellTool(timeout=cfg.get("timeout", 30)),
    "skill": lambda cfg, config: [
        ListSkillsTool(skills_dir=cfg.get("skills_dir", "skills")),
        UseSkillTool(skills_dir=cfg.get("skills_dir", "skills")),
    ],
    "config_env": lambda cfg, config: _build_config_env_tools(config),
}


def _build_config_env_tools(config: "Config | None") -> list[Tool]:
    """Build config_env_set, config_env_get, config_env_remove tools.

    Uses config._path if available, otherwise defaults to slife.json5 in cwd.
    """
    from slife.tools.config_env import ConfigEnvSetTool, ConfigEnvGetTool, ConfigEnvRemoveTool

    config_path: Path | None = config._path if config else None
    return [
        ConfigEnvSetTool(config_path=config_path),
        ConfigEnvGetTool(config_path=config_path),
        ConfigEnvRemoveTool(config_path=config_path),
    ]


def create_tools_from_config(
    tool_entries: list[dict],
    config: "Config | None" = None,
) -> ToolRegistry:
    """Build a ToolRegistry from configuration entries.

    Each entry must have a 'type' field matching a registered builder.
    Unknown types log a warning and are skipped.

    Args:
        tool_entries: List of tool config dicts from slife.json5.
        config: Optional Config object. Tools that need config access
            (e.g. config_env_*) use config._path for persistence.

    Example config:
        [[tools]]
        type = "serper"
        api_key = "${SERPER_API_KEY}"

        [[tools]]
        type = "shell"
        timeout = 30
    """
    registry = ToolRegistry()

    for entry in tool_entries:
        tool_type = entry.get("type", "")
        if not tool_type:
            logger.warning("Tool entry missing 'type': %s", entry)
            continue

        builder = _TOOL_BUILDERS.get(tool_type)
        if builder is None:
            logger.warning(
                "Unknown tool type '%s'. Available: %s",
                tool_type,
                list(_TOOL_BUILDERS.keys()),
            )
            continue

        logger.info("Creating tool: type=%s", tool_type)
        result = builder(entry, config)
        # Support builders that return a single tool or a list of tools
        tools = result if isinstance(result, list) else [result]
        for tool in tools:
            registry.register(tool)

    return registry
