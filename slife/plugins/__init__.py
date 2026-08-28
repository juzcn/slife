"""Slife plugin auto-discovery — like native tools, but as child processes.

Each plugin is a Python package with a ``server.py`` entry point exposing a
``main()`` that host its own FastMCP server.  There is ONE framework for every
plugin; "built-in" vs "external" is only a *registration source* — where the
``(name, module)`` tuple comes from:

Built-in plugins
  Discovered by a source scan of ``slife.plugins.*`` packages containing a
  ``server.py`` (``memdb``, ``wechat``, ``memfiles``, ``sharefile``, ``a2a``).
  They ship inside the slife wheel, so the scan is the right registration —
  no config entry needed.

External plugins
  Standalone distributions (e.g. ``mcp_plugin``) registered via the
  ``plugins.external`` section of ``slife.json5`` — a list of
  ``{"name": ..., "module": ...}`` entries.  Integrating one requires
  ``pip install`` + one config line, zero slife code changes.

Once discovered, both feed the identical generic lifecycle (spawn via
``sys.executable -m <module>``, connect over Streamable HTTP, register tools,
watchdog) — runtime never distinguishes them.

External (non‑Python) MCP servers
  npm‑/uvx‑based servers (filesystem, fetch, serper, etc.) are NOT Python
  plugins — they live inside ``mcp-plugin.json5`` and are connected by the
  ``mcp-plugin`` gateway, not by the harness.
"""

import pkgutil
import logging

logger = logging.getLogger(__name__)


def _scan_builtins() -> list[tuple[str, str]]:
    """Scan ``slife.plugins.*`` for packages containing ``server.py``.

    Returns ``(name, module_path)`` tuples::

        [("memdb", "slife.plugins.memdb.server"),
         ("wechat", "slife.plugins.wechat.server"),
         …]
    """
    import slife.plugins as _pkg

    plugins: list[tuple[str, str]] = []

    for _, name, is_pkg in pkgutil.iter_modules(
        _pkg.__path__, _pkg.__name__ + "."
    ):
        if not is_pkg:
            continue
        short_name = name.split(".")[-1]
        server_module = name + ".server"

        # Check that server.py exists — use find_spec to avoid importing the
        # module (it contains FastMCP setup that must run in the child
        # process, not here). pkgutil.find_loader was deprecated in 3.12.
        try:
            import importlib.util as _util
            if _util.find_spec(server_module) is None:
                continue
            plugins.append((short_name, server_module))
        except Exception:
            continue
    return plugins


def discover_plugins(
    external: list[dict] | None = None,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Merge built-in source-scan plugins with config-declared external ones.

    Args:
        external: The ``plugins.external`` list parsed from slife.json5 —
            ``[{"name": ..., "module": ...}, ...]``.  An entry whose module
            cannot be imported is skipped (logged and returned as missing so
            the caller can surface it); on a name collision with a built-in
            the external entry wins (a standalone distribution that replaces
            a built-in, e.g. ``mcp``).

    Returns a ``(plugins, missing)`` pair of ``(name, module_path)`` tuples:
    the discovered plugins (e.g. ``[("memdb", "slife.plugins.memdb.server"),
    …]``) plus the external entries declared in config whose module could not
    be imported — typically an uninstalled package.  The missing set is the
    caller's cue to report a plugin load failure in the UI.
    """
    plugins = _scan_builtins()
    seen = {name for name, _ in plugins}
    missing: list[tuple[str, str]] = []

    for entry in external or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        module = entry.get("module")
        if not name or not module:
            logger.warning("plugins_external_skip entry=%s", entry)
            continue
        try:
            import importlib.util as _util
            if _util.find_spec(str(module)) is None:
                logger.warning(
                    "plugins_external_missing name=%s module=%s", name, module,
                )
                missing.append((name, str(module)))
                continue
        except Exception:
            logger.warning(
                "plugins_external_missing name=%s module=%s", name, module,
            )
            missing.append((name, str(module)))
            continue
        if name in seen:
            plugins = [(n, m) for n, m in plugins if n != name]
        plugins.append((name, str(module)))
        seen.add(name)

    logger.debug("plugins_discovered count=%d names=%s",
                 len(plugins), [n for n, _ in plugins])
    return plugins, missing