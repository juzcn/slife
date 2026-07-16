"""Startup health collector — subsystems push status here during init.

Native tools (like ``system_health``) read from this module to report
system status to the LLM.  Logs are invisible to the agent; this module
bridges that gap.

Usage::

    from slife.health import record
    record("embeddings", "warning", key="backend", value="gguf",
           hint="llama-cpp-python not installed. pip install llama-cpp-python")

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

    *component*: subsystem name ("embeddings", "memory", "mcp", …).
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
