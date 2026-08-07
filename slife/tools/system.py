"""System introspection & health check tools.

Tools:
    check_embedding          — embedding backend status
    check_wechat             — WeChat plugin status
    check_watchdog           — plugin watchdog (auto-restart) status
    system_health            — orchestrate checks + startup records
    list_tools               — enumerate native vs MCP-proxied tools (with category filter)
    check_mcp_servers        — MCP server connection status

OS name, architecture, Python path/version, current shell, CWD,
environment mode, and package manager are in the system prompt.
Permissions and git status are covered by execute_shell / GitHub MCP.
check_os_info, check_shells, and check_workspace have been removed.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import ClassVar

from slife.paths import get_data_dir
from slife.tools.base import Tool
from slife.health import get_report as get_startup_records

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# check_embedding
# ═══════════════════════════════════════════════════════════════════════

def check_embedding() -> list[dict]:
    """Return embedding backend status as health-check entries."""
    results: list[dict] = []
    from slife.plugins.memdb.embeddings import EmbeddingClient
    from slife.plugins.memdb.embedding_config import read_embedding_config

    client = EmbeddingClient.from_config(quiet=True)
    cfg = read_embedding_config()

    if cfg is None:
        results.append({"component": "embeddings", "level": "warning", "key": "backend",
                        "value": "none",
                        "hint": ("No embedding backend configured. Semantic search (hybrid mode) will NOT work. "
                                 "Keyword search (grep/fts5/time) still works normally. "
                                 "Use memory_set_embedding to configure one: "
                                 "GGUF local model, transformer (sentence-transformers), or OpenAI-compatible API.")})
        return results

    backend = client.backend
    available = client.available

    if available:
        hints = {
            "gguf": f"GGUF model ready: {cfg.get('model', '?')} (dim={client.dimension}, path={cfg.get('gguf_path', 'unknown')})",
            "transformer": f"Transformer model ready: {cfg.get('model', '?')} (dim={client.dimension})",
        }
        results.append({"component": "embeddings", "level": "ok", "key": "backend",
                        "value": backend,
                        "hint": hints.get(backend, f"API embeddings ready: {cfg.get('model', '?')} (dim={client.dimension})")})
    else:
        warnings = {
            "gguf": (f"GGUF file exists ({cfg.get('gguf_path', 'unknown')}) but "
                     "llama-cpp-python is NOT installed. Semantic search (hybrid mode) will NOT work. "
                     "Install with: uv pip install llama-cpp-python. Keyword search (grep/fts5/time) still works normally."),
            "transformer": (f"Transformer model configured ({cfg.get('model', '?')}) but "
                            "sentence-transformers is NOT installed. Semantic search (hybrid mode) will NOT work. "
                            "Install with: uv pip install sentence-transformers. Keyword search (grep/fts5/time) still works normally."),
            "api": ("API key configured but openai package is NOT installed. "
                    "Semantic search (hybrid mode) will NOT work. "
                    "Install with: uv pip install openai. Keyword search (grep/fts5/time) still works normally."),
        }
        results.append({"component": "embeddings", "level": "warning", "key": "backend",
                        "value": backend,
                        "hint": warnings.get(backend, "Embedding backend is unavailable for unknown reasons. "
                                             "Semantic search (hybrid mode) will NOT work. "
                                             "Keyword search (grep/fts5/time) still works normally.")})
    return results


class CheckEmbeddingTool(Tool):
    """Check embedding backend for semantic memory search."""

    name = "check_embedding"
    category: ClassVar[str] = "System"
    description = "Embedding backend status (gguf/transformer/api/none) and availability."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return json.dumps(check_embedding(), ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# check_wechat
# ═══════════════════════════════════════════════════════════════════════

def _get_wechat_config():
    """Try to load slife config for wechat status.  Returns None on failure."""
    try:
        from slife.config import Config, parse_cli_agent
        agent_id = parse_cli_agent(sys.argv)
        cfg_path = get_data_dir() / "slife.json5"
        if cfg_path.exists():
            return Config.from_json5(cfg_path, agent_id=agent_id)
    except Exception:
        pass
    return None


def check_wechat(config=None) -> list[dict]:
    """Return WeChat plugin status as health-check entries."""
    results: list[dict] = []

    if config is None:
        config = _get_wechat_config()

    if config is None or config.wechat_config is None:
        results.append({"component": "wechat", "level": "ok", "key": "enabled",
                        "value": "unknown",
                        "hint": "WeChat plugin: config not available (no slife.json5?). "
                                "Default is enabled — will activate when config is loaded."})
        return results

    wc = config.wechat_config
    if not wc.enabled:
        results.append({"component": "wechat", "level": "ok", "key": "enabled",
                        "value": "disabled",
                        "hint": "WeChat plugin is disabled in config (wechat.enabled: false). "
                                "Set wechat.enabled: true in slife.json5 to enable."})
        return results

    try:
        from slife.plugins.wechat.config import load_wechat_config
        from slife.plugins.wechat.client import WechatClawbotClient
        SESSION_MAX_AGE = WechatClawbotClient.SESSION_MAX_AGE
    except ImportError:
        results.append({"component": "wechat", "level": "warning", "key": "status",
                        "value": "plugin_unavailable",
                        "hint": "WeChat plugin is enabled but the wechat package is not installed."})
        return results

    session = load_wechat_config(config.agent_id, get_data_dir())

    if not session.get("bot_token"):
        results.append({"component": "wechat", "level": "ok", "key": "status",
                        "value": "not_logged_in",
                        "hint": "WeChat plugin is enabled but not logged in. "
                                "Call wechat_login to scan QR code and connect."})
        return results

    saved_at = session.get("saved_at", 0)
    age = time.time() - saved_at
    remaining = max(0, SESSION_MAX_AGE - age)

    if remaining <= 0:
        results.append({"component": "wechat", "level": "warning", "key": "status",
                        "value": "session_expired",
                        "hint": f"WeChat session expired ({age / 3600:.1f}h old, max 23h). "
                                "Call wechat_login to re-scan QR code."})
    else:
        results.append({"component": "wechat", "level": "ok", "key": "status",
                        "value": "logged_in",
                        "hint": f"WeChat logged in. Session age: {age / 3600:.1f}h, "
                                f"remaining: {remaining / 3600:.1f}h."})
    return results


class CheckWechatTool(Tool):
    """Check WeChat plugin status: enabled, logged in, session expiry."""

    name = "check_wechat"
    category: ClassVar[str] = "System"
    description = "WeChat plugin status: disabled, not_logged_in, logged_in, or session_expired."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return json.dumps(check_wechat(), ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# check_watchdog
# ═══════════════════════════════════════════════════════════════════════

def check_watchdog() -> list[dict]:
    """Return plugin watchdog status from health records.

    Deduplicates by plugin name — only the latest record per plugin
    is kept.  Plugins without any record were never started (normal
    for subagents or when a plugin is disabled).
    """
    results: list[dict] = []
    records = get_startup_records()

    # Collect watchdog records, keep only latest per plugin
    seen: dict[str, dict] = {}
    for r in records:
        if r.get("component") != "watchdog":
            continue
        key = r.get("key", "")
        if key:
            seen[key] = r  # later records overwrite earlier ones

    if not seen:
        results.append({
            "component": "watchdog", "level": "ok",
            "key": "status", "value": "none",
            "hint": "No plugin watchdogs active (subagent, or plugins not started).",
        })
        return results

    for name in sorted(seen):
        r = seen[name]
        results.append(dict(r))  # copy — don't mutate health records

    return results


# ═══════════════════════════════════════════════════════════════════════
# check_mcp_servers
# ═══════════════════════════════════════════════════════════════════════

async def check_mcp_servers() -> list[dict]:
    """Return MCP server status by calling mcp_list_servers.

    Reports per-server: enabled/disabled, connected/disconnected state,
    disclosure mode (eager/lazy), tool count, transport, and errors.
    """
    results: list[dict] = []
    try:
        from slife.tools.registry import get_registry
        registry = get_registry()
        if registry is None:
            return [{"component": "mcp_servers", "level": "warning",
                     "key": "status", "value": "not_initialized",
                     "hint": "Tool registry not yet initialized — MCP status unavailable."}]

        mcp_list_tool = None
        for t in registry.list_tools():
            if t.name == "mcp_list_servers" or t.name.endswith("__mcp_list_servers"):
                mcp_list_tool = t
                break

        if mcp_list_tool is None:
            return [{"component": "mcp_servers", "level": "warning",
                     "key": "status", "value": "tool_missing",
                     "hint": "mcp_list_servers tool not found — slife-mcp may not be running."}]

        raw = await mcp_list_tool.execute()
        data = json.loads(raw)

        if not isinstance(data, list) or len(data) == 0:
            return [{"component": "mcp_servers", "level": "ok",
                     "key": "status", "value": "none",
                     "hint": "No external MCP servers configured."}]

        for server in data:
            name = server.get("name", "?")
            state = server.get("state", "unknown")
            tool_count = server.get("tool_count", 0)
            transport = server.get("transport", "")
            error_msg = server.get("error", "")
            enabled = server.get("enabled", True)
            active = server.get("active", True)

            if not enabled:
                # Disabled: intentionally not connected, no tools loaded.
                disclosure = "eager" if active else "lazy"
                results.append({
                    "component": "mcp_servers", "level": "info",
                    "key": name, "value": "disabled",
                    "enabled": False,
                    "state": "disabled",
                    "disclosure": disclosure,
                    "tool_count": 0,
                    "transport": transport,
                    "hint": f"MCP server '{name}' is disabled (disclosure={disclosure}, not connected).",
                })
            elif state == "running":
                disclosure = "eager" if active else "lazy"
                tool_note = f"{tool_count} tools loaded" if active else f"{tool_count} tools available (not loaded)"
                results.append({
                    "component": "mcp_servers", "level": "ok",
                    "key": name, "value": f"connected [{disclosure}] ({tool_note})",
                    "enabled": True,
                    "state": "connected",
                    "disclosure": disclosure,
                    "tool_count": tool_count,
                    "transport": transport,
                    "hint": (
                        f"MCP server '{name}': connected via {transport}, "
                        f"disclosure={disclosure}, {tool_note}."
                    ),
                })
            elif state == "stopped":
                detail = f" — {error_msg}" if error_msg else ""
                results.append({
                    "component": "mcp_servers", "level": "warning",
                    "key": name, "value": f"disconnected{detail}",
                    "enabled": True,
                    "state": "disconnected",
                    "disclosure": "eager" if active else "lazy",
                    "tool_count": 0,
                    "transport": transport,
                    "hint": (
                        f"MCP server '{name}' is enabled but NOT connected.{detail} "
                        f"Use mcp_check_server to diagnose."
                    ),
                })
            else:
                results.append({
                    "component": "mcp_servers", "level": "warning",
                    "key": name, "value": state,
                    "enabled": enabled,
                    "state": state,
                    "disclosure": "unknown",
                    "tool_count": tool_count,
                    "transport": transport,
                    "hint": f"MCP server '{name}' state={state}.",
                })

        return results
    except Exception as e:
        logger.warning("check_mcp_servers_failed err=%s", e)
        return [{"component": "mcp_servers", "level": "error",
                 "key": "check_failed", "value": str(e),
                 "hint": f"Failed to check MCP servers: {e}"}]


# ═══════════════════════════════════════════════════════════════════════
# system_health orchestrator
# ═══════════════════════════════════════════════════════════════════════

_CHECK_FUNCTIONS: list[str] = [
    "check_embedding",
    "check_wechat",
    "check_mcp_servers",
    "check_watchdog",
]


async def _run_checks() -> list[dict]:
    """Call every registered check function via dynamic lookup.

    Supports both sync and async check functions.  Uses ``getattr`` on
    the current module so that test patches (``unittest.mock.patch``)
    work — they replace the module attribute.  Failures in individual
    checks are recorded as error entries so one broken check never
    blocks the rest of the report.
    """
    import inspect as _inspect
    import sys as _sys
    _mod = _sys.modules[__name__]

    all_entries: list[dict] = []
    for func_name in _CHECK_FUNCTIONS:
        try:
            fn = getattr(_mod, func_name)
            if _inspect.iscoroutinefunction(fn):
                entries = await fn()
            else:
                entries = fn()
            all_entries.extend(entries)
        except Exception as e:
            logger.warning("health_check_failed check=%s err=%s", func_name, e)
            all_entries.append({
                "component": "system_health", "level": "error",
                "key": f"{func_name}_failed", "value": str(e),
                "hint": f"Check {func_name}() raised {type(e).__name__}: {e}",
            })
    return all_entries


def _group_by_component(entries: list[dict]) -> dict[str, list[dict]]:
    """Group flat entry list by component for structured display."""
    groups: dict[str, list[dict]] = {}
    for e in entries:
        comp = e.get("component", "unknown")
        groups.setdefault(comp, []).append(e)
    return groups


def _component_status(entries: list[dict]) -> str:
    """Worst status across a group: info/ok < warning < error.

    ``info`` is treated as non-problematic (e.g. disabled servers).
    """
    levels = {e.get("level", "ok") for e in entries}
    if "error" in levels:
        return "error"
    if "warning" in levels:
        return "warning"
    return "ok"


def _build_summary(groups: dict[str, list[dict]]) -> str:
    """One-line summary: '3 ok, 2 warnings (embeddings, memory), 0 errors'."""
    ok_count = sum(1 for es in groups.values() if _component_status(es) == "ok")
    warn_comps = [comp for comp, es in groups.items() if _component_status(es) == "warning"]
    err_comps = [comp for comp, es in groups.items() if _component_status(es) == "error"]
    parts: list[str] = [f"{ok_count} ok"]
    if warn_comps:
        parts.append(f"{len(warn_comps)} warning(s): {', '.join(warn_comps)}")
    if err_comps:
        parts.append(f"{len(err_comps)} error(s): {', '.join(err_comps)}")
    return "; ".join(parts)


def _overall_healthy(groups: dict[str, list[dict]]) -> bool:
    return all(_component_status(es) == "ok" for es in groups.values())


class SystemHealthTool(Tool):
    """Run all subsystem checks + startup records → unified health report."""

    name = "system_health"
    category: ClassVar[str] = "System"
    description = "Unified health report: startup records + OS/shell/workspace/embedding/WeChat/MCP, with healthy flag and summary."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        startup = get_startup_records()
        dynamic = await _run_checks()
        all_entries = startup + dynamic
        groups = _group_by_component(all_entries)

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


