"""System introspection & health check tools.

Tools:
    check_memdb              — MemDB plugin: database + embedding backend
    check_wechat             — WeChat plugin status
    check_sharefile          — file-sharing tunnel (ngrok) status
    check_watchdog           — plugin watchdog (auto-restart) status
    check_mcp                — external MCP server connection status
    check_a2a                — A2A mesh (MQTT) connection + peer status
    system_health            — orchestrate checks + startup records

(``list_native_tools`` is a meta tool in ``tools/meta.py``, not here.)

OS name, architecture, Python path/version, and package manager are in the
system prompt.  The current shell and working directory are reported by the
per-turn context footer (``_sys_note``) when they change.
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
# check_memdb
# ═══════════════════════════════════════════════════════════════════════

def check_memdb() -> list[dict]:
    """Return MemDB plugin status: database file + embedding backend."""
    results: list[dict] = []
    from slife.paths import get_db_path

    # ── Database file ─────────────────────────────────────────────
    db_path = get_db_path()
    if db_path.exists():
        size_mb = db_path.stat().st_size / (1024 * 1024)
        results.append({
            "component": "memdb", "level": "ok", "key": "db",
            "value": f"{size_mb:.1f} MB",
            "hint": f"Database ready: {db_path}",
        })
    else:
        results.append({
            "component": "memdb", "level": "warning", "key": "db",
            "value": "not found",
            "hint": f"Database file not found at {db_path}. "
                    "Will be created on first memory write.",
        })

    # ── Embedding backend ─────────────────────────────────────────
    from slife.plugins.memdb.embeddings import EmbeddingClient
    from slife.plugins.memdb.embedding_config import read_embedding_config

    client = EmbeddingClient.from_config(quiet=True)
    cfg = read_embedding_config()

    if cfg is None:
        results.append({
            "component": "memdb", "level": "warning", "key": "embedding",
            "value": "none",
            "hint": ("No embedding backend configured. Semantic search (hybrid mode) will NOT work. "
                     "Keyword search (grep/fts5/time) still works normally. "
                     "Use semantic_index_config to configure: GGUF local model, "
                     "transformer (sentence-transformers), or OpenAI-compatible API."),
        })
        return results

    backend = client.backend
    available = client.available

    if available:
        hints = {
            "gguf": f"GGUF model ready: {cfg.get('model', '?')} (dim={client.dimension}, path={cfg.get('gguf_path', 'unknown')})",
            "transformer": f"Transformer model ready: {cfg.get('model', '?')} (dim={client.dimension})",
        }
        results.append({
            "component": "memdb", "level": "ok", "key": "embedding",
            "value": backend,
            "hint": hints.get(backend, f"API embeddings ready: {cfg.get('model', '?')} (dim={client.dimension})"),
        })
    else:
        warnings = {
            "gguf": (f"GGUF file exists ({cfg.get('gguf_path', 'unknown')}) but "
                     "llama-cpp-python is NOT installed. Semantic search (hybrid mode) will NOT work. "
                     "Install with: uv pip install llama-cpp-python."),
            "transformer": (f"Transformer model configured ({cfg.get('model', '?')}) but "
                            "sentence-transformers is NOT installed. Semantic search (hybrid mode) will NOT work. "
                            "Install with: uv pip install sentence-transformers."),
            "api": ("API key configured but openai package is NOT installed. "
                    "Semantic search (hybrid mode) will NOT work. "
                    "Install with: uv pip install openai."),
        }
        results.append({
            "component": "memdb", "level": "warning", "key": "embedding",
            "value": backend,
            "hint": warnings.get(backend, "Embedding backend is unavailable. "
                                         "Semantic search (hybrid mode) will NOT work."),
        })
    return results


class CheckMemdbTool(Tool):
    """Check MemDB plugin status: database file and embedding backend."""

    name = "check_memdb"
    category: ClassVar[str] = "System"
    description = "MemDB plugin status: SQLite database size + embedding backend (gguf/transformer/api/none)."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return json.dumps(check_memdb(), ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# check_wechat
# ═══════════════════════════════════════════════════════════════════════

def _get_wechat_config():
    """Try to load slife config for wechat status.  Returns None on failure."""
    try:
        from slife.config import Config, parse_cli_agent
        agent_name = parse_cli_agent(sys.argv)
        cfg_path = get_data_dir() / "slife.json5"
        if cfg_path.exists():
            return Config.from_json5(cfg_path, agent_name=agent_name)
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

    session = load_wechat_config(config.agent_name, get_data_dir())

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


# check_sharefile
# ═══════════════════════════════════════════════════════════════════════

async def check_sharefile(client=None) -> list[dict]:
    """Return file-sharing tunnel status (queried from the sharefile plugin).

    The tunnel lives inside the sharefile plugin process, so this check asks
    the plugin's internal tool ``__tunnel_status`` through its MCP client
    (from ``ToolContext.sharefile_client``).  When the plugin is not
    connected, a warning is reported.
    """
    try:
        if client is None:
            return [{"component": "sharefile", "level": "warning", "key": "tunnel",
                     "value": "plugin_offline",
                     "hint": "sharefile plugin not connected — file sharing unavailable."}]
        raw = await client.call_tool("__tunnel_status")
        data = json.loads(raw)
        if data.get("active"):
            return [{"component": "sharefile", "level": "ok", "key": "tunnel",
                     "value": data.get("url", "?"),
                     "hint": "File sharing tunnel is online."}]
        return [{"component": "sharefile", "level": "warning", "key": "tunnel",
                 "value": "offline",
                 "hint": data.get("hint") or "File sharing tunnel unavailable."}]
    except Exception as e:
        logger.warning("sharefile_check_failed err=%s", e)
        return [{"component": "sharefile", "level": "warning", "key": "tunnel",
                 "value": "offline",
                 "hint": f"File sharing tunnel status unavailable: {e}"}]


class CheckSharefileTool(Tool):
    """Check file-sharing tunnel (ngrok) status."""

    name = "check_sharefile"
    category: ClassVar[str] = "System"
    description = "File sharing tunnel status (online/offline) for share_file."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        client = getattr(ctx, "sharefile_client", None) if ctx is not None else None
        return json.dumps(await check_sharefile(client=client), ensure_ascii=False, indent=2)


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


class CheckWatchdogTool(Tool):
    """Check plugin watchdog (auto-restart) status."""

    name = "check_watchdog"
    category: ClassVar[str] = "System"
    description = "Plugin watchdog status: which plugins are auto-restarted, latest restart records."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return json.dumps(check_watchdog(), ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# check_mcp
# ═══════════════════════════════════════════════════════════════════════

def _diagnose_mcp_server(server: dict) -> dict:
    """Diagnose a single MCP server from raw ``__mcp_connection_status`` data.

    Pure data transformation — no side effects, no external calls.
    Maps the raw server state to a health-check entry with an
    appropriate level (info / ok / warning) and human-readable hint.
    """
    name = server.get("name", "?")
    state = server.get("state", "unknown")
    enabled = server.get("enabled", True)
    tool_count = server.get("tool_count", 0)
    transport = server.get("transport", "")
    error_msg = server.get("error", "")

    if not enabled:
        return {
            "component": "mcp_servers", "level": "info",
            "key": name, "value": "disabled",
            "enabled": False, "state": "disabled",
            "tool_count": 0,
            "transport": transport,
            "hint": f"MCP server '{name}' is disabled (not connected).",
        }

    if state == "running":
        tool_note = f"{tool_count} tools loaded"
        return {
            "component": "mcp_servers", "level": "ok",
            "key": name, "value": f"connected ({tool_note})",
            "enabled": True, "state": "connected",
            "tool_count": tool_count,
            "transport": transport,
            "hint": (
                f"MCP server '{name}': connected via {transport}, "
                f"{tool_note}."
            ),
        }

    if state == "stopped":
        detail = f" — {error_msg}" if error_msg else ""
        return {
            "component": "mcp_servers", "level": "warning",
            "key": name, "value": f"disconnected{detail}",
            "enabled": True, "state": "disconnected",
            "tool_count": 0,
            "transport": transport,
            "hint": (
                f"MCP server '{name}' is enabled but NOT connected.{detail} "
                f"Use check_mcp to see current status and error details."
            ),
        }

    # Unknown / other states (e.g. "connecting", "failed")
    return {
        "component": "mcp_servers", "level": "warning",
        "key": name, "value": state,
        "enabled": enabled, "state": state,
        "tool_count": tool_count,
        "transport": transport,
        "hint": f"MCP server '{name}' state={state}.",
    }


async def check_mcp(server: str = "", client=None) -> list[dict]:
    """Check MCP wrapper health + diagnose external MCP server(s).

    Calls the wrapper's harness ``__mcp_connection_status`` for the raw live
    server state, then applies :func:`_diagnose_mcp_server` to each entry to
    produce health-check records with an appropriate level and remediation hint.

    The status report is authoritative: an enabled server whose state is
    ``running`` reports ok (its tools are guaranteed registered — the wrapper's
    reconnect notification plus the agent's periodic poll keep the registry in
    sync, so tools can only be missing transiently).

    Args:
        server: Optional server name to check alone.  Empty (default)
            checks all configured servers.
        client: The slife-mcp wrapper client (from ToolContext).  When it is
            unavailable (wrapper not running), a warning entry is reported.

    Wrapper-level problems (client unavailable) are reported before any
    per-server diagnostics.
    """
    def _not_found(target: str) -> list[dict]:
        return [{
            "component": "mcp_servers", "level": "warning",
            "key": target, "value": "not_found",
            "hint": f"MCP server '{target}' is not configured. "
                    "Use mcp_list to see configured servers.",
        }]

    try:
        if client is None:
            return [{"component": "mcp_servers", "level": "warning",
                     "key": "status", "value": "client_unavailable",
                     "hint": "MCP wrapper client not available — slife-mcp may not be running."}]

        raw = await client.call_tool("__mcp_connection_status")
        data = json.loads(raw)

        if not isinstance(data, list) or len(data) == 0:
            if server:
                return _not_found(server)
            return [{"component": "mcp_servers", "level": "ok",
                     "key": "status", "value": "none",
                     "hint": "No external MCP servers configured."}]

        if server:
            matched = [s for s in data if s.get("name") == server]
            if not matched:
                return _not_found(server)
            data = matched

        return [_diagnose_mcp_server(s) for s in data]

    except Exception as e:
        logger.warning("check_mcp_failed err=%s", e)
        return [{"component": "mcp_servers", "level": "error",
                 "key": "check_failed", "value": str(e),
                 "hint": f"Failed to check MCP servers: {e}"}]


class CheckMcpTool(Tool):
    """Check external MCP server connection status."""

    name = "check_mcp"
    category: ClassVar[str] = "System"
    description = ("External MCP server status: connected/disconnected/disabled per server, "
                   "with tool counts and error details. Pass 'server' to check a single server; "
                   "omit it to check all.")
    parameters = {
        "type": "object",
        "properties": {
            "server": {
                "type": "string",
                "description": "Server name to check alone. Leave empty to check all configured servers.",
                "default": "",
            },
        },
        "required": [],
    }

    async def execute(self, server: str = "", **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        client = getattr(ctx, "mcp_client", None) if ctx is not None else None
        return json.dumps(await check_mcp(server=server, client=client), ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# check_a2a
# ═══════════════════════════════════════════════════════════════════════

async def check_a2a(client=None) -> list[dict]:
    """Return A2A mesh status (queried from the a2a plugin).

    The A2A mesh transport (MQTT binding) lives inside the a2a plugin
    process, so this check asks the plugin's internal tool ``__a2a_status``
    through its MCP client (from ``ToolContext.a2a_mcp_client``).  When the
    mesh is unreachable — mosquitto not running (no active MQTT port), or
    the connection dropped — a warning is reported.
    """
    try:
        if client is None:
            return [{"component": "a2a", "level": "warning", "key": "status",
                     "value": "unavailable",
                     "hint": "No active MQTT port — A2A unavailable. Start mosquitto, then restart slife to enable the A2A mesh."}]

        raw = await client.call_tool("__a2a_status")
        data = json.loads(raw)

        if not data.get("connected"):
            broker = data.get("broker", "")
            where = f" (broker {broker})" if broker else ""
            return [{"component": "a2a", "level": "warning", "key": "status",
                     "value": "unavailable",
                     "hint": f"No active MQTT port — A2A unavailable{where}. Start mosquitto and the plugin will auto-reconnect."}]

        peers = data.get("peers", [])
        peer_names = ", ".join(p.get("agent_name") or "?" for p in peers)
        n = len(peers)
        if n == 0:
            peer_clause = "You have no peers online."
        elif n == 1:
            peer_clause = f"You have 1 peer: {peer_names}."
        else:
            peer_clause = f"You have {n} peers: {peer_names}."
        broker = data.get("broker", "")
        return [{
            "component": "a2a", "level": "ok", "key": "status",
            "value": "connected",
            "peers": peers,
            "hint": f"A2A mesh online (broker {broker}). {peer_clause}",
        }]
    except Exception as e:
        logger.warning("a2a_check_failed err=%s", e)
        return [{"component": "a2a", "level": "warning", "key": "status",
                 "value": "unavailable",
                 "hint": f"A2A mesh status unavailable: {e}"}]


class CheckA2aTool(Tool):
    """Check A2A mesh (MQTT) connection and peer status."""

    name = "check_a2a"
    category: ClassVar[str] = "System"
    description = "A2A mesh status: connected / unavailable (no active MQTT port), agent id, status, online peers."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        client = getattr(ctx, "a2a_mcp_client", None) if ctx is not None else None
        return json.dumps(await check_a2a(client=client), ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# system_health orchestrator
# ═══════════════════════════════════════════════════════════════════════

_CHECK_FUNCTIONS: list[str] = [
    "check_memdb",
    "check_wechat",
    "check_sharefile",
    "check_mcp",
    "check_a2a",
    "check_watchdog",
]

#: check_* functions that reach live plugin state via a ToolContext client.
#: ``check_mcp`` uses the slife-mcp wrapper client; ``check_sharefile`` and
#: ``check_a2a`` use their respective plugin clients.
_CLIENT_FIELD: dict[str, str] = {
    "check_mcp": "mcp_client",
    "check_sharefile": "sharefile_client",
    "check_a2a": "a2a_mcp_client",
}


async def _run_checks(ctx=None) -> list[dict]:
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
            field = _CLIENT_FIELD.get(func_name)
            if field is not None:
                # Plugin-backed checks reach live state via their plugin's
                # MCP client from ToolContext.
                client = getattr(ctx, field, None) if ctx is not None else None
                if _inspect.iscoroutinefunction(fn):
                    entries = await fn(client=client)
                else:
                    entries = fn(client=client)
            elif _inspect.iscoroutinefunction(fn):
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


def _dedupe_mcp_records(startup: list[dict], live: list[dict]) -> list[dict]:
    """Merge startup records with live check entries without double-reporting.

    ``mcp_server`` is the component used for *static* startup records (recorded
    by the main process during auto-connect / reconnect); ``mcp_servers`` is
    the component used by the *live* ``check_mcp`` diagnostics.  ``system_health``
    merges both, so without dedup a server that recovered after a slow cold
    start would still be reported as failed by its stale startup record while
    the live check says it is connected — contradictory health.

    Rules (keyed by the server name in each entry's ``key``):
      - an entry is the *live* one iff its component is ``mcp_servers``
        (regardless of level — a live "disconnected" report is authoritative
        too, so we never resurrect a stale "connected" startup record);
      - every startup record whose name is covered by a live entry is dropped;
      - startup records for servers the live check did not cover (e.g. the
        wrapper was unreachable, or only a single server was checked) are kept
        so recovery info is never lost.
    """
    live_names = {
        e["key"] for e in live
        if e.get("component") == "mcp_servers" and e.get("key")
    }
    kept = [
        e for e in startup
        if not (
            e.get("component") == "mcp_server"
            and e.get("key") in live_names
        )
    ]
    return kept + live


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
    description = "Unified health report: startup records + MemDB/WeChat/tunnel/MCP/watchdog checks, with healthy flag and summary."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        startup = get_startup_records()
        dynamic = await _run_checks(ctx=getattr(self, "_ctx", None))
        all_entries = _dedupe_mcp_records(startup, dynamic)
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


