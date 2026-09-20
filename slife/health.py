"""Startup health collector — subsystems push status here during init.

Builtin tools (like ``system_health``) read from this module to report
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
from typing import TYPE_CHECKING

# Annotation-only: the store must stay importable from the earliest startup
# path (``slife/__init__.py``), so it never imports config at runtime.
if TYPE_CHECKING:
    from slife.config import ModelConfig

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


def record_active_model(model: ModelConfig) -> None:
    """Record the active model's facts for ``system_health``.

    Called at startup AND on every live switch (``reload_active_model``):
    the report's ``model`` line is the only place the model describes
    itself (the system prompt carries no model name), and a switch would
    otherwise leave the session's opening model in the report.
    ``replace=True`` keeps the store at exactly one ``model`` entry — the
    one the last switch wrote — which is also what the report's own merge
    layer assumes.  One definition for both producers: a second inline copy
    of this text is what drifts.
    """
    record(
        "model", "ok", key="active", replace=True,
        value=(
            f"{model.ref} (thinking="
            f"{'on' if model.thinking_enabled else 'off'}, "
            f"vision={'on' if model.supports_vision else 'off'}, "
            f"ctx {model.context_window})"
        ),
    )


def record_host_facts(config, *, source: str) -> None:
    """Record the facts that are about the HOST, not about this process.

    The config's provenance and counts, the active model, and the external
    tools' versions: any process that loads the same config on the same
    machine sees the same values, so they belong in every process's report.  A
    subagent worker recorded none of them, and reported 14 components against
    its parent's 20 for no reason a reader of either report could see.

    One recorder for both entry points (``slife/__init__.py:main`` and
    ``slife/subagent/headless.py``): the two reports are meant to be
    comparable, and a second copy of this block is what drifts.

    *source* is where THIS process got its config — the yaml path for the main
    agent, and for a worker the inherited transfer, which is a fact about
    provenance rather than a path anyone can open.

    The toolchain probe is the expensive part — four subprocesses, each
    bounded at 5s — so it runs on a daemon thread: nothing waits on it, and
    ``system_health`` reads the entries lazily.  On a host with broken shims
    that is ~20s no startup ever pays.
    """
    import threading

    mcp_servers = 0
    try:
        from slife.plugins.mcp_gateway import config as _mcp_cfg
        mcp_servers = _mcp_cfg.count_servers()
    except Exception:
        pass
    embeddings = config.embeddings_config
    # The fact lives in ``value`` (a healthy report prints values only) — the
    # counts tell the reader which config is actually live.
    record(
        "config", "ok",
        key="path", value=(
            f"{source} ({len(config.models)} models, {mcp_servers} MCP "
            f"servers, embeddings="
            f"{'enabled' if (embeddings and embeddings.enabled and embeddings.active_model) else 'disabled'})"
        ),
    )
    record_active_model(config.active_model)
    threading.Thread(
        target=check_external_deps, name="ext-deps-check", daemon=True,
    ).start()


def get_report() -> list[dict]:
    """Return all recorded status entries, newest last."""
    with _lock:
        return list(_entries)


def clear() -> None:
    """Clear all entries (e.g. on re-init)."""
    with _lock:
        _entries.clear()


# ── External tooling availability check ─────────────────────────────────


def _probe_version(
    name: str, *,
    missing_hint: str, exit_hint: str, error_hint: str,
    wrap_cmd_on_windows: bool = False,
) -> None:
    """Probe one external tool's ``--version`` and record its health verdict.

    The shared shape behind the node / npm / bun / uv checks: ``which`` →
    run ``--version`` → record ``ok`` with the version, a ``warning`` with
    the non-zero exit code or the unexpected error, or a ``warning`` with a
    ``missing`` hint when the tool is absent.  A 5 s sync subprocess runs in
    a daemon-thread diagnostic, never on the event loop.
    """
    import shutil as _shutil
    import subprocess as _sp
    import sys as _sys

    if _shutil.which(name) is None:
        record(name, "warning", key="missing", value="not found",
               hint=missing_hint)
        return
    try:
        # npm / bun resolve through ``cmd`` on Windows (no .exe on PATH as a
        # bare name); node / uv exec directly.
        cmd = (
            ["cmd", "/c", name, "--version"]
            if wrap_cmd_on_windows and _sys.platform == "win32"
            else [name, "--version"]
        )
        r = _sp.run(cmd, capture_output=True, text=True, timeout=5)  # noqa-timeout — sync probe in a daemon-thread diagnostic
    except Exception:
        record(name, "warning", key="error", value="unexpected error",
               hint=error_hint)
        return
    if r.returncode == 0:
        record(name, "ok", key="version", value=(r.stdout.strip() or "?"))
    else:
        record(name, "warning", key="exit", value=str(r.returncode),
               hint=exit_hint)


def check_external_deps() -> None:
    """Check that optional external tools are available.

    Reports status via the health system so ``system_health`` can
    surface missing tools to the LLM / user.  Does NOT attempt to
    install anything — the one-click install scripts handle that.
    """
    # ── Node.js / npm (used by readabilipy for article extraction) ──
    _probe_version(
        "node",
        missing_hint="Install Node.js from https://nodejs.org (fetch falls back "
                     "to pure-Python extraction without it).",
        exit_hint="Reinstall Node.js from https://nodejs.org — fetch "
                  "falls back to pure-Python extraction meanwhile.",
        error_hint="Reinstall Node.js from https://nodejs.org.",
    )
    _probe_version(
        "npm",
        # ``npm --version`` is a local, lock-free check — unlike ``npm
        # version``, which can block on the npm cache lock while many npx
        # servers are warming up concurrently, causing spurious timeouts.
        wrap_cmd_on_windows=True,
        missing_hint="Install Node.js from https://nodejs.org — npx-based MCP "
                     "servers cannot start without it.",
        exit_hint="Reinstall Node.js from https://nodejs.org — npx-based "
                  "MCP servers cannot start meanwhile.",
        error_hint="Reinstall Node.js from https://nodejs.org.",
    )
    # ── bun (used to run Node.js MCP servers) ──
    _probe_version(
        "bun",
        wrap_cmd_on_windows=True,
        missing_hint="Optional: install from https://bun.sh. JS/TS MCP servers "
                     "run via npx without it.",
        exit_hint="Reinstall from https://bun.sh, or ignore — JS/TS MCP "
                  "servers also run via npx.",
        error_hint="Reinstall from https://bun.sh, or ignore — JS/TS MCP "
                   "servers also run via npx.",
    )
    # ── uv / uvx (used to run Python MCP servers) ──
    _probe_version(
        "uv",
        missing_hint="Install from https://astral.sh. uvx-based MCP servers "
                     "cannot start without it.",
        exit_hint="Reinstall from https://astral.sh. uvx-based MCP "
                  "servers cannot start meanwhile.",
        error_hint="Reinstall from https://astral.sh. uvx-based MCP "
                   "servers cannot start meanwhile.",
    )
