"""System introspection, health check & agent self-management tools.

Registered LLM tools:
    system_health            — one-call health report (every subsystem check
                               plus startup records, grouped per component)
    system_tools_list        — the system's own tool inventory (grouped, harness markers)
    check_async              — poll background task result
    cancel_async             — cancel a running background task
    clear_context            — reset the loaded turns
    set_max_iterations       — change the loop's iteration cap at runtime (0 = unlimited)
    notify_user              — push a desktop notification to the human operator

The per-subsystem ``check_*`` functions (memdb, wechat, memfiles,
embeddings, local-embed, sharefile, watchdog, mcp_gateway, a2a, media,
job-coding,
tool_catalog) are NOT registered as tools — ``system_health`` aggregates
them, plus the startup records, into one report.  They exist as functions
so the harness (and tests) can probe a single subsystem.  ``check_mcp_gateway``
reports the two external-server families as separate components
(``mcp_servers`` / ``rest-api``): an MCP server and a REST API are different
things, sharing only the transport they are currently implemented over, and
each is configured and managed by its own tool set.

Every check returns the same flat entries (``component``/``level``/``key``/
``value``/``hint``), and one rule governs their text: **``value`` is the
fact, ``hint`` is what to do about it.**  ``value`` must be self-contained
(it is what the healthy section of the report prints); ``hint`` is rendered
only for ``warning``/``error`` entries, so a healthy entry carries none.
Any other key on an entry is machine-only — the renderer ignores it.

(The agent self-management tools were a ``Meta`` category in ``tools/meta.py``;
merged here — one category (System), one module per category.)

OS name, architecture, Python path/version, and package manager are in the
system prompt.  The current shell and working directory are reported by the
per-turn prompt (``_turn_prompt``) when they change.
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
from pathlib import Path
from typing import ClassVar

import httpx2

from slife.env import is_env_ref
from slife.health import get_report as get_startup_records
from slife.plugins.spec import PLUGIN_SPECS, health_check_name
from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute
from slife.paths import get_data_dir
from slife.tools.base import Tool, make_params, require_params
from slife.ui.i18n import t
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Shared plugin-probe prologue
# ═══════════════════════════════════════════════════════════════════════

#: Remediation for any unreachable plugin process — the plugin is spawned at
#: startup and supervised by the watchdog, so the operator's move is a restart
#: (the reason, if it keeps failing, is in the plugin's own log).
#:
#: Hints are plain sentences: no ``—`` and no ``;``, because those are the
#: report line's own separators and would make the structure ambiguous.
_PLUGIN_DOWN_HINT = ("Restart slife to respawn the plugin. Its log has the "
                     "reason if it stays down.")


def _entry(component: str, level: str, key: str, value: str,
           hint: str = "") -> dict:
    """Build one health entry, omitting an empty hint.

    ``health.record`` omits falsy fields too; an empty ``hint`` would only be
    dead weight in the report (and the fact/hint rule says a healthy entry
    carries none).
    """
    e = {"component": component, "level": level, "key": key, "value": value}
    if hint:
        e["hint"] = hint
    return e


async def _probe_plugin(client, component: str, *, offline_hint: str = _PLUGIN_DOWN_HINT,
                        unavailable_hint: str = "probe failed",
                        key: str = "plugin",
                        offline_value: str = "offline",
                        unavailable_value: str = "unavailable",
                        log_name: str | None = None) -> tuple[dict | None, list[dict]]:
    """Probe a plugin's ``__check`` tool and parse its JSON payload.

    The shared prologue of every plugin-backed check.  On success returns
    ``(data, [])``; a missing client returns ``(None, [offline entry])``; a
    ``__check`` that raises returns ``(None, [unavailable entry])``.  The
    caller diverges with ``if entries: return entries`` and works from
    *data*.  Entry wording stays per-check via the explicit parameters —
    this helper centralises the *structure*, and the review noted the loose
    copies had already drifted from each other.

    Both failure entries follow the value/hint rule: ``value`` is the state
    (``offline``/``unavailable``) and ``hint`` is the remedy.
    """
    try:
        if client is None:
            return None, [{"component": component, "level": "warning",
                           "key": key, "value": offline_value,
                           "hint": offline_hint}]
        raw = await client.call_tool("__check")
        return json.loads(raw), []
    except Exception as e:
        logger.warning("%s_check_failed err=%s", log_name or component, e)
        return None, [{"component": component, "level": "warning",
                       "key": key, "value": unavailable_value,
                       "hint": f"{unavailable_hint}: {e}"}]


# ═══════════════════════════════════════════════════════════════════════
# check_memdb
# ═══════════════════════════════════════════════════════════════════════

#: Remedy for an embeddings endpoint that is missing or broken — shared by
#: every subsystem that owns a semantic index (memdb, memfiles, the catalog).
_EMBEDDING_FIX_HINT = ("Set a working endpoint with embeddings_model_set "
                       "(provider + base_url + api_key). Keyword search keeps "
                       "working meanwhile.")


def _semantic_facts(sem: dict, pending_noun: str = "items") -> tuple[str, str, str]:
    """Interpret a semantic-index facts block into ``(level, value, hint)``.

    The plugins' ``__check`` reports facts only — this is the harness's
    interpretation layer.  Splits by the facts available (configured /
    available / state / reason / unembedded) without assuming remediation
    text baked into the plugin, and keeps to the value/hint rule: a stalled
    index is a fact (it needs no action, it is catching up), a missing or
    broken endpoint is the case that carries a remedy.
    """
    if sem.get("local_drainer") is False:
        # This process holds the shared db but not the drainer, so it cannot
        # observe "ready" OR "broken" — only how much is pending, which is a
        # real shared fact.  Reporting the endpoint as unconfigured here was
        # a false alarm: the same db, read by the process that owns the
        # drainer, is fine.  A fact, not a problem — so no hint.
        pending = sem.get("unembedded", 0)
        value = "maintained by the main process"
        if pending:
            value += f" ({pending} {pending_noun} pending)"
        return ("info", value, "")
    if sem.get("configured") is False:
        return ("warning", "unavailable (no embeddings endpoint configured)",
                _EMBEDDING_FIX_HINT)
    if sem.get("available") is False:
        return ("warning", "unavailable (endpoint down or misconfigured)",
                _EMBEDDING_FIX_HINT)
    # Before the generic ``reason`` branch: a stall carries a reason too, and
    # reporting it as "unavailable" hid the word "stalled" from the report
    # entirely.  A stall is a fact (it retries on new content, keyword search
    # works meanwhile) so, like the branch below, it carries no hint.
    if sem.get("state") == "stalled":
        return ("warning",
                f"stalled ({sem.get('unembedded', 0)} {pending_noun} pending; "
                f"keyword search available)", "")
    if sem.get("reason"):
        return ("warning", f"unavailable ({sem['reason']})", "")
    if sem.get("state") == "disabled":
        return ("warning", "disabled",
                "Enable with embeddings_enable true, or edit the top-level "
                "embeddings section in slife.yaml.")
    if sem.get("semantic_ready"):
        # A width nobody has measured is NOT 0.  Each semantic index has its
        # own embedder, and only the one that has probed its endpoint knows
        # the dimension — so printing the raw number showed "dim=0" beside
        # another component reporting dim=1024 for the same model.  "?" is
        # this report's marker for "not known" (the model uses it too).
        dim = sem.get("dimension") or 0
        return ("ok",
                f"ready ({sem.get('model') or '?'}, "
                f"{f'dim={dim}' if dim else 'dim=?'})", "")
    state = sem.get("state") or "building"
    return ("warning",
            f"{state} ({sem.get('unembedded', 0)} {pending_noun} pending; "
            f"keyword search available)", "")


async def check_memdb(client=None) -> list[dict]:
    """Return MemDB plugin status: database file + embedding status.

    The turns DB + semantic-search facts live inside the memdb plugin
    process, so this check asks the plugin's internal ``__check`` tool
    (raw facts) through its MCP client (from ``ToolContext.memdb_client``)
    and interprets them into health entries.  When the plugin is not
    connected, a warning is reported.

    The DB value names the file (``1.2 MB (jack.db)``): the DB is
    agent-scoped, so *which* file is live is part of the fact.
    """
    data, entries = await _probe_plugin(
        client, "memdb",
        unavailable_value="offline",
    )
    if entries:
        return entries
    assert data is not None  # entries empty ⇒ probe succeeded
    entries = []

    # ── Database file ────────────────────────────────────────────
    db = data.get("db") or {}
    db_name = Path(str(db.get("path") or "?")).name
    if db.get("exists"):
        entries.append(_entry("memdb", "ok", "db",
                              f"{db.get('size_mb', 0):.1f} MB ({db_name})"))
    else:
        entries.append(_entry(
            "memdb", "warning", "db", f"not found ({db.get('path', '?')})",
            "It is created on the first memory write.",
        ))

    # ── Semantic search ──────────────────────────────────────────
    sem_level, sem_value, sem_hint = _semantic_facts(
        data.get("semantic") or {}, pending_noun="turns",
    )
    entries.append(_entry("memdb", sem_level, "embedding", sem_value, sem_hint))
    return entries


# ═══════════════════════════════════════════════════════════════════════
# check_wechat
# ═══════════════════════════════════════════════════════════════════════

def _get_wechat_config():
    """Try to load slife config for wechat status.  Returns None on failure."""
    try:
        from slife.config import Config, parse_cli_agent
        agent_name = parse_cli_agent(sys.argv)
        cfg_path = get_data_dir() / "slife.yaml"
        if cfg_path.exists():
            return Config.from_yaml(cfg_path, agent_name=agent_name)
    except Exception:
        pass
    return None


async def check_wechat(client=None, config=None) -> list[dict]:
    """Return WeChat plugin status as health-check entries.

    The enabled/disabled flag comes from slife.yaml (read in-process);
    login/session facts are asked of the wechat plugin's internal ``__check``
    tool through its MCP client (from ``ToolContext.wechat_client``) and
    interpreted into health entries.  When the plugin is not connected, a
    warning is reported.
    """
    results: list[dict] = []

    if config is None:
        config = _get_wechat_config()

    if config is None or config.wechat_config is None:
        results.append(_entry(
            "wechat", "ok", "enabled", "unknown (config not loaded)",
        ))
        return results

    wc = config.wechat_config
    if not wc.enabled:
        results.append(_entry("wechat", "ok", "enabled",
                              "disabled (wechat.enabled: false)"))
        return results

    data, entries = await _probe_plugin(client, "wechat")
    if entries:
        return entries
    assert data is not None  # entries empty ⇒ probe succeeded

    session = data.get("session") or {}
    age_h = session.get("age_h", 0.0)
    max_h = session.get("max_age_h", 0.0)
    remaining_h = max(0.0, round(max_h - age_h, 1))

    last_error = data.get("last_error") or ""
    if data.get("logged_in"):
        if data.get("auth_failed"):
            return [{"component": "wechat", "level": "error", "key": "status",
                     "value": f"session_rejected (last error: {last_error})",
                     "hint": "Call wechat_login to re-scan the QR code."}]
        if last_error:
            return [{"component": "wechat", "level": "warning", "key": "status",
                     "value": f"degraded (last error: {last_error})",
                     "hint": ("The link may recover on its own; messages will "
                              "not arrive until it does.")}]
        return [_entry("wechat", "ok", "status",
                       f"logged_in (session {age_h:.1f}h of {max_h:.0f}h, "
                       f"{remaining_h:.1f}h left)")]

    if session.get("saved"):
        if remaining_h <= 0:
            return [{"component": "wechat", "level": "warning", "key": "status",
                     "value": f"session_expired ({age_h:.1f}h old, max {max_h:.0f}h)",
                     "hint": "Call wechat_login to re-scan the QR code."}]
        return [_entry("wechat", "ok", "status",
                       f"not_logged_in (saved session, {remaining_h:.1f}h left; "
                       "restores on the next wechat_check_status)")]
    return [{"component": "wechat", "level": "warning", "key": "status",
             "value": "not_logged_in",
             "hint": "Call wechat_login to scan the QR code."}]


# ═══════════════════════════════════════════════════════════════════════

async def check_sharefile(client=None) -> list[dict]:
    """Return file-sharing tunnel status (queried from the sharefile plugin).

    The tunnel lives inside the sharefile plugin process, so this check asks
    the plugin's internal tool ``__check`` through its MCP client
    (from ``ToolContext.sharefile_client``).  When the plugin is not
    connected, a warning is reported.
    """
    data, entries = await _probe_plugin(
        client, "sharefile",
        key="tunnel", offline_value="plugin_offline", unavailable_value="offline",
        unavailable_hint="Tunnel probe failed",
    )
    if entries:
        return entries
    assert data is not None
    # Which provider is live is a FACT (it is chosen by sharefile.yaml), so it
    # rides in the value; the reason it is down is the plugin's own diagnosis
    # for ITS provider (a missing NGROK_AUTHTOKEN, an absent ssh/cloudflared
    # binary) and the harness must not paste one provider's remediation onto
    # another's failure.
    provider = (data.get("provider") or "").strip()
    reason = (data.get("reason") or "").strip()
    if data.get("active"):
        # A published URL is not a working one.  A transport that lost the
        # edge keeps its URL and answers every request to it with HTTP 530,
        # so "active" alone reports a dead tunnel as healthy — an all-green
        # system_health next to a link that cannot be fetched.  The plugin
        # asks the edge (``reachable``); the harness reports what it says.
        # Absent (an older plugin) reads as reachable: never cry wolf.
        if data.get("reachable", True):
            entries = [_entry("sharefile", "ok", "tunnel", data.get("url", "?"))]
        else:
            value = f"unreachable ({provider})" if provider else "unreachable"
            entries = [{
                "component": "sharefile", "level": "warning", "key": "tunnel",
                "value": value,
                "hint": "The tunnel is published but the public edge cannot "
                        "route to it, so every request to that URL answers "
                        "HTTP 530; share_file refuses until it recovers. It "
                        "keeps retrying in the background."}]
    else:
        value = f"offline ({provider})" if provider else "offline"
        hint = f"{reason} " if reason else ""
        entries = [{
            "component": "sharefile", "level": "warning", "key": "tunnel",
            "value": value,
            "hint": hint + "The active provider is set by sharefile.yaml "
                           "(active_provider)."}]

    if data.get("edge_via_proxy"):
        # The CAUSE of a flap that otherwise reads as Cloudflare's fault.  A
        # proxy in fake-ip mode answers the edge hostname with a synthetic
        # address, so cloudflared's control connection is carried — and cut —
        # by that proxy: it registers, dies, repeats every 30-60s, and every
        # window between answers the published link with HTTP 530.  Nothing
        # about the transport or the protocol helps; the dial itself lands on
        # a fake address.  Named here because it is invisible from inside
        # slife — the tunnel simply looks flaky.
        edge_ip = data.get("edge_ip") or "?"
        entries.append({
            "component": "sharefile", "level": "warning", "key": "edge",
            "value": f"edge via fake-ip ({edge_ip})",
            "hint": "A local proxy in fake-ip mode (Clash / Mihomo / sing-box) "
                    "is resolving the tunnel's edge, so the control connection "
                    "is carried and cut by that proxy — the tunnel registers "
                    f"and dies repeatedly, whatever protocol it uses. Exclude "
                    f"the edge from fake-ip and route it direct: for "
                    f"cloudflared add '+.argotunnel.com' to fake-ip-filter and "
                    f"'DOMAIN-SUFFIX,argotunnel.com,DIRECT' to the proxy rules "
                    f"(other providers: their own edge hostname).",
        })
    return entries


# ═══════════════════════════════════════════════════════════════════════

async def check_memfiles(client=None) -> list[dict]:
    """Return file-cabinet (notes / diary / files) status as health entries.

    The cabinet lives inside the memfiles plugin process, so this check asks
    the plugin's internal tool ``__check`` through its MCP client
    (from ``ToolContext.memfiles_client``).  When the plugin is not
    connected, a warning is reported.

    An index that is still catching up is ``ok`` — semantic search is a
    bonus on top of keyword search, so a building index is a fact, not a
    problem.
    """
    data, entries = await _probe_plugin(
        client, "memfiles",
        unavailable_value="offline",
    )
    if entries:
        return entries
    assert data is not None
    if data.get("ok"):
        if data.get("semantic_ready"):
            value = "connected (semantic index ready)"
        else:
            value = (f"connected (semantic index {data.get('state') or 'building'}, "
                     f"{data.get('unembedded', 0)} pending)")
        return [_entry("memfiles", "ok", "plugin", value)]
    # A broken store has no remedy to offer — the reason IS the fact.
    reason = (data.get("reason") or "").strip()
    state = data.get("state", "degraded")
    return [{"component": "memfiles", "level": "warning", "key": "plugin",
             "value": f"{state}: {reason}" if reason else str(state)}]


# ═══════════════════════════════════════════════════════════════════════
# Endpoint probe deadline is developer-owned (registry ready.probe_endpoint).


async def check_local_embed(client=None) -> list[dict]:
    """Report the local-embed service: up? which models? are they usable?

    local-embed is an ordinary child plugin — spawned, watched and probed like
    every other — so this is the same ``__check`` call the rest of them get.
    Its ``__check`` reports the engine's per-model facts (backend, dimension,
    loaded, available) and this layer interprets them, which is what makes a
    *failed load* legible: the plugin line says whether the service is up, and
    a model whose backend dependency is missing is a warning with the remedy.

    The ENDPOINT fact (what this session embeds with) is
    :func:`check_embeddings`' business — this component is the SERVICE.
    """
    data, entries = await _probe_plugin(
        client, "local-embed",
        offline_hint=("Restart slife to respawn the plugin. If a NON-local-embed "
                      "service holds its port, free that port first — an "
                      "existing local-embed on it is adopted, not a conflict. "
                      "Its log has the reason if it stays down."),
    )
    if entries:
        return entries
    assert data is not None
    models = data.get("models")
    if not isinstance(models, list) or not models:
        return [_entry("local-embed", "warning", "models", "none configured",
                       "Add one with the local-embed CLI, or point the "
                       "embeddings section at a cloud endpoint.")]
    out: list[dict] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        name = m.get("name") or "?"
        dim = m.get("dimension") or 0
        if not m.get("available", True):
            # The backend dependency (llama-cpp / sentence-transformers) is
            # missing, so a request naming this model would 503.
            out.append(_entry(
                "local-embed", "warning", name,
                f"backend unavailable ({m.get('backend') or '?'})",
                "Install the backend for this model (see the local-embed "
                "README), then restart slife. Keyword search keeps working "
                "meanwhile.",
            ))
        elif m.get("loaded"):
            out.append(_entry("local-embed", "ok", name,
                              f"loaded (dim={dim})" if dim else "loaded"))
        else:
            # Not an error: models load on the first request that names them.
            out.append(_entry("local-embed", "info", name, "not loaded"))
    return out


async def check_embeddings(base_url: str = "") -> list[dict]:
    """Probe the ACTIVE embedding endpoint.

    Every configured embeddings provider — the local-embed daemon or a cloud
    API like SiliconFlow — is treated as ONE ordinary OpenAI-compatible
    endpoint and handled the same way: the api_key is resolved like the model
    section's (env → credstore → literal) and sent as the Bearer header, then
    ``GET {base_url}/models`` is probed.  Reachability IS the health signal.

    The reported fact is the endpoint's identity and the model this session
    embeds with: the configured one, or — for a provider that names no model —
    the listing's first entry, which is the one ``EmbeddingClient`` pins.  The
    listing itself carries no active marker, so on a cloud endpoint its first
    entry is a catalogue entry (a chat model, on SiliconFlow) and says nothing
    about us.  The entry is keyed by provider id, so the line names which
    endpoint answered.

    Only the ACTIVE provider is probed: an inactive one is idle by choice.
    The local-embed DAEMON has a component of its own (``check_local_embed``),
    because it is a plugin slife starts and supervises.
    """
    provider = "endpoint"
    base_url = base_url.strip()
    try:
        from slife.plugins.memdb.embedding_config import get_active_endpoint
        ep = get_active_endpoint()
        provider = (ep.get("provider") or "").strip() or provider
        base_url = (base_url or ep.get("base_url") or "").strip()
        if not base_url:
            return [_entry(
                "embeddings", "warning", provider, "offline (no base_url)",
                "Configure the top-level embeddings section "
                "(see embeddings_model_set).",
            )]
        headers: dict[str, str] = {}
        api_key = ep.get("api_key") or ""
        if api_key:
            # Uniform for every endpoint: a cloud API needs the key, local-embed
            # ignores it.  An unresolvable placeholder is never sent as a token.
            from slife.config import _resolve_secret
            api_key = _resolve_secret(api_key, accept_keyring_uri=True)
            if not is_env_ref(api_key):
                headers["Authorization"] = f"Bearer {api_key}"
        base_url = base_url.rstrip("/")
        async with httpx2.AsyncClient(
            timeout=httpx2.Timeout(_timeouts.timeouts.ready.probe_endpoint),
        ) as http:
            resp = await http.get(f"{base_url}/models", headers=headers)
            resp.raise_for_status()
            payload = resp.json()
        models = payload.get("data") or []
        # The configured id is the model this session embeds with.  A provider
        # that names none leaves the choice to the endpoint, and the client
        # resolves it by pinning the listing's first entry — so that, and not
        # some arbitrary catalogue entry, is the fact to report.
        model = (ep.get("model") or "").strip() or next(
            (m.get("id") or "" for m in models if m.get("id")), "",
        )
        return [_entry("embeddings", "ok", provider,
                       f"{model or '?'} at {base_url}")]
    except Exception as e:
        logger.warning("embeddings_check_failed err=%s", e)
        return [{
            "component": "embeddings", "level": "warning", "key": provider,
            "value": "unavailable",
            "hint": f"GET {base_url}/models failed: {e}. Point this provider "
                    f"at a reachable endpoint with embeddings_model_set, or "
                    f"start the service it names. Keyword search keeps "
                    f"working meanwhile.",
        }]


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
        results.append(_entry("watchdog", "ok", "status",
                              "none (subagent, or plugins not started)"))
        return results

    for name in sorted(seen):
        r = seen[name]
        results.append(dict(r))  # copy — don't mutate health records

    return results


# ═══════════════════════════════════════════════════════════════════════
# check_mcp_gateway
# ═══════════════════════════════════════════════════════════════════════

#: Remediation shared by every disconnected server — identical text is what
#: lets the report collapse 20 entries into one line instead of 20 sentences.
#: One per family, because each is managed by its own tool set.
_MCP_RETRY_HINT = ("The wrapper retries in the background. Re-run system_health "
                   "shortly, or inspect one server with mcp_list and turn it "
                   "off with mcp_set_enabled.")
_REST_API_RETRY_HINT = ("The wrapper retries in the background. Re-run "
                        "system_health shortly, or inspect one API with "
                        "rest_api_list and turn it off with "
                        "rest_api_set_enabled.")


def _server_family(server: dict) -> str:
    """``"rest-api"`` for a REST-API entry, else ``""``.

    ``list_servers`` carries the wrapper's own ``rest_api`` verdict, read off
    the entry's config SECTION there.  It is NOT ``source``: that records
    where a definition was downloaded from.

    The families get separate components because they are separate things —
    an MCP server and an API described by an OpenAPI document.  That a REST
    API currently runs through an MCP proxy is an implementation choice, not
    an identity: it is configured, probed and managed by a different tool set
    (``rest_api_*`` vs ``mcp_*``), so an operator reading "which servers are
    down" needs them apart.
    """
    if server.get("rest_api") is True:
        return "rest-api"
    return ""


def _diagnose_mcp_server(server: dict) -> dict:
    """Diagnose a single MCP server from raw ``__check`` data.

    Pure data transformation — no side effects, no external calls.  This is
    where an MCP server's raw facts become a health level: the wrapper reports
    only what it measured (``tools_ok`` / ``tool_count`` / ``last_error`` …)
    and never a state word of its own (DESIGN.md §Health).

    The verdict is the tool list, because that is what everything downstream
    needs: a server that answered ``tools/list`` has its tools in the catalog
    and callable, and one that did not is absent from it.  Only a broken
    server carries a ``hint``; the machine-only keys are deliberately not
    copied onto the entry — a fact belongs in exactly one place, and
    ``mcp_list`` owns the config view.
    """
    name = server.get("name", "?")
    tools_ok = server.get("tools_ok", False)
    enabled = server.get("enabled", True)
    tool_count = server.get("tool_count", 0)
    transport = server.get("transport", "")
    error_msg = server.get("last_error", "")
    needs_user_auth = server.get("needs_user_auth", False)
    family = _server_family(server)
    # The family decides the reported component (and therefore the tool set its
    # remedy names); the rest of the entry is identical.
    component = family or "mcp_servers"
    retry_hint = _REST_API_RETRY_HINT if family else _MCP_RETRY_HINT
    remove_tool, add_tool = (
        ("rest_api_remove", "rest_api_set") if family else ("mcp_remove", "mcp_set")
    )

    if needs_user_auth:
        # OAuth device flow needs a human — nothing repairs this in the
        # background (F5), so "wait for the retry" is not the right advice.
        return _entry(
            component, "warning", name,
            f"needs_user_auth ({error_msg or 'device flow not completed'})",
            f"Re-run the OAuth device flow with {remove_tool} + {add_tool}.",
        )

    if not enabled:
        return _entry(component, "info", name, "disabled")

    if tools_ok:
        return _entry(
            component, "ok", name,
            f"tool list current ({tool_count} tools, {transport})",
        )

    detail = f" — {error_msg}" if error_msg else ""
    return _entry(component, "warning", name,
                  f"no working tool list{detail}", retry_hint)


async def check_mcp_gateway(server: str = "", client=None) -> list[dict]:
    """Check MCP wrapper health + diagnose external MCP server(s).

    Calls the wrapper's harness ``__check`` for the raw live server state,
    then applies :func:`_diagnose_mcp_server` to each entry to produce
    health-check records with an appropriate level and remediation hint.

    The status report is authoritative: an enabled server whose state is
    ``running`` reports ok.  External tools load on demand via the shared
    catalog (``tool_search`` / ``func-tool-load`` — the unified tool system);
    the catalog's own health (db + semantic index) is reported by
    :func:`check_tool_catalog`, not here.

    Args:
        server: Optional server name to check alone.  Empty (default)
            checks all configured servers.
        client: The slife-mcp wrapper client (from ToolContext).  When it is
            unavailable (wrapper not running), a warning entry is reported.

    Wrapper-level problems (client unavailable) are reported before any
    per-server diagnostics.
    """
    def _not_found(target: str) -> list[dict]:
        return [_entry("mcp_servers", "warning", target, "not_found",
                       "Use mcp_list to see the configured servers.")]

    try:
        if client is None:
            # A worker reaches this only if its connect failed — it shares the
            # parent's gateway and re-points ``mcp_client`` like every other
            # plugin.  So this stays a WARNING in both processes: tolerating it
            # for workers masked a real wiring gap behind a plausible story.
            return [_entry(
                "mcp_servers", "warning", "status", "unavailable (client not connected)",
                "Restart slife to respawn the plugin. Its log has the reason "
                "if it stays down.",
            )]

        raw = await client.call_tool("__check")
        data = json.loads(raw)
        data = data.get("servers") if isinstance(data, dict) else None

        if not isinstance(data, list) or len(data) == 0:
            if server:
                return _not_found(server)
            return [_entry("mcp_servers", "ok", "status", "none configured")]

        if server:
            matched = [s for s in data if s.get("name") == server]
            if not matched:
                return _not_found(server)
            data = matched

        records = [_diagnose_mcp_server(s) for s in data]
        return records

    except Exception as e:
        logger.warning("check_mcp_gateway_failed err=%s", e)
        return [{"component": "mcp_servers", "level": "error",
                 "key": "check_failed", "value": "probe failed",
                 "hint": f"{e} — server states are unknown until it recovers."}]


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
    data, entries = await _probe_plugin(
        client, "a2a",
        key="status", offline_value="unavailable",
        offline_hint="Start mosquitto, then restart slife to bring the mesh up.",
        unavailable_hint="Mesh probe failed",
    )
    if entries:
        return entries
    assert data is not None

    broker = data.get("broker", "")
    where = f" (broker {broker})" if broker else ""
    if not data.get("connected"):
        return [{"component": "a2a", "level": "warning", "key": "status",
                 "value": f"unavailable{where}",
                 "hint": "No active MQTT port. Start mosquitto and the plugin "
                         "reconnects on its own."}]

    peers = data.get("peers", [])
    peer_names = ", ".join(p.get("agent_name") or "?" for p in peers)
    n = len(peers)
    if n == 0:
        peer_clause = "no peers"
    elif n == 1:
        peer_clause = f"1 peer: {peer_names}"
    else:
        peer_clause = f"{n} peers: {peer_names}"
    queued = sum(
        v for v in (data.get("queued") or {}).values()
        if isinstance(v, (int, float))
    )
    backlog = f", {queued} queued" if queued else ""
    return [{
        "component": "a2a", "level": "ok", "key": "status",
        "value": f"connected (broker {broker}, {peer_clause}{backlog})",
        "peers": peers,
    }]


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
            return [_entry("media", "ok", "enabled",
                           "not_configured (add a media: section to enable "
                           "generate_image / generate_video / text_to_speech / "
                           "transcribe_audio)")]
    except Exception as e:
        logger.warning("media_check_config_failed err=%s", e)
        return [_entry("media", "warning", "config", "unreadable", str(e))]
    data, entries = await _probe_plugin(client, "media")
    if entries:
        return entries
    assert data is not None

    # The plugin reports facts; shape the health entries here.
    if data.get("error"):
        return [_entry("media", "warning", "config", "error",
                       str(data.get("error")))]
    if not data.get("configured"):
        return [_entry("media", "ok", "enabled", "not_configured")]
    providers = data.get("providers") or []
    all_kinds = sorted({k for p in providers for k in (p.get("kinds") or [])})
    results: list[dict] = [_entry(
        "media", "ok", "enabled",
        f"{len(providers)} provider(s) ({', '.join(all_kinds) or 'no models'})",
    )]
    for p in providers:
        pid = p.get("id", "?")
        caps = ", ".join(p.get("kinds") or []) or "(no models)"
        if p.get("has_api_key"):
            results.append(_entry(
                "media", "ok", pid, f"{caps} ({p.get('api')})",
            ))
        else:
            results.append(_entry(
                "media", "warning", pid, caps,
                "No api_key set, so generation calls fail.",
            ))
    return results


# ═══════════════════════════════════════════════════════════════════════
# check_tool_catalog
# ═══════════════════════════════════════════════════════════════════════


async def check_tool_catalog(ctx=None) -> list[dict]:
    """Return the unified tool catalog's status (``tools.db`` + its index).

    ``tools.db`` is the single source of truth for every tool the agent can
    search or load, so a catalog that failed to open or a semantic drain that
    stalled degrades ``tool_search`` silently.  The catalog lives in the main
    process (not a plugin), so there is no MCP client to probe: the raw facts
    come from :func:`slife.mcp.host_server._host_catalog_facts` — the same
    probe the host-as-plugin ``__check`` serves — and are interpreted here.

    Unlike the plugin checks this one needs the whole ``ToolContext`` (the
    catalog service is a context field).  A subagent opens the SAME ``tools.db``
    (``_init_catalog`` runs for both processes), so the db half of this report
    is identical either way — and a missing catalog is a fault in both, not a
    worker's normal state.

    The SEMANTIC half is the one genuinely process-local piece: the drainer is
    main-agent-only (``service.py``), so a worker holds the index but not the
    thing that fills it.  That is reported as a fact about where it is
    maintained, never as an unconfigured endpoint.
    """
    catalog = getattr(ctx, "catalog", None) if ctx is not None else None
    if ctx is not None and catalog is None:
        # A worker opens the SAME tools.db (``_init_catalog`` runs for both),
        # so a missing catalog is a real fault in either process — not the
        # "shares no catalog" the docstring imagined.  Reported for both.
        return [{
            "component": "tool_catalog", "level": "warning", "key": "db",
            "value": "unavailable",
            "hint": ("Restart slife. tool_search and func-tool-load fall back to "
                     "name matching meanwhile, and the session log has the reason."),
        }]
    if catalog is None:
        return []

    from slife.mcp.host_server import _host_catalog_facts
    facts = await _host_catalog_facts(catalog)
    if facts.get("error"):
        return [{"component": "tool_catalog", "level": "warning", "key": "db",
                 "value": "probe failed", "hint": str(facts["error"])}]

    # "servers WITH ROWS", and no family split.  The count is over catalog
    # rows, so a configured server whose tool list has not been read yet owns
    # none and is absent — while `mcp_servers` / `rest-api` still list it.
    # Naming the population is what keeps the two lines from reading as one
    # number; splitting it by family only put the mcp/rest-api implementation
    # detail back on a surface where it means nothing.
    entries = [_entry(
        "tool_catalog", "ok", "db",
        f"{facts.get('tools', 0)} tools, {facts.get('servers', 0)} servers "
        f"with rows, {facts.get('loaded', 0)} loaded",
    )]
    sem_level, sem_value, sem_hint = _semantic_facts(
        facts.get("semantic") or {}, pending_noun="tools",
    )
    entries.append(_entry("tool_catalog", sem_level, "semantic", sem_value, sem_hint))
    return entries


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
    data, entries = await _probe_plugin(
        client, "job-coding", log_name="job_coding",
    )
    if entries:
        return entries
    assert data is not None

    if data.get("error"):
        return [_entry("job-coding", "warning", "config", "error",
                       str(data.get("error")))]

    jobs = data.get("job_names") or []
    jobs_dir = data.get("jobs_dir") or "(none)"
    if jobs:
        entries: list[dict] = [_entry(
            "job-coding", "ok", "jobs", f"{len(jobs)} ({', '.join(jobs)})",
        )]
    else:
        entries = [_entry("job-coding", "ok", "jobs",
                          f"none (add a .py file to {jobs_dir} or use job-write)")]

    model = data.get("llm_model") or ""
    if model in ("", "?", "unconfigured"):
        entries.append(_entry(
            "job-coding", "warning", "llm_model", "unconfigured",
            "Set job_coding_model in slife.yaml. Jobs that call llm.chat "
            "fail without it, while pure-computation jobs still work.",
        ))
    else:
        entries.append(_entry("job-coding", "ok", "llm_model", model))

    # Jobs reach MCP tools through this gateway; without a port those jobs
    # fail while pure-computation ones keep working.  Holding no connection
    # is the DESIGN, not degradation: the modern protocol is stateless, so
    # each ``mcp.call`` opens its own short-lived client against this port
    # and the port is the whole live fact.  Only a missing port is a
    # problem, and no health probe can change it (the probe never connects),
    # so the remedy never claims a re-run will.
    gw = data.get("mcp_gateway")
    if isinstance(gw, dict):
        port = gw.get("port")
        if gw.get("error"):
            entries.append(_entry("job-coding", "warning", "mcp_gateway",
                                  "unknown", str(gw["error"])))
        elif not port:
            entries.append(_entry(
                "job-coding", "warning", "mcp_gateway",
                "unavailable (no gateway port)",
                "Jobs that call mcp.call fail without it. The gateway "
                "publishes its port when it starts, so restart slife to "
                "respawn it.",
            ))
        else:
            entries.append(_entry("job-coding", "ok", "mcp_gateway",
                                  f"on demand (port {port})"))
    return entries


#: Plugin-backed health checks — derived from the central plugin contract so
#: ``system_health`` always enumerates exactly the declared plugins (no hand
#: list to drift from the registry).  The non-plugin checks are appended by
#: hand: the catalog lives in the main process, and the active embedding
#: endpoint / the watchdog are not plugins at all.
_SPEC_CHECKS: list[tuple[str, str | None]] = [
    (health_check_name(spec.name), spec.ctx_field)
    for spec in PLUGIN_SPECS.values()
    if spec.health
]
_CHECK_FUNCTIONS: list[str] = [name for name, _ in _SPEC_CHECKS] + [
    "check_tool_catalog",
    "check_embeddings",
    "check_watchdog",
]

#: Checks that need the whole ``ToolContext`` rather than a plugin client —
#: the catalog is an in-process service exposed as a context field, not an MCP
#: server to probe.
_CTX_CHECKS: frozenset[str] = frozenset({"check_tool_catalog"})

#: check_* function → ToolContext client field it reaches live plugin state
#: through (also derived from the spec).
_CLIENT_FIELD: dict[str, str] = {
    name: ctx for name, ctx in _SPEC_CHECKS if ctx
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
            if func_name in _CTX_CHECKS:
                # In-process checks (the tool catalog) read the context's
                # services directly — there is no plugin to probe.
                entries = await fn(ctx=ctx)
            elif field is not None:
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
                "key": f"{func_name}_failed", "value": f"{func_name} failed",
                "hint": f"{type(e).__name__}: {e} — the rest of this report "
                        f"is unaffected.",
            })
    return all_entries


def _dedupe_records(startup: list[dict], live: list[dict]) -> list[dict]:
    """Merge startup records with live check entries without double-reporting.

    One rule: **a startup record is dropped when a live entry covers the same
    ``(component, key)``.**  Producers name their component after the live
    check that re-reports it (``mcp_servers``, ``watchdog``, ``wechat``,
    ``a2a``), so the match needs no alias table — the earlier component-pair
    map is exactly what let ``a2a`` report ``status`` twice.

    The live entry wins in BOTH directions: a live "disconnected" is not
    masked by a stale "connected" record, and a stale startup warning is not
    resurrected next to a live "connected" (contradictory health).  A startup
    record no live entry covers is kept — e.g. the gateway was unreachable, so
    the record is the only evidence of what did start.
    """
    live_keys = {
        (e.get("component"), e.get("key"))
        for e in live
        if isinstance(e.get("component"), str)
        and isinstance(e.get("key"), str) and e.get("key")
    }

    kept: list[dict] = []
    for e in startup:
        pair = (e.get("component"), e.get("key"))
        if isinstance(pair[0], str) and isinstance(pair[1], str) and pair[1] and pair in live_keys:
            logger.debug(
                "health_startup_record_superseded component=%s key=%s", *pair,
            )
            continue
        kept.append(e)
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

    ``info`` is treated as non-problematic (e.g. disabled servers).  A level
    the renderer does not recognise — a typo in a check — counts as a warning
    instead of being reported as healthy, which is what an unknown state
    deserves in a report whose whole job is surfacing degradation.
    """
    levels: set[str] = set()
    for e in entries:
        level = str(e.get("level", "ok"))
        levels.add(level if level in _LEVELS else "warning")
    if "error" in levels:
        return "error"
    if "warning" in levels:
        return "warning"
    return "ok"


# ── Report rendering ───────────────────────────────────────────────────
# The report is plain text, not JSON, and that is a consequence of the
# harness's two result budgets rather than a style preference: a tool result is
# tail-cut at the live cap, and above ``memory_tool_result_chars`` it is
# head+tail cut at save (the middle is dropped for good).  A line-oriented
# report degrades to *fewer whole lines*; a JSON document degrades to an
# unparseable fragment — which is exactly what the previous JSON report did to
# itself.  So: the verdict and the problems come first, and the report is built
# to fit the save budget in the first place.

#: Severity rank — lower sorts first.  An unknown level ranks as a warning
#: instead of raising, so a check with a typo'd level still renders.
_RANK: dict[str, int] = {"error": 0, "warning": 1, "info": 2, "ok": 3}
_RANK_UNKNOWN = 1

#: The levels a check may emit (``info`` = intentionally off, not a problem).
_LEVELS = frozenset({"ok", "info", "warning", "error"})

#: Component order: live subsystems first, then the in-process checks, then the
#: static startup records.  The tail is what a truncated report loses, and the
#: environment facts are the least actionable thing in it.
_ORDER: dict[str, int] = {
    "system_health": 0,   # a check that blew up is the most alarming entry
    "mcp_servers": 1, "rest-api": 2, "tool_catalog": 3, "memdb": 4,
    "memfiles": 5, "wechat": 6, "sharefile": 7, "a2a": 8, "media": 9,
    "job-coding": 10, "embeddings": 11, "watchdog": 12,
    "local-embed": 13,
}

#: Static startup records (environment facts) — last by design.
_ENV_COMPONENTS: tuple[str, ...] = (
    "config", "model", "subagent", "node", "npm", "bun", "uv",
)
_ORDER_FALLBACK = 20
_ORDER_ENV = 30

#: Cap on the key list a collapsed fact prints — a 200-server outage must not
#: print 200 names (the count still tells the truth).
_KEY_LIST_CAP = 24

#: The report is kept under the save-side compaction budget
#: (``memory_tool_result_chars``, 8000) so the model's permanent copy of a
#: health check is the whole report rather than a head and a tail with the
#: middle amputated.  Only the healthy section is ever folded to fit.
_MAX_REPORT_CHARS = 7000

#: Problem components named in the verdict line before it is truncated.
_MAX_VERDICT_NAMES = 8


def _order(component: str) -> int:
    if component in _ORDER:
        return _ORDER[component]
    if component in _ENV_COMPONENTS:
        return _ORDER_ENV + _ENV_COMPONENTS.index(component)
    return _ORDER_FALLBACK


def _rank(level: str) -> int:
    return _RANK.get(level, _RANK_UNKNOWN)


def _collapse(entries: list[dict]) -> list[dict]:
    """Reduce a component's entries to one item per distinct (level, value, hint).

    Entries agreeing on all three are one fact reported once per key — the
    shape 20 disconnected MCP servers arrive in, and the reason the old report
    repeated the same sentence 19 times.  ``value`` is part of the key on
    purpose: two servers whose errors differ are two facts, and merging them
    would silently drop one.
    """
    groups: dict[tuple[str, str, str], dict] = {}
    for e in entries:
        level = str(e.get("level") or "ok")
        value = str(e.get("value") or "")
        hint = str(e.get("hint") or "")
        item = groups.setdefault(
            (level, value, hint),
            {"level": level, "value": value, "hint": hint, "keys": []},
        )
        if e.get("key"):
            item["keys"].append(str(e["key"]))
    items = list(groups.values())
    for item in items:
        item["keys"] = sorted(set(item["keys"]))
    # Problems lead inside a component; the wider group breaks the tie, so the
    # line reads "19 disconnected" before a one-off.
    items.sort(key=lambda i: (_rank(i["level"]), -len(i["keys"]), i["value"]))
    return items


def _format_fact(item: dict) -> str:
    keys = item["keys"]
    value = item["value"]
    if len(keys) > 1:
        names = ", ".join(keys[:_KEY_LIST_CAP])
        if len(keys) > _KEY_LIST_CAP:
            names += f", +{len(keys) - _KEY_LIST_CAP} more"
        return f"{value} [{names}]" if value else f"[{names}]"
    if len(keys) == 1:
        return f"{keys[0]}={value}" if value else keys[0]
    return value


def _component_line(items: list[dict], *, hints: bool) -> str:
    """One component's facts as a single line, with remedies when *hints*.

    A remedy is attached to the line rather than to one fact because in
    practice every problem fact in a component shares it (19 disconnected
    servers, one sentence); when they genuinely differ, each fact carries its
    own so the pairing stays unambiguous.
    """
    facts = [_format_fact(i) for i in items]
    if not hints:
        return "; ".join(f for f in facts if f)
    remedies = {i["hint"] for i in items
                if i["hint"] and i["level"] in ("warning", "error")}
    if len(remedies) == 1:
        return "; ".join(f for f in facts if f) + f" — {remedies.pop()}"
    parts = []
    for item, fact in zip(items, facts):
        if not fact:
            continue
        remedy = item["hint"] if item["level"] in ("warning", "error") else ""
        parts.append(f"{fact} — {remedy}" if remedy else fact)
    return "; ".join(parts)


def _plural(count: int, noun: str, plural: str = "") -> str:
    if count == 1:
        return f"{count} {noun}"
    return f"{count} {plural or noun + 's'}"


def _verdict(problems: list[tuple], ok_count: int) -> str:
    """The one-line verdict that leads the report."""
    checked = len(problems) + ok_count
    if not problems:
        return f"HEALTHY — {_plural(checked, 'component')} checked, no problems."
    errors = sum(1 for p in problems if p[3] == "error")
    warnings = len(problems) - errors
    names = [p[2] for p in problems]
    shown = ", ".join(names[:_MAX_VERDICT_NAMES])
    if len(names) > _MAX_VERDICT_NAMES:
        shown += f", +{len(names) - _MAX_VERDICT_NAMES} more"
    return (f"DEGRADED — {_plural(len(problems), 'problem')} "
            f"({_plural(errors, 'error')}, {_plural(warnings, 'warning')}): "
            f"{shown}; {_plural(ok_count, 'component')} OK.")


def _section_width(names: list[str]) -> int:
    """Column width for a section's component names (capped, so one long name
    cannot push every fact off the screen)."""
    return min(max((len(n) for n in names), default=0), 15)


def _render_report(groups: dict[str, list[dict]]) -> str:
    """Render merged, grouped health entries into the LLM-facing report.

    Layout::

        system_health: <verdict>

        ## Problems
        [WARN]  <component>: <fact(s)> — <remedy>

        ## Components OK
        <component>: <fact(s)>

    A component appears in exactly one section (by its worst level) and shows
    all of its facts there, so "what is the state of memdb?" is answered in one
    place.  ``## Problems`` is omitted when there is nothing wrong.
    """
    problems: list[tuple] = []
    oks: list[tuple[str, str]] = []
    for component, entries in groups.items():
        status = _component_status(entries)
        line = _component_line(_collapse(entries), hints=status != "ok")
        if status == "ok":
            oks.append((component, line))
        else:
            problems.append((_rank(status), _order(component), component,
                             status, line))
    problems.sort()  # worst level first, then report order
    oks.sort(key=lambda o: (_order(o[0]), o[0]))

    head = [f"system_health: {_verdict(problems, len(oks))}"]
    if problems:
        width = _section_width([p[2] for p in problems])
        head += ["", "## Problems"]
        for _rank_i, _order_i, component, status, line in problems:
            marker = "[ERROR]" if status == "error" else "[WARN] "
            head.append(f"{marker} {component.ljust(width)}: {line}")

    def _ok_block(fold: bool) -> list[str]:
        if not oks:
            return []
        width = _section_width([c for c, _ in oks])
        if not fold:
            return ["", "## Components OK"] + [
                f"{c.ljust(width)}: {line}" for c, line in oks
            ]
        # Folding is the only lossy path in the renderer, it never touches the
        # problems, and it says so — a silently middle-cut report is worse.
        return (["", "## Components OK"]
                + [c.ljust(width) for c, _ in oks]
                + ["", f"(detail omitted to fit the result budget — "
                       f"{_plural(len(oks), 'healthy component')}; re-run "
                       f"system_health and read one component's line below)"])

    report = "\n".join(head + _ok_block(fold=False))
    if len(report) > _MAX_REPORT_CHARS:
        report = "\n".join(head + _ok_block(fold=True))
    return report


class SystemHealthTool(Tool):
    """Run all subsystem checks + startup records → unified health report."""

    name = "system_health"
    category: ClassVar[str] = "System"
    description = ("One-call health report over every subsystem: problems first "
                   "with the remedy, then one line per healthy component "
                   "(startup records included). No arguments.")
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        startup = get_startup_records()
        dynamic = await _run_checks(ctx=getattr(self, "_ctx", None))
        groups = _group_by_component(_dedupe_records(startup, dynamic))
        return _render_report(groups)


# ═══════════════════════════════════════════════════════════════════════
# system_tools_list
# ═══════════════════════════════════════════════════════════════════════


def _system_category(tool) -> str:
    """Display category for a tool in ``system_tools_list``.

    Source-based, no name-prefix guessing:
      - builtin tools carry their own ``category`` class attribute;
      - built-in plugin tools (MCP proxy tools) group by their plugin
        name — the grouping heading carries the identity, so the
        per-tool ``[<server>] `` description prefix is stripped below.
    External MCP proxies and job tools are filtered out before this is called.
    """
    if isinstance(tool, MCPProxyTool):
        return getattr(tool, "_server", "") or "Plugins"
    return getattr(tool, "category", "") or "Other"


def _strip_server_prefix(tool, desc: str) -> str:
    """Remove the ``[<server>] `` prefix MCPProxyTool stamps on its
    description.  In ``system_tools_list`` the plugin name is already the
    group heading — the per-line prefix is redundant noise (tools are bare
    names, no ``server__`` prefix)."""
    if isinstance(tool, MCPProxyTool):
        server = getattr(tool, "_server", "")
        prefix = f"[{server}] "
        if server and desc.startswith(prefix):
            return desc[len(prefix):]
    return desc


def _is_system_tool(tool) -> bool:
    """True for a tool the SYSTEM itself provides.

    System = builtin (a module in ``slife/tools/``) + the built-in plugins'
    own tools.  Two things are deliberately NOT system tools: an external
    MCP/REST tool (`{server}__{tool}`, someone else's server), and a JOB
    (``job-<function>``) — that one is the user's own code, inventoried by
    ``job-list`` and searchable in the catalog like any other row.
    """
    if isinstance(tool, MCPProxyTool):
        if getattr(tool, "_route", None) == ProxyRoute.EXTERNAL:
            return False
        from slife.tools.catalog_service import plugin_category
        return plugin_category(
            getattr(tool, "_server", "") or "", getattr(tool, "name", "") or "",
        ) != "job"
    return True


class SystemToolsListTool(Tool):
    name: ClassVar[str] = "system_tools_list"
    category: ClassVar[str] = "System"
    description: ClassVar[str] = (
        "List the system's own tools — builtin modules and built-in plugin "
        "tools — grouped by category (harness/auto-invoked markers; jobs and "
        "external MCP excluded)."
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

        # Two exclusions, one reason — each already has an owner that lists it:
        # an external server's tools ride the request's `tools` array of every
        # request (their schemas are already in context), and a job is the
        # user's own tool, inventoried by `job-list`.
        system = [t for t in all_tools if _is_system_tool(t)]
        if not system:
            return "No system tools are currently registered."

        lines = [f"## System Tools ({len(system)} total)\n"]
        groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for t in sorted(system, key=lambda t: t.name):
            cat = _system_category(t)
            desc = t.description.split(".")[0].strip() + "."
            desc = _strip_server_prefix(t, desc)
            groups[cat].append((t.name, desc))

        for cat in sorted(groups):
            items = groups[cat]
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
    # Sanitize at storage time, not at check_async time: while a task runs
    # its raw output sits in memory (up to the stream caps), and a save-side
    # re-sanitize would leave secrets visible to anyone between run and poll.
    from slife.logfmt import sanitize_secrets
    result = sanitize_secrets(result)
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
        # A failed async task must keep the "Error" prefix so the loop's
        # is_error detection (result.startswith("Error")) and the TUI's
        # red render both work — wrapping it in the success banner would
        # hide the failure.
        if result.startswith("Error"):
            return f"Error: Async task failed (task_id: {task_id})\n\n{result}"
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
        # Restart the "Context covers" range — otherwise the next _turn_prompt
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
# set_midturn_input
# ═══════════════════════════════════════════════════════════════════════


class SetMidturnInputTool(Tool):
    name = "set_midturn_input"
    category: ClassVar[str] = "System"
    description = (
        "Allow inbound messages (peer tasks, chat) to cut into the running "
        "turn at the next safe point (true, the default), or queue them "
        "until the turn ends (false — the original strict behavior)."
    )
    parameters = make_params(
        enabled={
            "type": "boolean",
            "description": (
                "true = mid-turn cut-in allowed (default); "
                "false = messages queue until the running turn ends."
            ),
        },
    )

    async def execute(self, enabled: bool = True, **kwargs) -> str:
        setter = getattr(self, "_ctx", None)
        if setter is not None:
            setter = setter.set_midturn_input
        if setter is None:
            return "Error: agent service is not available yet — call this after the agent service has started."
        return setter(enabled)


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
                "description": "Notification title.",
            },
            "message": {
                "type": "string",
                "description": "Notification body (one sentence).",
            },
        },
        "required": ["message"],
    }

    async def execute(self, title: str = "", message: str = "", **kwargs) -> str:
        if err := require_params(message=message):
            return err

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

        # t() applies str.format, and an LLM-authored {message} must never
        # raise KeyError inside the tool — escape braces so a message like
        # "Deploy failed — see {output}" renders literally.
        return t(
            "notify_sent",
            title=title.replace("{", "{{").replace("}", "}}"),
            message=message.replace("{", "{{").replace("}", "}}"),
        )


