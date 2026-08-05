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

logger = logging.getLogger(__name__)

#: Ordered list of status entries recorded during startup / runtime.
_entries: list[dict] = []


def record(
    component: str,
    level: str,
    *,
    key: str = "",
    value: str = "",
    hint: str = "",
) -> None:
    """Push a status entry.

    *component*: subsystem name ("embeddings", "memdb", "mcp", …).
    *level*: "ok", "warning", "error".
    *key* / *value*: structured k=v for programmatic consumption.
    *hint*: human-readable remediation or context.
    """
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


def get_report() -> list[dict]:
    """Return all recorded status entries, newest last."""
    return list(_entries)


def clear() -> None:
    """Clear all entries (e.g. on re-init)."""
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
            r = _sp.run(["node", "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                record("node", "ok", key="version", value=r.stdout.strip(),
                        hint="Node.js found — fetch MCP can use Readability.js for article extraction.")
            else:
                record("node", "warning", key="exit", value=str(r.returncode),
                        hint="node exists but returned non-zero. Fetch MCP falls back to pure-Python extraction.")
        except Exception:
            record("node", "warning", key="error", value="unexpected error",
                    hint="node check failed. Fetch MCP uses pure-Python extraction.")
    else:
        record("node", "warning", key="missing", value="not found",
                hint="Node.js not installed. Re-run install script or install manually from https://nodejs.org. Fetch MCP uses pure-Python extraction.")

    if npm_path:
        try:
            # `npm --version` is a local, lock-free check — unlike `npm version`,
            # which can block on the npm cache lock while many npx servers are
            # warming up concurrently, causing spurious timeouts at startup.
            npm_cmd = ["cmd", "/c", "npm", "--version"] if _sys.platform == "win32" else ["npm", "--version"]
            r = _sp.run(npm_cmd, capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                record("npm", "ok", key="version", value=(r.stdout.strip() or "?"),
                        hint="npm found.")
            else:
                record("npm", "warning", key="exit", value=str(r.returncode),
                        hint="npm exists but returned non-zero.")
        except Exception:
            record("npm", "warning", key="error", value="unexpected error",
                    hint="npm check failed.")
    else:
        record("npm", "warning", key="missing", value="not found",
                hint="npm not installed. Re-run install script or install Node.js from https://nodejs.org.")

    # ── bun / bunx (used to run Node.js MCP servers) ──
    bun_path = _shutil.which("bun")
    bunx_path = _shutil.which("bunx")

    if bun_path:
        try:
            r = _sp.run(["bun", "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                record("bun", "ok", key="version", value=r.stdout.strip(),
                        hint="bun found — JavaScript/TypeScript MCP servers can run via bunx.")
            else:
                record("bun", "warning", key="exit", value=str(r.returncode),
                        hint="bun exists but returned non-zero.")
        except Exception:
            record("bun", "warning", key="error", value="unexpected error",
                    hint="bun check failed.")
    else:
        record("bun", "warning", key="missing", value="not found",
                hint="bun not installed. Run the install script or install from https://bun.sh. "
                     "nvidia-nim MCP server requires bunx; npm/npx fallback used for all other servers.")

    # ── uv / uvx (used to run Python MCP servers) ──
    uv_path = _shutil.which("uv")
    if uv_path:
        try:
            r = _sp.run(["uv", "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                record("uv", "ok", key="version", value=r.stdout.strip(),
                        hint="uv found — MCP servers can be spawned via uvx.")
            else:
                record("uv", "warning", key="exit", value=str(r.returncode),
                        hint="uv exists but returned non-zero.")
        except Exception:
            record("uv", "warning", key="error", value="unexpected error")
    else:
        record("uv", "warning", key="missing", value="not found",
                hint="uv not installed. Re-run the install script or install from https://astral.sh.")
