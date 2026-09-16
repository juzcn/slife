"""Startup health collector — subsystems push status here during init.

Native tools (like ``system_health``) read from this module to report
system status to the LLM.  Logs are invisible to the agent; this module
bridges that gap.

Usage::

    from slife.health import record
    record("embeddings", "warning", key="backend", value="gguf",
           hint="llama-cpp-python not installed. uv pip install llama-cpp-python")

    from slife.health import get_report
    report = get_report()  # → list[dict]
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

#: Ordered list of status entries recorded during startup / runtime.
_entries: list[dict] = []
_MAX_ENTRIES = 200  # bound — a long session must not grow the list forever
#: ``check_external_deps`` runs on a daemon thread while the event loop reads
#: ``get_report`` — serialize the slice-assignment/append/evict window so a
#: reader never interleaves with a ``replace`` mutation.
_lock = threading.Lock()


def record(
    component: str,
    level: str,
    *,
    key: str = "",
    value: str = "",
    hint: str = "",
    replace: bool = False,
) -> None:
    """Push a status entry.

    *component*: subsystem name ("embeddings", "memdb", "mcp-gateway", …).
    *level*: "ok", "warning", "error".
    *key* / *value*: structured k=v for programmatic consumption.
    *hint*: human-readable remediation or context.
    *replace*: when True, drop any earlier entry with the same
        ``(component, key)`` before appending — lets a later recovery
        (e.g. an MCP server reconnecting in the background) supersede a
        stale startup warning instead of leaving both in the report.
    """
    with _lock:
        if replace and key:
            _entries[:] = [
                e for e in _entries
                if not (e.get("component") == component and e.get("key") == key)
            ]
        entry: dict = {
            "component": component,
            "level": level,
        }
        if key:
            entry["key"] = key
        if value:
            entry["value"] = value
        if hint:
            entry["hint"] = hint
        _entries.append(entry)
        if len(_entries) > _MAX_ENTRIES:
            del _entries[:-_MAX_ENTRIES]


def get_report() -> list[dict]:
    """Return all recorded status entries, newest last."""
    with _lock:
        return list(_entries)


def clear() -> None:
    """Clear all entries (e.g. on re-init)."""
    with _lock:
        _entries.clear()


# ── External tooling availability check ─────────────────────────────────


def check_external_deps() -> None:
    """Check that optional external tools are available.

    Reports status via the health system so ``system_health`` can
    surface missing tools to the LLM / user.  Does NOT attempt to
    install anything — the one-click install scripts handle that.
    """
    import shutil as _shutil
    import subprocess as _sp
    import sys as _sys

    # ── Node.js / npm (used by readabilipy for article extraction) ──
    node_path = _shutil.which("node")
    npm_path = _shutil.which("npm")

    if node_path:
        try:
            r = _sp.run(["node", "--version"], capture_output=True, text=True, timeout=5)  # noqa-timeout — sync probe in a daemon-thread diagnostic
            if r.returncode == 0:
                record("node", "ok", key="version", value=r.stdout.strip())
            else:
                record("node", "warning", key="exit", value=str(r.returncode),
                        hint="Reinstall Node.js from https://nodejs.org — fetch "
                             "falls back to pure-Python extraction meanwhile.")
        except Exception:
            record("node", "warning", key="error", value="unexpected error",
                    hint="Reinstall Node.js from https://nodejs.org.")
    else:
        record("node", "warning", key="missing", value="not found",
                hint="Install Node.js from https://nodejs.org (fetch falls back "
                     "to pure-Python extraction without it).")

    if npm_path:
        try:
            # `npm --version` is a local, lock-free check — unlike `npm version`,
            # which can block on the npm cache lock while many npx servers are
            # warming up concurrently, causing spurious timeouts at startup.
            npm_cmd = ["cmd", "/c", "npm", "--version"] if _sys.platform == "win32" else ["npm", "--version"]
            r = _sp.run(npm_cmd, capture_output=True, text=True, timeout=5)  # noqa-timeout — sync probe in a daemon-thread diagnostic
            if r.returncode == 0:
                record("npm", "ok", key="version", value=(r.stdout.strip() or "?"))
            else:
                record("npm", "warning", key="exit", value=str(r.returncode),
                        hint="Reinstall Node.js from https://nodejs.org — npx-based "
                             "MCP servers cannot start meanwhile.")
        except Exception:
            record("npm", "warning", key="error", value="unexpected error",
                    hint="Reinstall Node.js from https://nodejs.org.")
    else:
        record("npm", "warning", key="missing", value="not found",
                hint="Install Node.js from https://nodejs.org — npx-based MCP "
                     "servers cannot start without it.")

    # ── bun (used to run Node.js MCP servers) ──
    bun_path = _shutil.which("bun")

    if bun_path:
        try:
            bun_cmd = ["cmd", "/c", "bun", "--version"] if _sys.platform == "win32" else ["bun", "--version"]
            r = _sp.run(bun_cmd, capture_output=True, text=True, timeout=5)  # noqa-timeout — sync probe in a daemon-thread diagnostic
            if r.returncode == 0:
                record("bun", "ok", key="version", value=r.stdout.strip())
            else:
                record("bun", "warning", key="exit", value=str(r.returncode),
                        hint="Reinstall from https://bun.sh, or ignore — JS/TS MCP "
                             "servers also run via npx.")
        except Exception:
            record("bun", "warning", key="error", value="unexpected error",
                    hint="Reinstall from https://bun.sh, or ignore — JS/TS MCP "
                         "servers also run via npx.")
    else:
        record("bun", "warning", key="missing", value="not found",
                hint="Optional: install from https://bun.sh. JS/TS MCP servers "
                     "run via npx without it.")

    # ── uv / uvx (used to run Python MCP servers) ──
    uv_path = _shutil.which("uv")
    if uv_path:
        try:
            r = _sp.run(["uv", "--version"], capture_output=True, text=True, timeout=5)  # noqa-timeout — sync probe in a daemon-thread diagnostic
            if r.returncode == 0:
                record("uv", "ok", key="version", value=r.stdout.strip())
            else:
                record("uv", "warning", key="exit", value=str(r.returncode),
                        hint="Reinstall from https://astral.sh. uvx-based MCP "
                             "servers cannot start meanwhile.")
        except Exception:
            record("uv", "warning", key="error", value="unexpected error",
                    hint="Reinstall from https://astral.sh. uvx-based MCP "
                         "servers cannot start meanwhile.")
    else:
        record("uv", "warning", key="missing", value="not found",
                hint="Install from https://astral.sh. uvx-based MCP servers "
                     "cannot start without it.")
