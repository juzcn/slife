"""System introspection & health check tools.

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
from typing import ClassVar

from slife.paths import get_data_dir
from slife.tools.base import Tool
from slife.health import get_report as get_startup_records

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# check_memdb
# ═══════════════════════════════════════════════════════════════════════

async def check_memdb(client=None) -> list[dict]:
    """Return MemDB plugin status: database file + embedding status.

    The turns DB + semantic-search status live inside the memdb plugin
    process, so this check asks the plugin's internal ``__check`` tool
    through its MCP client (from ``ToolContext.memdb_client``).  When the
    plugin is not connected, a warning is reported.
    """
    try:
        if client is None:
            return [{"component": "memdb", "level": "warning", "key": "plugin",
                     "value": "offline",
                     "hint": "memdb plugin not connected — turns DB unavailable."}]
        raw = await client.call_tool("__check")
        return json.loads(raw)
    except Exception as e:
        logger.warning("memdb_check_failed err=%s", e)
        return [{"component": "memdb", "level": "warning", "key": "plugin",
                 "value": "offline",
                 "hint": f"memdb status unavailable: {e}"}]


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
    login/session status is asked of the wechat plugin's internal ``__check``
    tool through its MCP client (from ``ToolContext.wechat_client``).  When
    the plugin is not connected, a warning is reported.
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
        return json.loads(raw)
    except Exception as e:
        logger.warning("wechat_check_failed err=%s", e)
        return [{"component": "wechat", "level": "warning", "key": "plugin",
                 "value": "unavailable",
                 "hint": f"WeChat status unavailable: {e}"}]


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
                 "hint": data.get("hint") or "Cabinet store unavailable."}]
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
    healthy = server.get("healthy", True)
    tool_count = server.get("tool_count", 0)
    transport = server.get("transport", "")
    error_msg = server.get("error", "")

    if not enabled:
        return {
            "component": "mcp_servers", "level": "info",
            "key": name, "value": "disabled",
            "enabled": False, "state": "disabled",
            "healthy": healthy,
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
            "healthy": healthy,
            "tool_count": tool_count,
            "transport": transport,
            "hint": (
                f"MCP server '{name}': connected via {transport}, "
                f"{tool_note}."
            ),
        }

    if state == "stopped":
        detail = f" — {error_msg}" if error_msg else ""
        if not healthy:
            # build's probe verdict: a known-unusable server (missing apikey,
            # outdated version, …).  The wrapper registers it but never loads
            # its tools; only 'mcp-plugin build' re-probes and flips the flag.
            return {
                "component": "mcp_servers", "level": "warning",
                "key": name, "value": f"unhealthy{detail}",
                "enabled": True, "state": "disconnected",
                "healthy": False,
                "tool_count": 0,
                "transport": transport,
                "hint": (
                    f"MCP server '{name}' is flagged unhealthy (healthy=false) "
                    f"by 'mcp-plugin build' — the last probe could not connect "
                    f"it (missing apikey / outdated version / another error), "
                    f"so it is not loaded.{detail} Fix the cause, then run "
                    f"`mcp-plugin build` to re-probe."
                ),
            }
        return {
            "component": "mcp_servers", "level": "warning",
            "key": name, "value": f"disconnected{detail}",
            "enabled": True, "state": "disconnected",
            "healthy": healthy,
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
        "healthy": healthy,
        "tool_count": tool_count,
        "transport": transport,
        "hint": f"MCP server '{name}' state={state}.",
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
    _skip_auto_register: ClassVar[bool] = True
    description = ("External MCP server status: connected/disconnected/disabled per server, "
                   "with tool counts and error details. Pass 'server' to check a single server; "
                   "omit it to check all. One subsystem of system_health.")
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
        return json.loads(raw)
    except Exception as e:
        logger.warning("media_check_failed err=%s", e)
        return [{"component": "media", "level": "warning", "key": "plugin",
                 "value": "unavailable",
                 "hint": f"media status unavailable: {e}"}]


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


_CHECK_FUNCTIONS: list[str] = [
    "check_memdb",
    "check_wechat",
    "check_memfiles",
    "check_local_embed",
    "check_sharefile",
    "check_media",
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
                   "watchdog) plus startup records, grouped per component with an "
                   "overall healthy flag and summary. One call gives the whole "
                   "picture — no separate health tools needed.")
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


