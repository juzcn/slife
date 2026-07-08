"""Config-driven tool loading.

Maps TOML tool entries to Tool instances. Add new tool types here.
"""

from slife.tools.base import Tool
from slife.tools.registry import ToolRegistry
from slife.tools.serper import SerperSearchTool
from slife.tools.shell import ShellTool

# Map of tool type string → factory function
_TOOL_BUILDERS = {
    "serper": lambda cfg: SerperSearchTool(api_key=cfg["api_key"]),
    "shell": lambda cfg: ShellTool(timeout=cfg.get("timeout", 30)),
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
            import warnings
            warnings.warn(f"Tool entry missing 'type': {entry}")
            continue

        builder = _TOOL_BUILDERS.get(tool_type)
        if builder is None:
            import warnings
            warnings.warn(
                f"Unknown tool type '{tool_type}'. "
                f"Available: {list(_TOOL_BUILDERS.keys())}"
            )
            continue

        tool = builder(entry)
        registry.register(tool)

    return registry
