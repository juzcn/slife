"""Slife plugin auto-discovery — like native tools, but as child processes.

Each plugin is a Python package under ``slife.plugins/<name>/`` with a
``server.py`` entry point.  The harness auto-discovers them at startup
via ``pkgutil.iter_modules`` — no config entry needed.

Third-party plugin
  Drop a package into ``slife/plugins/my_plugin/`` with a ``server.py``
  that follows the plugin contract (DESIGN.md → "The Plugin Contract").
  It will be discovered and started automatically on next launch.

Built-in plugins
  ``mcp``, ``memdb``, ``wechat``, ``memfiles``, ``sharefile``, and ``a2a``
  are discovered the same way.  Each has a small amount of harness-side
  post‑connect logic (MCP auto‑connect, memory restore, WeChat poll loop,
  memfiles/sharefile client wiring, A2A mesh) that is triggered by plugin
  name rather than by special registration.

External (non‑Python) MCP servers
  npm‑/uvx‑based servers (filesystem, fetch, serper, etc.) are NOT
  Python plugins — they are configured in ``slife.json5`` →
  ``mcp.servers`` and connected via the ``mcp_set`` tool.
"""

import pkgutil
import logging

logger = logging.getLogger(__name__)


def discover_plugins() -> list[tuple[str, str]]:
    """Scan ``slife.plugins.*`` for packages containing ``server.py``.

    Returns a list of ``(name, module_path)`` tuples::

        [("memdb", "slife.plugins.memdb.server"),
         ("mcp",    "slife.plugins.mcp.server"),
         ("wechat", "slife.plugins.wechat.server"),
         …]

    Third-party packages under ``slife.plugins/`` are discovered
    automatically — just add a ``server.py`` with a ``main()``.
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

    logger.debug("plugins_discovered count=%d names=%s",
                 len(plugins), [n for n, _ in plugins])
    return plugins
