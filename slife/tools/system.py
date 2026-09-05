"""System introspection, health check & agent self-management tools.

Tools:
    check_memdb              — MemDB plugin: database + embedding backend
    check_wechat             — WeChat plugin status
    check_memfiles           — file cabinet (notes / diary / files) status
    check_local_embed        — local embedding service (local-embed) status
    check_sharefile          — file-sharing tunnel (ngrok) status
    check_watchdog           — plugin watchdog (auto-restart) status
    check_mcp                — external MCP server connection status
    check_a2a                — A2A mesh (MQTT) connection + peer status
    system_health            — orchestrate checks + startup records
    list_native_tools        — native tool inventory (grouped, harness markers)
    check_async              — poll background task result
    cancel_async             — cancel a running background task
    clear_context            — reset the loaded turns
    set_max_iterations       — change the loop's iteration cap at runtime (0 = unlimited)
    notify_user              — push a desktop notification to the human operator

(The agent self-management tools were a ``Meta`` category in ``tools/meta.py``;
merged here — one category (System), one module per category.)

OS name, architecture, Python path/version, and package manager are in the
system prompt.  The current shell and working directory are reported by the
per-turn context footer (``_sys_note``) when they change.
Permissions and git status are covered by execute_shell / GitHub MCP.
check_os_info, check_shells, and check_workspace have been removed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from collections import defaultdict
from typing import ClassVar

from slife.health import get_report as get_startup_records
from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute
from slife.paths import get_data_dir
from slife.tools.base import Tool, make_params
from slife.ui.i18n import t

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# check_memdb
# ═══════════════════════════════════════════════════════════════════════

def _semantic_index_hint(sem: dict, pending_noun: str = "items") -> str:
    """Compose a hint for a semantic-index facts block (memdb/memfiles).

    The plugins' ``__check`` reports facts only — this is the harness's
    interpretation layer.  Splits by the facts available (configured /
    available / state / reason / unembedded) without assuming remediation
    text baked into the plugin.
    """
    if sem.get("configured") is False:
        return ("Semantic search unavailable — no embeddings endpoint "
                "configured. Add one with embeddings_model_set "
                "(provider + base_url + api_key). Keyword search works.")
    if sem.get("available") is False:
        return ("Semantic search unavailable — the active embedding endpoint "
                "is down or misconfigured. Fix base_url/api_key with "
                "embeddings_model_set.")
    if sem.get("reason"):
        return sem["reason"]
    if sem.get("state") == "disabled":
        return ("Semantic search disabled — enable with embeddings_enable "
                "true, or edit the top-level embeddings section in slife.json5.")
    state = sem.get("state") or "building"
    return (f"Semantic index {state} — {sem.get('unembedded', 0)} "
            f"{pending_noun} pending embedding; keyword search remains available.")


async def check_memdb(client=None) -> list[dict]:
    """Return MemDB plugin status: database file + embedding status.

    The turns DB + semantic-search facts live inside the memdb plugin
    process, so this check asks the plugin's internal ``__check`` tool
    (raw facts) through its MCP client (from ``ToolContext.memdb_client``)
    and interprets them into health entries.  When the plugin is not
    connected, a warning is reported.
    """
    try:
        if client is None:
            return [{"component": "memdb", "level": "warning", "key": "plugin",
                     "value": "offline",
                     "hint": "memdb plugin not connected — turns DB unavailable."}]
        raw = await client.call_tool("__check")
        data = json.loads(raw)
    except Exception as e:
        logger.warning("memdb_check_failed err=%s", e)
        return [{"component": "memdb", "level": "warning", "key": "plugin",
                 "value": "offline",
                 "hint": f"memdb status unavailable: {e}"}]

    entries: list[dict] = []

    # ── Database file ────────────────────────────────────────────
    db = data.get("db") or {}
    if db.get("exists"):
        entries.append({
            "component": "memdb", "level": "ok", "key": "db",
            "value": f"{db.get('size_mb', 0):.1f} MB",
            "hint": f"Database ready: {db.get('path', '?')}",
        })
    else:
        entries.append({
            "component": "memdb", "level": "warning", "key": "db",
            "value": "not found",
            "hint": (f"Database file not found at {db.get('path', '?')}. "
                     "Will be created on first memory write."),
        })

    # ── Semantic search ──────────────────────────────────────────
    sem = data.get("semantic") or {}
    if sem.get("configured") is False or sem.get("available") is False:
        entries.append({
            "component": "memdb", "level": "warning", "key": "embedding",
            "value": "unavailable",
            "hint": _semantic_index_hint(sem, pending_noun="turns"),
        })
    elif sem.get("semantic_ready"):
        entries.append({
            "component": "memdb", "level": "ok", "key": "embedding",
            "value": "ready",
            "hint": (f"Semantic search ready "
                     f"({sem.get('model', '?')}, dim={sem.get('dimension')})."),
        })
    else:
        entries.append({
            "component": "memdb", "level": "warning", "key": "embedding",
            "value": sem.get("state", "building"),
            "hint": _semantic_index_hint(sem, pending_noun="turns"),
        })
    return entries


class CheckMemdbTool(Tool):
    """Check MemDB plugin status: database file and embedding status."""

    name = "check_memdb"
    category: ClassVar[str] = "System"
    _skip_auto_register: ClassVar[bool] = True
    description = ("MemDB status: SQLite database size + embedding status. "
                   "One subsystem of system_health.")
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        client = getattr(ctx, "memdb_client", None) if ctx is not None else None
        return json.dumps(await check_memdb(client=client), ensure_ascii=False, indent=2)


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


async def check_wechat(client=None, config=None) -> list[dict]:
    """Return WeChat plugin status as health-check entries.

    The enabled/disabled flag comes from slife.json5 (read in-process);
    login/session facts are asked of the wechat plugin's internal ``__check``
    tool through its MCP client (from ``ToolContext.wechat_client``) and
    interpreted into health entries.  When the plugin is not connected, a
    warning is reported.
    """
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
        if client is None:
            return [{"component": "wechat", "level": "warning", "key": "plugin",
                     "value": "offline",
                     "hint": "WeChat plugin not connected — login/session status unavailable."}]
        raw = await client.call_tool("__check")
        data = json.loads(raw)
    except Exception as e:
        logger.warning("wechat_check_failed err=%s", e)
        return [{"component": "wechat", "level": "warning", "key": "plugin",
                 "value": "unavailable",
                 "hint": f"WeChat status unavailable: {e}"}]

    session = data.get("session") or {}
    age_h = session.get("age_h", 0.0)
    max_h = session.get("max_age_h", 0.0)
    remaining_h = max(0.0, round(max_h - age_h, 1))

    last_error = data.get("last_error") or ""
    if data.get("logged_in"):
        if data.get("auth_failed"):
            return [{"component": "wechat", "level": "error", "key": "status",
                     "value": "session_rejected",
                     "hint": ("WeChat session was rejected by the server — "
                              "call wechat_login to re-scan. "
                              f"Last error: {last_error}")}]
        if last_error:
            return [{"component": "wechat", "level": "warning", "key": "status",
                     "value": "degraded",
                     "hint": (f"WeChat link is down — messages will not arrive until "
                              f"it recovers. Last error: {last_error}")}]
        return [{"component": "wechat", "level": "ok", "key": "status",
                 "value": "logged_in",
                 "hint": (f"WeChat logged in. Session age: {age_h:.1f}h, "
                          f"remaining: {remaining_h:.1f}h.")}]

    if session.get("saved"):
        if remaining_h <= 0:
            return [{"component": "wechat", "level": "warning", "key": "status",
                     "value": "session_expired",
                     "hint": (f"WeChat session expired ({age_h:.1f}h old, "
                              f"max {max_h:.0f}h). "
                              "Call wechat_login to re-scan.")}]
        return [{"component": "wechat", "level": "ok", "key": "status",
                 "value": "not_logged_in",
                 "hint": (f"WeChat not logged in. Saved session "
                          f"{remaining_h:.1f}h left — restores on the "
                          "next wechat_check_status.")}]
    return [{"component": "wechat", "level": "warning", "key": "status",
             "value": "not_logged_in",
             "hint": "WeChat not logged in. Call wechat_login to scan the QR code."}]


class CheckWechatTool(Tool):
    """Check WeChat plugin status: enabled, logged in, session expiry."""

    name = "check_wechat"
    category: ClassVar[str] = "System"
    _skip_auto_register: ClassVar[bool] = True
    description = ("WeChat plugin status: disabled, not_logged_in, logged_in, or "
                   "session_expired. One subsystem of system_health.")
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        client = getattr(ctx, "wechat_client", None) if ctx is not None else None
        return json.dumps(await check_wechat(client=client), ensure_ascii=False, indent=2)


# check_sharefile
# ═══════════════════════════════════════════════════════════════════════

async def check_sharefile(client=None) -> list[dict]:
    """Return file-sharing tunnel status (queried from the sharefile plugin).

    The tunnel lives inside the sharefile plugin process, so this check asks
    the plugin's internal tool ``__check`` through its MCP client
    (from ``ToolContext.sharefile_client``).  When the plugin is not
    connected, a warning is reported.
    """
    try:
        if client is None:
            return [{"component": "sharefile", "level": "warning", "key": "tunnel",
                     "value": "plugin_offline",
                     "hint": "sharefile plugin not connected — file sharing unavailable."}]
        raw = await client.call_tool("__check")
        data = json.loads(raw)
        if data.get("active"):
            return [{"component": "sharefile", "level": "ok", "key": "tunnel",
                     "value": data.get("url", "?"),
                     "hint": "File sharing tunnel is online."}]
        reason = (data.get("reason") or "").strip()
        detail = f" — {reason}" if reason else ""
        return [{"component": "sharefile", "level": "warning", "key": "tunnel",
                 "value": "offline",
                 "hint": (f"File sharing tunnel unavailable.{detail} "
                          "Check NGROK_AUTHTOKEN credential or ngrok account "
                          "limits (free tier: 1 online agent — one tunnel per token).")}]
    except Exception as e:
        logger.warning("sharefile_check_failed err=%s", e)
        return [{"component": "sharefile", "level": "warning", "key": "tunnel",
                 "value": "offline",
                 "hint": f"File sharing tunnel status unavailable: {e}"}]


class CheckSharefileTool(Tool):
    """Check file-sharing tunnel (ngrok) status."""

    name = "check_sharefile"
    category: ClassVar[str] = "System"
    _skip_auto_register: ClassVar[bool] = True
    description = ("File sharing tunnel status (online/offline) for share_file. "
                   "One subsystem of system_health.")
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        client = getattr(ctx, "sharefile_client", None) if ctx is not None else None
        return json.dumps(await check_sharefile(client=client), ensure_ascii=False, indent=2)


# check_memfiles
# ═══════════════════════════════════════════════════════════════════════

async def check_memfiles(client=None) -> list[dict]:
    """Return file-cabinet (notes / diary / files) status as health entries.

    The cabinet lives inside the memfiles plugin process, so this check asks
    the plugin's internal tool ``__check`` through its MCP client
    (from ``ToolContext.memfiles_client``).  When the plugin is not
    connected, a warning is reported.
    """
    try:
        if client is None:
            return [{"component": "memfiles", "level": "warning", "key": "plugin",
                     "value": "offline",
                     "hint": "memfiles plugin not connected — file cabinet unavailable."}]
        raw = await client.call_tool("__check")
        data = json.loads(raw)
        if data.get("ok"):
            if data.get("semantic_ready"):
                return [{"component": "memfiles", "level": "ok", "key": "plugin",
                         "value": "connected",
                         "hint": "Cabinet connected; semantic index ready."}]
            return [{"component": "memfiles", "level": "ok", "key": "plugin",
                     "value": "connected",
                     "hint": (f"Cabinet connected — semantic index "
                              f"{data.get('state')}, {data.get('unembedded', 0)} "
                              "pending embedding; keyword search available.")}]
        return [{"component": "memfiles", "level": "warning", "key": "plugin",
                 "value": data.get("state", "degraded"),
                 "hint": data.get("reason") or "Cabinet store unavailable."}]
    except Exception as e:
        logger.warning("memfiles_check_failed err=%s", e)
        return [{"component": "memfiles", "level": "warning", "key": "plugin",
                 "value": "offline",
                 "hint": f"Cabinet status unavailable: {e}"}]


class CheckMemfilesTool(Tool):
    """Check file-cabinet (notes / diary / files) status."""

    name = "check_memfiles"
    category: ClassVar[str] = "System"
    _skip_auto_register: ClassVar[bool] = True
    description = ("File cabinet (memfiles) status: connected, store, semantic index "
                   "(search). One subsystem of system_health.")
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        client = getattr(ctx, "memfiles_client", None) if ctx is not None else None
        return json.dumps(await check_memfiles(client=client), ensure_ascii=False, indent=2)


# check_local_embed
# ═══════════════════════════════════════════════════════════════════════

async def check_local_embed(client=None) -> list[dict]:
    """Return local-embed (local embedding service) status as health entries.

    The service runs in its own plugin process (``local-embed``, an external
    plugin serving OpenAI-compatible ``/v1/embeddings`` + MCP tools), so this
    check asks its internal ``__check`` tool through its MCP client
    (from ``ToolContext.local_embed_client``).  When the plugin is not
    connected, a warning is reported.
    """
    try:
        if client is None:
            return [{"component": "local_embed", "level": "warning", "key": "plugin",
                     "value": "offline",
                     "hint": "local-embed plugin not connected — local embedding unavailable."}]
        raw = await client.call_tool("__check")
        data = json.loads(raw)
        active = data.get("active_model") or "?"
        models = data.get("models") or []
        active_entry = next(
            (m for m in models if m.get("name") == active),
            None,
        )
        active_loaded = active_entry.get("loaded") if active_entry else None
        active_available = active_entry.get("available") if active_entry else None
        loaded_count = sum(1 for m in models if m.get("loaded"))
        if active_loaded:
            return [{"component": "local_embed", "level": "ok", "key": "status",
                     "value": active,
                     "hint": f"local-embed online: active model {active} loaded, "
                             f"{loaded_count}/{len(models)} model(s) loaded."}]
        if active_available is False:
            # The endpoint is up but the active model's backend is unusable —
            # dependency not installed (sentence-transformers / llama-cpp-python)
            # or the model file is missing/broken.  Not a load-block; it will
            # keep failing.  Semantic search degrades to keyword-only.
            return [{"component": "local_embed", "level": "warning", "key": "status",
                     "value": active,
                     "hint": (f"local-embed online but active model {active} unavailable — "
                              "embedding backend dependency missing or model file invalid. "
                              "Semantic search off (keyword search still works).")}]
        return [{"component": "local_embed", "level": "warning", "key": "status",
                 "value": active,
                 "hint": (f"local-embed online but active model {active} NOT loaded yet — "
                          "first embed will load it (or it fails then).")}]
    except Exception as e:
        logger.warning("local_embed_check_failed err=%s", e)
        return [{"component": "local_embed", "level": "warning", "key": "status",
                 "value": "unavailable",
                 "hint": f"local-embed status unavailable: {e}"}]


class CheckLocalEmbedTool(Tool):
    """Check the local-embed (local embedding service) status."""

    name = "check_local_embed"
    category: ClassVar[str] = "System"
    _skip_auto_register: ClassVar[bool] = True
    description = ("Local embedding service (local-embed) status: online/offline, "
                   "active model, loaded models. One subsystem of system_health.")
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        client = getattr(ctx, "local_embed_client", None) if ctx is not None else None
        return json.dumps(await check_local_embed(client=client), ensure_ascii=False, indent=2)


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
    _skip_auto_register: ClassVar[bool] = True
    description = ("Plugin watchdog status: which plugins are auto-restarted, latest "
                   "restart records. One subsystem of system_health.")
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return json.dumps(check_watchdog(), ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# check_mcp
# ═══════════════════════════════════════════════════════════════════════

def _diagnose_mcp_server(server: dict) -> dict:
    """Diagnose a single MCP server from raw ``__check`` data.

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
                f"The wrapper auto-reconnects in the background; use check_mcp "
                f"to see current status and error details."
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


def _diagnose_mcp_semantic(sem: dict) -> dict:
    """Health entry for the wrapper's semantic-index status (memdb-style).

    ``sem`` is the ``semantic`` block of the wrapper ``__check`` payload:
    configured/available (embedder), semantic_ready (gate), state, reason,
    model/dimension/unembedded.  Reports ok only when the gate is ready;
    anything warm-up-shaped (not started / building / stalled / unavailable)
    is a warning so the harness can tell readiness from degradation without
    an LLM round-trip.  Mirrors the memdb embedding health entry.
    """
    model = sem.get("model") or "?"
    dim = sem.get("dimension") or 0
    state = sem.get("state", "not_started")
    reason = sem.get("reason") or ""
    comp = {"component": "mcp_semantic", "key": "semantic"}
    if sem.get("configured") is False or sem.get("available") is False:
        return {
            **comp, "level": "warning", "value": "unavailable",
            "hint": reason or (
                "Semantic tool search unavailable — no embeddings endpoint. "
                "Keyword/grep (fts5) tool search still works."
            ),
        }
    if sem.get("semantic_ready"):
        return {
            **comp, "level": "ok", "value": "ready",
            "hint": f"Semantic tool search ready ({model}, dim={dim}).",
        }
    return {
        **comp, "level": "warning", "value": state,
        "hint": reason or "Semantic tool index building.",
    }


async def check_mcp(server: str = "", client=None) -> list[dict]:
    """Check MCP wrapper health + diagnose external MCP server(s).

    Calls the wrapper's harness ``__check`` for the raw live
    server state, then applies :func:`_diagnose_mcp_server` to each entry to
    produce health-check records with an appropriate level and remediation hint.

    The status report is authoritative: an enabled server whose state is
    ``running`` reports ok.  Note: external tools are on-demand by default
    (loaded via ``mcp_tool_load``) — ``running`` means the server is reachable
    and its tools are discoverable via ``mcp_tool_search``, not that they are
    all registered; loaded proxies are validated on every ``tools/list_changed``
    (see :meth:`slife.agent.service.AgentService._sync_mcp_proxies`).

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

        raw = await client.call_tool("__check")
        data = json.loads(raw)

        # Current wrapper shape: {"servers": [...], "semantic": {...}}.
        # An older/standalone wrapper returns a bare server list — handle both.
        semantic = None
        if isinstance(data, dict):
            semantic = data.get("semantic")
            data = data.get("servers") or []

        if not isinstance(data, list) or len(data) == 0:
            if server:
                return _not_found(server)
            records = [{"component": "mcp_servers", "level": "ok",
                        "key": "status", "value": "none",
                        "hint": "No external MCP servers configured."}]
            if semantic is not None:
                records.append(_diagnose_mcp_semantic(semantic))
            return records

        if server:
            matched = [s for s in data if s.get("name") == server]
            if not matched:
                return _not_found(server)
            data = matched

        records = [_diagnose_mcp_server(s) for s in data]
        if semantic is not None:
            records.append(_diagnose_mcp_semantic(semantic))
        return records

    except Exception as e:
        logger.warning("check_mcp_failed err=%s", e)
        return [{"component": "mcp_servers", "level": "error",
                 "key": "check_failed", "value": str(e),
                 "hint": f"Failed to check MCP servers: {e}"}]


# ═══════════════════════════════════════════════════════════════════════
# check_a2a
# ═══════════════════════════════════════════════════════════════════════

async def check_a2a(client=None) -> list[dict]:
    """Return A2A mesh status (queried from the a2a plugin).

    The A2A mesh transport (MQTT binding) lives inside the a2a plugin
    process, so this check asks the plugin's internal tool ``__check``
    through its MCP client (from ``ToolContext.a2a_mcp_client``).  When the
    mesh is unreachable — mosquitto not running (no active MQTT port), or
    the connection dropped — a warning is reported.
    """
    try:
        if client is None:
            return [{"component": "a2a", "level": "warning", "key": "status",
                     "value": "unavailable",
                     "hint": "No active MQTT port — A2A unavailable. Start mosquitto, then restart slife to enable the A2A mesh."}]

        raw = await client.call_tool("__check")
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
    _skip_auto_register: ClassVar[bool] = True
    description = ("A2A mesh status: connected / unavailable (no active MQTT port), "
                   "agent id, status, online peers. One subsystem of system_health.")
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        client = getattr(ctx, "a2a_mcp_client", None) if ctx is not None else None
        return json.dumps(await check_a2a(client=client), ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# system_health orchestrator
# ═══════════════════════════════════════════════════════════════════════

# check_media
# ═══════════════════════════════════════════════════════════════════════

async def check_media(client=None) -> list[dict]:
    """Return media plugin status: config + generation capabilities.

    Media is optional — when no ``media:`` section is configured the entry
    is informational (not a warning).  When configured, this check asks the
    plugin's internal ``__check`` tool through its MCP client (from
    ``ToolContext.media_client``).
    """
    try:
        from slife.plugins.media.config import load_media_config
        cfg = load_media_config()
        if cfg.is_empty():
            return [{"component": "media", "level": "ok", "key": "enabled",
                     "value": "not_configured",
                     "hint": "Media generation not configured (no media: section in slife.json5)."}]
    except Exception as e:
        logger.warning("media_check_config_failed err=%s", e)
        return [{"component": "media", "level": "warning", "key": "config",
                 "value": "error",
                 "hint": f"Media config status unavailable: {e}"}]
    try:
        if client is None:
            return [{"component": "media", "level": "warning", "key": "plugin",
                     "value": "offline",
                     "hint": "media plugin configured but not connected."}]
        raw = await client.call_tool("__check")
        data = json.loads(raw)
    except Exception as e:
        logger.warning("media_check_failed err=%s", e)
        return [{"component": "media", "level": "warning", "key": "plugin",
                 "value": "unavailable",
                 "hint": f"media status unavailable: {e}"}]

    # The plugin reports facts; shape the health entries here.
    if data.get("error"):
        return [{"component": "media", "level": "warning", "key": "config",
                 "value": "error",
                 "hint": f"Media config status unavailable: {data.get('error')}"}]
    if not data.get("configured"):
        return [{"component": "media", "level": "ok", "key": "enabled",
                 "value": "not_configured",
                 "hint": ("Media generation not configured. Add a media: "
                          "section to slife.json5 to enable generate_image / "
                          "generate_video / text_to_speech / transcribe_audio.")}]
    providers = data.get("providers") or []
    all_kinds = sorted({k for p in providers for k in (p.get("kinds") or [])})
    results: list[dict] = [{
        "component": "media", "level": "ok", "key": "enabled",
        "value": f"{len(providers)} provider(s)",
        "hint": (f"Media configured: {len(providers)} provider(s), "
                 f"capabilities {', '.join(all_kinds) or '(none)'}."),
    }]
    for p in providers:
        pid = p.get("id", "?")
        caps = ", ".join(p.get("kinds") or []) or "(no models)"
        if p.get("has_api_key"):
            results.append({
                "component": "media", "level": "ok", "key": pid,
                "value": caps,
                "hint": f"Provider '{pid}' ({p.get('api')}) configured with api_key.",
            })
        else:
            results.append({
                "component": "media", "level": "warning", "key": pid,
                "value": caps,
                "hint": f"Provider '{pid}' has no api_key set — generation calls will fail.",
            })
    return results


class CheckMediaTool(Tool):
    """Check media plugin status: configured capabilities + api_key."""

    name = "check_media"
    category: ClassVar[str] = "System"
    _skip_auto_register: ClassVar[bool] = True
    description = ("Media (image/video/TTS/ASR) config status: providers, "
                   "capabilities, api_key. One subsystem of system_health.")
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        client = getattr(ctx, "media_client", None) if ctx is not None else None
        return json.dumps(await check_media(client=client), ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# check_job_coding
# ═══════════════════════════════════════════════════════════════════════

async def check_job_coding(client=None) -> list[dict]:
    """Return job-coding plugin status: registered jobs + job LLM model.

    The plugin is a built-in that auto-registers job tools from the jobs
    directory; this asks its internal ``__check`` (raw facts) through its
    MCP client (from ``ToolContext.job_coding_client``) and interprets
    them into health entries.
    """
    try:
        if client is None:
            return [{"component": "job-coding", "level": "warning", "key": "plugin",
                     "value": "offline",
                     "hint": "job-coding plugin not connected — job tools unavailable."}]
        raw = await client.call_tool("__check")
        data = json.loads(raw)
    except Exception as e:
        logger.warning("job_coding_check_failed err=%s", e)
        return [{"component": "job-coding", "level": "warning", "key": "plugin",
                 "value": "unavailable",
                 "hint": f"job-coding status unavailable: {e}"}]

    if data.get("error"):
        return [{"component": "job-coding", "level": "warning", "key": "config",
                 "value": "error",
                 "hint": f"job-coding config status unavailable: {data.get('error')}"}]

    jobs = data.get("job_names") or []
    model = data.get("llm_model") or ""
    hints = [f"Jobs dir: {data.get('jobs_dir') or '(none)'}."]
    if jobs:
        hints.insert(0, f"Jobs registered: {', '.join(jobs)}.")
        level, value, hint = "ok", f"{len(jobs)} job(s)", " ".join(hints)
    else:
        level, value, hint = (
            "ok", "no jobs",
            "No jobs registered. Add a .py file to the jobs directory or use "
            "job-write; load the job-coding skill to author one.",
        )
    entries: list[dict] = [{
        "component": "job-coding", "level": level, "key": "jobs",
        "value": value, "hint": hint,
    }]
    if model in ("", "?", "unconfigured"):
        entries.append({
            "component": "job-coding", "level": "warning", "key": "llm_model",
            "value": "unconfigured",
            "hint": ("No job LLM resolved (set job_coding_model in slife.json5) — "
                     "jobs that call llm.chat will fail; pure-computation jobs work."),
        })
    else:
        entries.append({
            "component": "job-coding", "level": "ok", "key": "llm_model",
            "value": model, "hint": f"Job LLM model: {model}.",
        })
    return entries


class CheckJobCodingTool(Tool):
    """Check job-coding plugin status: registered jobs + job LLM model."""

    name = "check_job_coding"
    category: ClassVar[str] = "System"
    _skip_auto_register: ClassVar[bool] = True
    description = ("job-coding status: registered jobs, job LLM model. "
                   "One subsystem of system_health.")
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        client = getattr(ctx, "job_coding_client", None) if ctx is not None else None
        return json.dumps(await check_job_coding(client=client), ensure_ascii=False, indent=2)


_CHECK_FUNCTIONS: list[str] = [
    "check_memdb",
    "check_wechat",
    "check_memfiles",
    "check_local_embed",
    "check_sharefile",
    "check_media",
    "check_job_coding",
    "check_mcp",
    "check_a2a",
    "check_watchdog",
]

#: check_* functions that reach live plugin state via a ToolContext client.
#: ``check_mcp`` uses the slife-mcp wrapper client; ``check_memfiles``,
#: ``check_local_embed``, ``check_sharefile`` and ``check_a2a`` use their
#: respective plugin clients.
_CLIENT_FIELD: dict[str, str] = {
    "check_memdb": "memdb_client",
    "check_wechat": "wechat_client",
    "check_mcp": "mcp_client",
    "check_memfiles": "memfiles_client",
    "check_local_embed": "local_embed_client",
    "check_sharefile": "sharefile_client",
    "check_media": "media_client",
    "check_job_coding": "job_coding_client",
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


#: Startup-record components that a live ``check_*`` inside ``system_health``
#: re-reports, mapped to that live check's component.  A live entry is the
#: deduplicated, latest-per-key view of the same health store, so the startup
#: records it covers are dropped when the two are merged.
_LIVE_REPORTS: dict[str, str] = {
    "mcp_server": "mcp_servers",  # startup auto-connect record vs check_mcp
    "watchdog": "watchdog",        # startup watchdog record vs check_watchdog
}


def _dedupe_records(startup: list[dict], live: list[dict]) -> list[dict]:
    """Merge startup records with live check entries without double-reporting.

    Some startup records are re-reported by a live ``check_*`` inside
    ``system_health``: ``mcp_server`` records (recorded by the main process
    during auto-connect / reconnect) are re-reported by ``check_mcp`` as
    ``mcp_servers``; ``watchdog`` records are re-reported (deduplicated to
    one entry per plugin) by ``check_watchdog``.  Merging both without dedup
    would double-report each plugin — and for watchdogs, surface every
    historical record instead of the latest — or keep a stale startup warning
    next to a live "connected" report (contradictory health).

    Rules (keyed by the name in each entry's ``key``):
      - an entry is the *live* one iff its component is a live component in
        ``_LIVE_REPORTS`` (regardless of level — a live "disconnected"
        report is authoritative too, so a stale "connected" startup record
        is never resurrected);
      - every startup record whose component maps to a live component and
        whose name is covered by a live entry is dropped;
      - startup records not covered by a live entry (e.g. the wrapper was
        unreachable, or only a single server was checked) are kept so
        recovery info is never lost.
    """
    live_keys: dict[str, set[str]] = {}
    for e in live:
        comp = e.get("component")
        key = e.get("key")
        if isinstance(comp, str) and comp in _LIVE_REPORTS.values() and isinstance(key, str) and key:
            live_keys.setdefault(comp, set()).add(key)

    def _reported_live(e: dict) -> bool:
        """True if startup record *e* is re-reported by a live entry."""
        comp = e.get("component")
        key = e.get("key")
        if not (isinstance(comp, str) and isinstance(key, str)):
            return False
        live_comp = _LIVE_REPORTS.get(comp)
        return live_comp is not None and key in live_keys.get(live_comp, set())

    kept = [e for e in startup if not _reported_live(e)]
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
    description = ("Complete health report in one call: every subsystem check "
                   "(memdb, wechat, memfiles, embeddings, sharefile, mcp, a2a, "
                   "job-coding, watchdog) plus startup records, grouped per "
                   "component with an overall healthy flag and summary. One "
                   "call gives the whole picture — no separate health tools "
                   "needed.")
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        startup = get_startup_records()
        dynamic = await _run_checks(ctx=getattr(self, "_ctx", None))
        all_entries = _dedupe_records(startup, dynamic)
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


# ═══════════════════════════════════════════════════════════════════════
# list_native_tools
# ═══════════════════════════════════════════════════════════════════════


def _native_category(tool) -> str:
    """Display category for a tool in ``list_native_tools``.

    Source-based, no name-prefix guessing:
      - native tools carry their own ``category`` class attribute;
      - built-in plugin tools (MCP proxy tools) group by their plugin
        name — the grouping heading carries the identity, so the
        per-tool ``[<server>] `` description prefix is stripped below.
    External MCP proxy tools are filtered out before this is called.
    """
    if isinstance(tool, MCPProxyTool):
        return getattr(tool, "_server", "") or "Plugins"
    return getattr(tool, "category", "") or "Other"


def _strip_server_prefix(tool, desc: str) -> str:
    """Remove the ``[<server>] `` prefix MCPProxyTool stamps on its
    description.  In ``list_native_tools`` the plugin name is already the
    group heading — the per-line prefix is redundant noise (tools are bare
    names, no ``server__`` prefix)."""
    if isinstance(tool, MCPProxyTool):
        server = getattr(tool, "_server", "")
        prefix = f"[{server}] "
        if server and desc.startswith(prefix):
            return desc[len(prefix):]
    return desc


class ListNativeToolsTool(Tool):
    name: ClassVar[str] = "list_native_tools"
    category: ClassVar[str] = "System"
    description: ClassVar[str] = (
        "Inventory of native tools (grouped, first-sentence summaries, "
        "harness/auto-invoked markers). Includes built-in plugin tools "
        "(Turns DB, File Cabinet, sharing, media, WeChat, A2A) — they are "
        "first-class like native tools. External MCP server tools are NOT "
        "listed — the model already receives their full schemas natively."
    )
    parameters: ClassVar[dict] = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        registry = ctx.registry if ctx is not None else None
        if registry is None:
            return "Tool registry is not available (called before initialization)."

        all_tools = registry.list_tools()
        if not all_tools:
            return "No tools are currently registered."

        # External MCP server tools are excluded: the model already receives
        # their full schemas in the native `tools` array of every request, so
        # a second listing here is pure redundant context.  Built-in plugin
        # tools (DIRECT/WRAPPER — bare names) ARE native: included here.
        natives = [
            t for t in all_tools
            if not (isinstance(t, MCPProxyTool) and t._route == ProxyRoute.EXTERNAL)
        ]
        if not natives:
            return "No native tools are currently registered."

        lines = [f"## Native Tools ({len(natives)} total)\n"]
        native_groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for t in sorted(natives, key=lambda t: t.name):
            cat = _native_category(t)
            desc = t.description.split(".")[0].strip() + "."
            desc = _strip_server_prefix(t, desc)
            native_groups[cat].append((t.name, desc))

        for cat in sorted(native_groups):
            items = native_groups[cat]
            lines.append(f"### {cat} ({len(items)})")
            for name, desc in items:
                marker = " — harness, auto-invoked" if name.startswith("_") else ""
                lines.append(f"- **`{name}`**{marker} — {desc}")
            lines.append("")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# Async tasks
# ═══════════════════════════════════════════════════════════════════════

_tasks: dict[str, asyncio.Task] = {}

#: Bound on ``_tasks`` — an ``_async: true`` call the LLM never polls would
#: otherwise keep its finished task in the dict (holding tool resources) for
#: the whole session.
_MAX_ASYNC_TASKS = 100


def _prune_old_done() -> None:
    """Drop the oldest completed tasks while over the cap; running tasks stay."""
    if len(_tasks) <= _MAX_ASYNC_TASKS:
        return
    for tid in list(_tasks.keys()):  # insertion order = oldest first
        if len(_tasks) <= _MAX_ASYNC_TASKS:
            break
        task = _tasks[tid]
        if task.done():
            _tasks.pop(tid, None)


def schedule(coro) -> str:
    task_id = uuid.uuid4().hex[:8]
    task = asyncio.create_task(_runner(coro, task_id))
    _tasks[task_id] = task
    _prune_old_done()
    logger.info("async_task_scheduled id=%s", task_id)
    return task_id


async def _runner(coro, task_id: str) -> str:
    try:
        result = await coro
    except Exception as e:
        result = f"Error: {type(e).__name__}: {e}"
    logger.info("async_task_done id=%s len=%d", task_id, len(result))
    return result


def _get_task(task_id: str) -> asyncio.Task | None:
    return _tasks.get(task_id)


def _pop_task(task_id: str) -> asyncio.Task | None:
    return _tasks.pop(task_id, None)


class CheckAsyncTool(Tool):
    name: ClassVar[str] = "check_async"
    category: ClassVar[str] = "System"
    description: ClassVar[str] = "Query an async task result. Returns status while running, the result when done."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task_id returned by the async task."},
        },
        "required": ["task_id"],
    }

    async def execute(self, **kwargs) -> str:
        task_id: str = kwargs["task_id"]
        task = _get_task(task_id)
        if task is None:
            return f"Error: Task '{task_id}' not found. It may have already completed and been cleaned up, or the task_id is incorrect."
        if not task.done():
            return f"⏳ Task is still running…\n  task_id: {task_id}\n  Try check_async again later."
        _pop_task(task_id)
        try:
            result = task.result()
        except Exception as e:
            result = f"Error: Async task failed: {type(e).__name__}: {e}"
        return f"✓ Task completed (task_id: {task_id})\n\n{result}"


class CancelAsyncTool(Tool):
    name: ClassVar[str] = "cancel_async"
    category: ClassVar[str] = "System"
    description: ClassVar[str] = "Cancel a running async task. Completed tasks cannot be cancelled."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task_id to cancel."},
        },
        "required": ["task_id"],
    }

    async def execute(self, **kwargs) -> str:
        task_id: str = kwargs["task_id"]
        task = _get_task(task_id)
        if task is None:
            return f"Error: Task '{task_id}' not found. It may have already completed and been cleaned up, or the task_id is incorrect."
        if task.done():
            _pop_task(task_id)
            return f"Task '{task_id}' already completed — nothing to cancel."
        task.cancel()
        _pop_task(task_id)
        logger.info("async_task_cancelled id=%s", task_id)
        return f"✓ Task '{task_id}' cancelled."


# ═══════════════════════════════════════════════════════════════════════
# clear_context
# ═══════════════════════════════════════════════════════════════════════

class ClearContextTool(Tool):
    name = "clear_context"
    category: ClassVar[str] = "System"
    description = "Clear the loaded turns from context, keeping only the system prompt."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        conv = ctx.message_history if ctx is not None else None
        if conv is None:
            return "MessageHistory is not yet initialised. This tool must be called after the agent service has started."
        removed = conv.clear_history()
        if removed == 0:
            return "Context is already clean — no old turns to remove."
        # A one-shot clear is one big trim: advance the persisted boundary
        # with the same hook the internal trim uses, so the next restore is
        # a genuine fresh start (only turns saved afterwards come back).
        # The count is deliberately generous — the advance lands on the last
        # row regardless — and best-effort: an unreachable memdb only makes
        # the next restore a superset, never a loss.
        advance = getattr(ctx, "advance_context_start", None)
        if advance is not None:
            try:
                await advance(removed)
            except Exception:
                logger.exception("context_start_advance_failed_on_clear")
        # Restart the "Context covers" range — otherwise the next _sys_note
        # would keep reporting the pre-clear start.
        reset_time = getattr(ctx, "reset_context_time", None)
        if reset_time is not None:
            try:
                reset_time()
            except Exception:
                logger.exception("context_time_reset_failed")
        remaining = len(conv.messages)
        logger.info("clear_context removed=%d remaining=%d", removed, remaining)
        return f"[OK] Cleared {removed} old message(s); {remaining} remaining (system prompt + current turn)."


# ═══════════════════════════════════════════════════════════════════════
# set_max_iterations
# ═══════════════════════════════════════════════════════════════════════


class SetMaxIterationsTool(Tool):
    name = "set_max_iterations"
    category: ClassVar[str] = "System"
    description = (
        "Set the maximum tool-call iterations per turn (0 = unlimited); "
        "applies from the next turn."
    )
    parameters = make_params(
        max_iterations={
            "type": "integer",
            "description": "Max tool-call iterations per turn. 0 = unlimited (no cap).",
        },
    )

    async def execute(self, max_iterations: int = 0, **kwargs) -> str:
        setter = getattr(self, "_ctx", None)
        if setter is not None:
            setter = setter.set_max_iterations
        if setter is None:
            return "Error: agent loop is not available yet — call this after the agent service has started."
        return setter(max_iterations)


# ═══════════════════════════════════════════════════════════════════════
# notify_user
# ═══════════════════════════════════════════════════════════════════════


class NotifyUserTool(Tool):
    """Push a desktop notification to the human operator.

    A pure UI tool — it only triggers the display; the LLM never sees
    the notification itself.
    """

    name: ClassVar[str] = "notify_user"
    category: ClassVar[str] = "System"
    description: ClassVar[str] = (
        "Send a desktop notification to the human user."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short notification title (e.g. 'Task Complete', 'Alert').",
            },
            "message": {
                "type": "string",
                "description": "The notification body — be concise (one sentence).",
            },
        },
        "required": ["message"],
    }

    async def execute(self, title: str = "", message: str = "", **kwargs) -> str:
        if not message:
            return "Error: message is required."

        # Default title is the localized app name — the LLM may pass its own.
        if not title:
            title = t("notify_default_title")

        # Log for the session file at WARNING (the console is capped below
        # WARNING, so this is diagnostic-only; the notification below is the
        # user-facing channel).
        logging.getLogger(__name__).warning(
            "USER_NOTIFICATION title=%s message=%s", title, message,
        )

        # Fire desktop notification (best-effort, non-blocking).
        # Daemon thread: a hung notify backend must never block shutdown.
        from slife.platform import desktop_notify
        from slife.threads import run_daemon
        run_daemon(desktop_notify, title, message, name="desktop-notify")

        return t("notify_sent", title=title, message=message)


