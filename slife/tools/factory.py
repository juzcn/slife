"""Config-driven tool loading.

Maps JSON5 tool entries to Tool instances. Add new tool types here.
"""

import logging

from slife.tools.base import Tool
from slife.tools.registry import ToolRegistry
from slife.tools.serper import SerperSearchTool
from slife.tools.shell import ShellTool
from slife.tools.skill import ListSkillsTool, UseSkillTool

logger = logging.getLogger(__name__)

# Map of tool type string → factory function
_TOOL_BUILDERS = {
    "serper": lambda cfg: SerperSearchTool(api_key=cfg["api_key"]),
    "shell": lambda cfg: ShellTool(timeout=cfg.get("timeout", 30)),
    "skill": lambda cfg: [
        ListSkillsTool(skills_dir=cfg.get("skills_dir", "skills")),
        UseSkillTool(skills_dir=cfg.get("skills_dir", "skills")),
    ],
}


def create_tools_from_config(tool_entries: list[dict]) -> ToolRegistry:
    """Build a ToolRegistry from configuration entries.

    Each entry must have a 'type' field matching a registered builder.
    Unknown types log a warning and are skipped.

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
        result = builder(entry)
        # Support builders that return a single tool or a list of tools
        tools = result if isinstance(result, list) else [result]
        for tool in tools:
            registry.register(tool)

    return registry
