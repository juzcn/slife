"""Slife plugin auto-discovery — like native tools, but as child processes.

Each plugin is a Python package under ``slife.plugins/<name>/`` with a
``server.py`` entry point.  The harness auto-discovers them at startup
via ``pkgutil.iter_modules`` — no config entry needed.

Third-party plugin
  Drop a package into ``slife/plugins/my_plugin/`` with a ``server.py``
  that follows the :doc:`plugin spec </docs/plugins>`.  It will be
  discovered and started automatically on next launch.

Built-in plugins
  ``memory``, ``mcp``, and ``wechat`` are discovered the same way.
  They each have a small amount of harness-side post‑connect logic
  (memory restore, MCP auto‑connect, WeChat poll loop) that is
  triggered by plugin name rather than by special registration.

External (non‑Python) MCP servers
  npm‑/uvx‑based servers (filesystem, fetch, serper, etc.) are NOT
  Python plugins — they are configured in ``slife.json5`` →
  ``mcp.servers`` and connected via the ``mcp_add_server`` tool.
"""

import pkgutil
import logging

logger = logging.getLogger(__name__)


def discover_plugins() -> list[tuple[str, str]]:
    """Scan ``slife.plugins.*`` for packages containing ``server.py``.

    Returns a list of ``(name, module_path)`` tuples::

        [("memory", "slife.plugins.memory.server"),
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

        # Check that server.py exists — use the loader to avoid
        # importing the module (it contains FastMCP setup that must
        # run in the child process, not here).
        try:
            loader = pkgutil.find_loader(server_module)
            if loader is None:
                continue
            plugins.append((short_name, server_module))
        except Exception:
            continue

    logger.debug("plugins_discovered count=%d names=%s",
                 len(plugins), [n for n, _ in plugins])
    return plugins
