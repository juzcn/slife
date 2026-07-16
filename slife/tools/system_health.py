"""System health check — reports startup status that logs capture but
the agent can't see.

Call this early in a conversation to discover silently-degraded
subsystems: embedding failures, schema migration errors, missing
Python packages, MCP connection issues, etc.  All of these are
invisible to the agent otherwise — they only appear in log files.
"""

import json
import logging

from slife.tools.base import Tool
from slife.health import get_report as get_startup_records

logger = logging.getLogger(__name__)


# ── Active checks (run every time the tool is called) ──────────────


def _check_runtime_imports() -> list[dict]:
    """Verify that backend Python packages are importable."""
    results: list[dict] = []
    from slife.plugins.memory.embeddings import _BACKEND_RUNTIME_IMPORTS
    for backend, (pkg, pip_name) in _BACKEND_RUNTIME_IMPORTS.items():
        try:
            __import__(pkg)
            results.append({
                "component": "runtime",
                "level": "ok",
                "key": f"{backend}_import",
                "value": pkg,
                "hint": f"{pkg} is importable.",
            })
        except ImportError:
            results.append({
                "component": "runtime",
                "level": "warning",
                "key": f"{backend}_import",
                "value": pkg,
                "hint": f"{pkg} is NOT installed. Install: pip install {pip_name}",
            })
    return results


def _check_embedding_config() -> list[dict]:
    """Check the embedding backend configuration and runtime usability."""
    results: list[dict] = []
    from slife.plugins.memory.embeddings import EmbeddingClient
    from slife.plugins.memory.embedding_config import read_embedding_config

    client = EmbeddingClient.from_config()
    cfg = read_embedding_config()

    if cfg is None:
        results.append({
            "component": "embeddings",
            "level": "warning",
            "key": "backend",
            "value": "none",
            "hint": (
                "No embedding backend configured. "
                "Semantic search (hybrid mode) will NOT work. "
                "Keyword search (grep/fts5/time) still works normally. "
                "Use memory_set_embedding to configure one: "
                "GGUF local model or OpenAI-compatible API."
            ),
        })
        return results

    backend = client.backend
    available = client.available

    if available:
        if backend == "gguf":
            gguf_path = cfg.get("gguf_path", "unknown")
            results.append({
                "component": "embeddings",
                "level": "ok",
                "key": "backend",
                "value": "gguf",
                "hint": f"GGUF model ready: {cfg.get('model', '?')} "
                        f"(dim={client.dimension}, path={gguf_path})",
            })
        else:
            results.append({
                "component": "embeddings",
                "level": "ok",
                "key": "backend",
                "value": "api",
                "hint": f"API embeddings ready: {cfg.get('model', '?')} "
                        f"(dim={client.dimension})",
            })
    else:
        if backend == "gguf":
            gguf_path = cfg.get("gguf_path", "unknown")
            results.append({
                "component": "embeddings",
                "level": "warning",
                "key": "backend",
                "value": "gguf",
                "hint": (
                    f"GGUF file exists ({gguf_path}) but "
                    "llama-cpp-python is NOT installed. "
                    "Semantic search (hybrid mode) will NOT work. "
                    "Install with: pip install llama-cpp-python. "
                    "Keyword search (grep/fts5/time) still works normally."
                ),
            })
        elif backend == "api":
            results.append({
                "component": "embeddings",
                "level": "warning",
                "key": "backend",
                "value": "api",
                "hint": (
                    "API key configured but openai package is NOT installed. "
                    "Semantic search (hybrid mode) will NOT work. "
                    "Install with: pip install openai. "
                    "Keyword search (grep/fts5/time) still works normally."
                ),
            })
        else:
            results.append({
                "component": "embeddings",
                "level": "warning",
                "key": "backend",
                "value": "unknown",
                "hint": (
                    "Embedding backend is unavailable for unknown reasons. "
                    "Semantic search (hybrid mode) will NOT work. "
                    "Keyword search (grep/fts5/time) still works normally."
                ),
            })
    return results


# ── Grouping ───────────────────────────────────────────────────────


def _group_by_component(entries: list[dict]) -> dict[str, list[dict]]:
    """Group flat entry list by component for structured display."""
    groups: dict[str, list[dict]] = {}
    for e in entries:
        comp = e.get("component", "unknown")
        groups.setdefault(comp, []).append(e)
    return groups


def _component_status(entries: list[dict]) -> str:
    """Worst status across a group: ok < warning < error."""
    levels = {e.get("level", "ok") for e in entries}
    if "error" in levels:
        return "error"
    if "warning" in levels:
        return "warning"
    return "ok"


def _build_summary(groups: dict[str, list[dict]]) -> str:
    """One-line summary: '3 ok, 2 warnings (embeddings, memory), 0 errors'."""
    ok_count = sum(1 for es in groups.values() if _component_status(es) == "ok")
    warn_comps = [
        comp for comp, es in groups.items()
        if _component_status(es) == "warning"
    ]
    err_comps = [
        comp for comp, es in groups.items()
        if _component_status(es) == "error"
    ]
    parts: list[str] = [f"{ok_count} ok"]
    if warn_comps:
        parts.append(f"{len(warn_comps)} warning(s): {', '.join(warn_comps)}")
    if err_comps:
        parts.append(f"{len(err_comps)} error(s): {', '.join(err_comps)}")
    return "; ".join(parts)


def _overall_healthy(groups: dict[str, list[dict]]) -> bool:
    return all(
        _component_status(es) == "ok"
        for es in groups.values()
    )


# ── Tool ───────────────────────────────────────────────────────────


class SystemHealthTool(Tool):
    """Report system startup health — embedding availability, schema
    migration errors, missing packages, MCP server status, etc.

    The agent should call this at the start of a conversation (or when
    the user first asks about system capabilities) to discover problems
    that are only visible in log files.
    """

    name = "system_health"
    description = (
        "Check Slife system health. Reports embedding backend status, "
        "schema migration errors, missing Python packages, MCP server "
        "connection status, and other startup issues that are only logged "
        "to files (invisible to you). "
        "Call this at conversation start or when the user asks about "
        "system capabilities — silently-degraded features can't be "
        "detected otherwise."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        # Phase 1: Pre-recorded entries from startup (config, model,
        #          memory_service, mcp_wrapper, mcp_server, a2a, subagent)
        startup = get_startup_records()

        # Phase 2: Dynamic checks — run fresh every call
        runtime = _check_runtime_imports()
        embedding = _check_embedding_config()

        all_entries = startup + runtime + embedding
        groups = _group_by_component(all_entries)

        # Build per-component status
        components: dict[str, dict] = {}
        for comp, entries in groups.items():
            components[comp] = {
                "status": _component_status(entries),
                "entries": entries,
            }

        result = {
            "healthy": _overall_healthy(groups),
            "summary": _build_summary(groups),
            "components": components,
        }
        return json.dumps(result, ensure_ascii=False, indent=2)
