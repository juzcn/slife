"""Auto-discovery tool loading.

Scans slife.tools.* for Tool subclasses and registers them automatically.
The slife.yaml ``tools`` array is optional — use it only to override
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
    from slife.tools.context import ToolContext

logger = logging.getLogger(__name__)


def create_tools_from_config(
    overrides: list[dict] | None = None,
    config: "Config | None" = None,
    ctx: "ToolContext | None" = None,
) -> ToolRegistry:
    """Build a ToolRegistry by auto-discovering all Tool subclasses.

    All modules in slife.tools.* are imported so Tool.__subclasses__()
    can find them. The optional ``overrides`` list matches entries
    by ``name`` against each tool's ``Tool.name`` to customise
    or disable individual tools.

    Example overrides:
        [{name: "execute_shell", timeout: 60}, {name: "skill_list", enabled: false}]
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

        # NOTE: vision tools are NOT filtered here anymore — they are always
        # registered (attach_image is the only one).  A tool that needs
        # vision enforces it at execute() time (see AttachImageTool) so a
        # non-vision model that calls it gets a clear "vision=false" refusal
        # instead of a silently-missing tool.

        # Note: the cabinet + sharing tools (note_save / share_file)
        # live in their plugins (registered as proxy tools), not here — so
        # there is no tunnel-gating needed at builtin-tool load time.

        tool = tool_cls.from_config(cfg, config, ctx)

        registry.register(tool)

    logger.info("tools_loaded count=%d", len(registry.list_tools()))
    return registry


def disabled_tool_instances(
    overrides: list[dict] | None = None,
    config: "Config | None" = None,
    ctx: "ToolContext | None" = None,
) -> list[Tool]:
    """Instances of the tools an override switched OFF — never registered.

    A disabled builtin is skipped by :func:`create_tools_from_config`, so it
    never reaches the registry.  It still exists as far as ``tools.yaml`` is
    concerned, and the catalog carries a row for every entry (marked
    ``disabled``, unloadable) — otherwise yaml would declare a tool the db had
    never heard of, and ``tool_search`` could not report it as switched off.

    Built for the same reason the registry is built (``from_config``), so the
    row's description/schema are the tool's own, not a stub.
    """
    override_map = {
        entry["name"]: entry for entry in (overrides or []) if entry.get("name")
    }
    disabled: list[Tool] = []
    for tool_cls in _discover_tools():
        cfg = override_map.get(tool_cls.name, {})
        if cfg.get("enabled") is not False:
            continue
        try:
            disabled.append(tool_cls.from_config(cfg, config, ctx))
        except Exception as e:
            # A tool that cannot be built has no row to mirror — worse than a
            # missing catalog row is a boot that dies over one disabled tool.
            logger.warning("disabled_tool_build_failed name=%s err=%s", tool_cls.name, e)
    return disabled


def _discover_tools():
    """Import all modules in Slife.tools and yield every Tool subclass.

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

    A class that is skipped (e.g. an abstract base like ``_ModelConfigTool``)
    still gets its subclasses visited — real tools like ``model_set`` /
    ``model_remove`` / ``model_switch`` inherit from it and must be
    discovered.  Only skipping an entire subtree (bug 069c954) would drop
    them from the registry.
    """
    for sub in cls.__subclasses__():
        if not _is_valid(sub):
            yield from _iter_subclasses(sub)
            continue
        yield sub
        yield from _iter_subclasses(sub)


def _is_valid(cls) -> bool:
    """Return True if cls is a properly initialised Tool subclass.

    CPython registers the class in __subclasses__() *before* calling
    __init_subclass__, so subclasses that fail validation (like test
    stubs) still appear here.  We re-check the same required attributes as
    ``Tool.__init_subclass__`` (name, description, parameters) — a leaked
    invalid subclass must never reach the registry, where a missing
    attribute would blow up schema generation.

    Classes with ``_skip_auto_register = True`` (e.g. MCPProxyTool,
    whose real name/description/parameters are set per-instance) are
    excluded — they are created manually by their own factory functions.

    The flag is checked with ``vars(cls)`` (not ``getattr``) so it only
    applies to the class that sets it.  It must NOT be inherited: a shared
    base like ``_ModelConfigTool`` sets it to exclude itself while its
    subclasses (model_set / model_remove / model_switch) are real tools
    that must still be discovered.

    Only classes defined INSIDE the ``slife.tools`` package are candidate
    tools.  ``Tool.__subclasses__()`` reaches every subclass ever created in
    the process — including module-level stubs from the test-suite era that
    collide with production names (a test stub ``execute_shell`` without a
    ``timeout`` would silently replace the real ``ShellTool`` in the registry
    depending on import order).  Tools from plugin/adapter modules
    (``create_proxy_tools``) are registered by their own factories.
    """
    if not cls.__module__.startswith("slife.tools"):
        return False
    if cls.__dict__.get("_skip_auto_register", False):
        return False
    for attr in ("name", "description", "parameters"):
        value = getattr(cls, attr, "")
        if value in (None, ""):
            return False
    return True
