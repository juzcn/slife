"""Slife plugin auto-discovery — internal plugins as child processes.

Every plugin is a Python package under ``slife.plugins.*`` with a
``server.py`` entry point exposing a ``main()`` that hosts its own FastMCP
server.  This is the ONE framework for every plugin: discovery returns the
canonical plugin list, and once discovered everything feeds the identical
generic lifecycle (spawn via ``sys.executable -m <module>``, connect over
Streamable HTTP, register tools, watchdog).

Discovery is **spec-driven**: the authoritative list and start order come
from :data:`slife.plugins.spec.PLUGIN_SPECS` — the central plugin contract
that also drives the registry, watchdog, system-health enumeration and tool
routing.  A package under ``slife.plugins.*`` with no spec row is still
picked up by a source scan and started through the same generic lifecycle
(default spec), so discovery stays open for future internal plugins without
harness changes.

There is no config-driven "external plugin" registration — third-party
capability enters only as a standard MCP server in ``mcp-plugin.json5``,
connected by the internal ``mcp`` gateway.

This module must stay import-light: ``slife.plugins.spec`` (stdlib only) and
`pkgutil`.  It is imported by the mcp plugin child process and by
``tools.system``.
"""

import pkgutil
import logging

from slife.plugins.spec import PLUGIN_SPECS

logger = logging.getLogger(__name__)


#: Public-name override for built-in packages whose canonical plugin name
#: uses a hyphen (Python package names cannot).  Plugin names are hyphenated
#: in the UI/health/tool prefixes, while module paths stay snake_case.
#: Spec-declared plugins carry their public name in ``PLUGIN_SPECS``; this
#: override only shapes how *undeclared* packages are named on discovery.
_PUBLIC_NAME_OVERRIDE: dict[str, str] = {"job_coding": "job-coding"}


def _server_module_exists(server_module: str) -> bool:
    """True if *server_module* is importable.

    Use ``find_spec`` to avoid importing the module (it contains FastMCP
    setup that must run in the child process, not here).  pkgutil.find_loader
    was deprecated in 3.12.
    """
    import importlib.util as _util
    try:
        return _util.find_spec(server_module) is not None
    except Exception:
        return False


def _scan_undeclared() -> list[tuple[str, str]]:
    """Source-scan ``slife.plugins.*`` for packages (with a ``server.py``)
    that have no ``PLUGIN_SPECS`` entry — the open-discovery path for future
    internal plugins.

    Returns ``(public_name, module_path)`` pairs.
    """
    # A spec-declared module must never also be source-scanned as
    # "undeclared" — identity match, not name match: PLUGIN_SPECS is keyed by
    # the hyphenated PUBLIC name while the package leaf is snake_case
    # (mcp-gateway ↔ mcp_gateway), so a leaf:in-PLUGIN_SPECS check would miss
    # the collision and spawn the same server.module twice.
    declared_modules = {s.module for s in PLUGIN_SPECS.values()}

    import slife.plugins as _pkg

    plugins: list[tuple[str, str]] = []

    for _, name, is_pkg in pkgutil.iter_modules(
        _pkg.__path__, _pkg.__name__ + "."
    ):
        if not is_pkg:
            continue
        leaf = name.split(".")[-1]
        server_module = name + ".server"
        if server_module in declared_modules:
            continue  # spec-declared — handled by the canonical pass
        if not _server_module_exists(server_module):
            continue
        public = _PUBLIC_NAME_OVERRIDE.get(leaf, leaf)
        plugins.append((public, server_module))
    return plugins


def discover_plugins() -> list[tuple[str, str]]:
    """Return every discovered plugin as ``(name, module_path)`` pairs.

    Canonical order: every spec-declared plugin whose ``server.py`` exists,
    in :data:`PLUGIN_SPECS` order; then any undeclared package under
    ``slife.plugins.*`` (a future internal plugin starts through the same
    generic lifecycle via its default spec).

    Pure source scan of internal packages — there is no external
    registration (third-party capability enters via standard MCP servers in
    ``mcp-plugin.json5`` through the internal ``mcp`` gateway).
    """
    plugins: list[tuple[str, str]] = []
    seen: set[str] = set()

    # Spec-declared plugins, canonical order.
    for name, spec in PLUGIN_SPECS.items():
        if _server_module_exists(spec.module):
            plugins.append((name, spec.module))
            seen.add(name)

    # Open discovery for any undeclared package under slife.plugins.*.
    for name, module in _scan_undeclared():
        if name not in seen:
            plugins.append((name, module))
            seen.add(name)

    logger.debug("plugins_discovered count=%d names=%s",
                 len(plugins), [n for n, _ in plugins])
    return plugins
