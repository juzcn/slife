"""Auto-discovery tool loading.

Scans slife.tools.* for Tool subclasses and registers them automatically.
The slife.json5 ``tools`` array is optional — use it only to override
defaults (e.g. shell timeout) or disable a tool (``enabled: false``).
"""

import importlib
import logging
import pkgutil
from typing import TYPE_CHECKING

from slife.tools.base import Tool
from slife.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from slife.config import Config

logger = logging.getLogger(__name__)


def create_tools_from_config(
    overrides: list[dict] | None = None,
    config: "Config | None" = None,
) -> ToolRegistry:
    """Build a ToolRegistry by auto-discovering all Tool subclasses.

    All modules in slife.tools.* are imported so Tool.__subclasses__()
    can find them. The optional ``overrides`` list matches entries
    by ``name`` against each tool's ``Tool.name`` to customise
    or disable individual tools.

    Example overrides:
        [{name: "execute_shell", timeout: 60}, {name: "list_skills", enabled: false}]
    """
    registry = ToolRegistry()
    override_map: dict[str, dict] = {}
    for entry in (overrides or []):
        name = entry.get("name", "")
        if name:
            override_map[name] = entry
        else:
            logger.warning("tool_override_no_name entry=%s", entry)

    for tool_cls in _discover_tools():
        cfg = override_map.get(tool_cls.name, {})
        if cfg.get("enabled") is False:
            logger.info("tool_disabled name=%s", tool_cls.name)
            continue

        # Skip tools that require the MQTT/A2A mesh when it's not enabled
        if getattr(tool_cls, "requires_a2a", False):
            a2a_cfg = getattr(config, "a2a_config", None) if config else None
            if a2a_cfg is None or not a2a_cfg.enabled:
                logger.debug("tool_skipped_no_a2a name=%s", tool_cls.name)
                continue

        tool = tool_cls.from_config(cfg, config)
        registry.register(tool)

    return registry


def _discover_tools():
    """Import all modules in slife.tools and yield every Tool subclass.

    Uses pkgutil.iter_modules so new tool files are picked up
    automatically — no manual imports or registry entries needed.
    """
    import slife.tools as pkg

    for _, modname, _ in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
        if modname.endswith(".base") or modname.endswith(".factory"):
            continue
        importlib.import_module(modname)

    # Walk __subclasses__ recursively to catch any hierarchy depth
    yield from _iter_subclasses(Tool)


def _iter_subclasses(cls):
    """Recursively yield all subclasses of cls.

    Only yields valid Tool subclasses — those that passed
    __init_subclass__ validation.  Broken subclasses (e.g. test
    stubs that raised TypeError during definition) are ignored.
    """
    for sub in cls.__subclasses__():
        if not _is_valid(sub):
            continue
        yield sub
        yield from _iter_subclasses(sub)


def _is_valid(cls) -> bool:
    """Return True if cls is a properly initialised Tool subclass.

    CPython registers the class in __subclasses__() *before* calling
    __init_subclass__, so subclasses that fail validation (like test
    stubs) still appear here.  We re-check the required attributes.
    """
    name = getattr(cls, "name", "")
    return bool(name) and name != "_mcp_proxy"
