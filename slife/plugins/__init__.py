"""Slife plugin auto-discovery — internal plugins as child processes.

Every plugin is a Python package under ``slife.plugins.*`` with a
``server.py`` entry point exposing a ``main()`` that hosts its own FastMCP
server.  This is the ONE framework for every plugin: discovery is a source
scan of ``slife.plugins.*`` (``memdb``, ``memfiles``, ``wechat``,
``sharefile``, ``a2a``, ``media``, ``job-coding``, ``mcp``).  There is no
config-driven "external plugin" registration — third-party capability
enters only as a standard MCP server in ``mcp-plugin.json5``, connected by
the internal ``mcp`` gateway.

Once discovered, everything feeds the identical generic lifecycle (spawn via
``sys.executable -m <module>``, connect over Streamable HTTP, register tools,
watchdog) — the runtime never distinguishes plugins.
"""

import pkgutil
import logging

logger = logging.getLogger(__name__)


#: Public-name override for built-in packages whose canonical plugin name
#: uses a hyphen (Python package names cannot).  Plugin names are hyphenated
#: in the UI/health/tool prefixes, while module paths stay snake_case.
_PUBLIC_NAME_OVERRIDE: dict[str, str] = {"job_coding": "job-coding"}


def _scan_builtins() -> list[tuple[str, str]]:
    """Scan ``slife.plugins.*`` for packages containing ``server.py``.

    Returns ``(name, module_path)`` tuples::

        [("memdb", "slife.plugins.memdb.server"),
         ("wechat", "slife.plugins.wechat.server"),
         ("job-coding", "slife.plugins.job_coding.server"),
         …]
    """
    import slife.plugins as _pkg

    plugins: list[tuple[str, str]] = []

    for _, name, is_pkg in pkgutil.iter_modules(
        _pkg.__path__, _pkg.__name__ + "."
    ):
        if not is_pkg:
            continue
        short_name = _PUBLIC_NAME_OVERRIDE.get(
            name.split(".")[-1], name.split(".")[-1]
        )
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


def discover_plugins() -> list[tuple[str, str]]:
    """Return every discovered plugin as ``(name, module_path)`` pairs.

    Pure source-scan of the internal ``slife.plugins.*`` packages — there is
    no external registration (the ``plugins.external`` mechanism was
    removed; third-party capability enters via standard MCP servers in
    ``mcp-plugin.json5`` through the internal ``mcp`` gateway).
    """
    plugins = _scan_builtins()
    logger.debug("plugins_discovered count=%d names=%s",
                 len(plugins), [n for n, _ in plugins])
    return plugins