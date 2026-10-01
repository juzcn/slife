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

import slife.timeouts as _T  # module ref — call-time lookup, reload/patch-safe
# (``slife.timeouts`` is stdlib-only by design, so importing it here keeps this
# store importable from the earliest startup path — the constraint the
# TYPE_CHECKING import below exists to respect.)

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
    bounded by ``ready.probe_toolchain`` — so it runs on a daemon thread:
    nothing waits on it, and ``system_health`` reads the entries lazily.  On a
    host with broken shims that is four such bounds no startup ever pays.
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


# ── External tooling availability check ─────────────────────────────────


def _probe_version(
    name: str, *,
    missing_hint: str, exit_hint: str, error_hint: str,
    wrap_cmd_on_windows: bool = False,
) -> None:
    """Probe one external tool's ``--version`` and record its health verdict.

    The shared shape behind the node / npm / bun / uv checks: ``which`` →
    run ``--version`` → record ``ok`` with the version, or a ``warning``
    carrying the non-zero exit code, the timeout, or the unexpected error —
    and a ``warning`` with a ``missing`` hint when the tool is absent.  A sync
    subprocess bounded by ``ready.probe_toolchain`` runs in a daemon-thread
    diagnostic, never on the event loop.

    Every failure is CLASSIFIED and LOGGED, because the verdict is what the
    model reads and repeats to the user.  A bare ``except Exception`` that
    recorded the literal string "unexpected error" — which is what this was —
    cost exactly that: a report asserting npm was broken and prescribing a
    Node.js reinstall, on a machine where ``npm --version`` answers in 0.2 s,
    with nothing in the log to say why.  A timeout now says it timed out and
    claims nothing about the tool; the other failures carry the exception
    itself rather than a placeholder.
    """
    import shutil as _shutil
    import subprocess as _sp
    import sys as _sys
    import tempfile

    if _shutil.which(name) is None:
        record(name, "warning", key="missing", value="not found",
               hint=missing_hint)
        return
    # npm / bun resolve through ``cmd`` on Windows (no .exe on PATH as a
    # bare name); node / uv exec directly.
    cmd = (
        ["cmd", "/c", name, "--version"]
        if wrap_cmd_on_windows and _sys.platform == "win32"
        else [name, "--version"]
    )
    bound = _T.timeouts.ready.probe_toolchain  # call-time lookup

    # The child writes to FILES, never pipes — the same reason the MCP stdio
    # child does (``connection.py``).  On timeout ``subprocess.run`` kills the
    # direct child and then ``communicate()``s; over a pipe that call waits for
    # the FAR end to close, and on Windows the grandchild under ``cmd.exe``
    # (``npm.cmd`` runs ``node``) survives the kill and still holds it.
    # Measured: a 1 s bound took 30.07 s that way.  A file has no far end, so
    # the bound is the bound.
    #
    # stdout and stderr stay APART: the version is stdout's first line, and a
    # tool that emits a warning on stderr first must not have that warning read
    # back as its version.  stderr is kept for the failure paths — a non-zero
    # exit's own output is the diagnostic, and it used to be discarded.
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        try:
            r = _sp.run(cmd, stdout=out_f, stderr=err_f, timeout=bound)
        except _sp.TimeoutExpired:
            logger.warning("toolchain_probe_timeout name=%s bound=%.1fs",
                           name, bound)
            record(
                name, "warning", key="timeout", value=f"no answer in {bound:g}s",
                hint=(f"{name} did not answer its version probe — its state is "
                      f"unverified and it may be fine. Restart slife to "
                      f"re-probe."),
            )
            return
        except Exception as e:  # noqa: BLE001 — a probe never takes down the collector
            logger.warning("toolchain_probe_failed name=%s err=%s", name, e,
                           exc_info=True)
            record(name, "warning", key="error",
                   value=f"{type(e).__name__}: {e}", hint=error_hint)
            return

        out_f.seek(0)
        version = out_f.read().decode("utf-8", errors="replace").strip()
        if r.returncode != 0:
            err_f.seek(0)
            detail = err_f.read().decode("utf-8", errors="replace").strip()
            logger.warning("toolchain_probe_exit name=%s code=%s err=%s",
                           name, r.returncode, (detail or version)[:200])
            record(name, "warning", key="exit", value=str(r.returncode),
                   hint=exit_hint)
            return

    record(name, "ok", key="version",
           value=(version.splitlines()[0].strip() if version else "?"))


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
