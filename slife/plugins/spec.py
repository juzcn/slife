"""Central plugin contract — the single declarative source of truth.

Every internal plugin is described by one :class:`PluginSpec`.  The harness
builds its runtime registry (``AgentService._registry`` →
``PluginRegistry``) from :data:`PLUGIN_SPECS` once; discovery, start, stop,
watchdog, system-health enumeration, subagent sharing and tool routing all
read from here instead of hard-coding plugin names or keeping parallel
name-keyed tables.  Adding a plugin = one spec row + a ``server.py`` package —
nothing else in the harness changes.

This module is dependency-light (stdlib only) so it can be imported by the
MCP plugin *child* process and by ``slife.tools.system`` / ``tool_adapter``
without dragging in ``AgentService``.  Per-plugin behavior that touches
service internals (enable gates, post-ready glue like poll/drain loops) is
*declared* here as an ``AgentService`` method-name string and resolved once
in ``AgentService.__init__`` — the spec stays import-safe, the runtime still
iterates the registry with no ``if name == ...``.

Only child plugins under ``slife.plugins.*`` belong here.  The main agent's
in-process host server (``slife.mcp.host_server``, the "slife-as-plugin"
face) and the ``local-embed`` standalone daemon are NOT plugins and are
deliberately absent — they are not spawned and never enter the registry.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PluginSpec:
    """Declarative contract for one internal child plugin.

    Attributes:
        name: Public plugin name, hyphenated where the package cannot be
            (``"job-coding"``).  UI / health / env-var / tool-tag identity.
        module: Server entry module spawned as ``python -m <module>``.
        ctx_field: Optional ``ToolContext`` attribute that receives the
            plugin's live MCP client after (re)start and on subagent HTTP
            connect.  ``None`` = the plugin exposes no harness-side client.
        gateway: ``mcp`` only — the wrapper that proxies external MCP
            servers: reconcile-style tool notification, host params, and the
            WRAPPER proxy route.
        host_params: ``mcp`` only — contributes ``client_info_extra``
            (host parameters) on connect.
        enable_method: Optional ``AgentService`` coroutine name returning
            bool.  ``None`` = always start.  Used for config-gated plugins
            (wechat disabled, a2a without a broker).  Runs on the initial
            start only, never on a watchdog restart.
        after_ready_method: Optional ``AgentService`` coroutine name run once
            after a child is ready (tools registered) — starts the wechat
            poll/restore tasks, the a2a drain loop, the sharefile tunnel
            watch, or the mcp enrichment glue.  Re-run on every restart.
        health: Whether ``system_health`` enumerates this plugin via its
            ``check_<name>`` function.
    """

    name: str
    module: str
    ctx_field: str | None = None
    gateway: bool = False
    host_params: bool = False
    enable_method: str | None = None
    after_ready_method: str | None = None
    health: bool = True


#: The 8 built-in child plugins, in deterministic start order.
#: ``ctx_field`` names are the ``ToolContext`` attributes each plugin's live
#: client is exposed on (note the two non-uniform names: ``a2a_mcp_client``
#: and ``job_coding_client``).
_PLUGIN_DEFS: tuple[PluginSpec, ...] = (
    PluginSpec(
        "mcp-gateway", "slife.plugins.mcp_gateway.server",
        ctx_field="mcp_client",
        gateway=True, host_params=True,
        after_ready_method="_after_ready_mcp",
    ),
    PluginSpec(
        "memdb", "slife.plugins.memdb.server",
        ctx_field="memdb_client",
    ),
    PluginSpec(
        "memfiles", "slife.plugins.memfiles.server",
        ctx_field="memfiles_client",
    ),
    PluginSpec(
        "wechat", "slife.plugins.wechat.server",
        ctx_field="wechat_client",
        enable_method="_gate_wechat",
        after_ready_method="_after_ready_wechat",
    ),
    PluginSpec(
        "sharefile", "slife.plugins.sharefile.server",
        ctx_field="sharefile_client",
        after_ready_method="_after_ready_sharefile",
    ),
    PluginSpec(
        "a2a", "slife.plugins.a2a.server",
        ctx_field="a2a_mcp_client",
        enable_method="_gate_a2a",
        after_ready_method="_after_ready_a2a",
    ),
    PluginSpec(
        "media", "slife.plugins.media.server",
        ctx_field="media_client",
    ),
    PluginSpec(
        "job-coding", "slife.plugins.job_coding.server",
        ctx_field="job_coding_client",
    ),
)

#: Public-name → spec, insertion-ordered.
PLUGIN_SPECS: dict[str, PluginSpec] = {s.name: s for s in _PLUGIN_DEFS}

#: Spec order (deterministic discovery / start ordering).
SPEC_ORDER: tuple[str, ...] = tuple(s.name for s in _PLUGIN_DEFS)


def spec_for(name: str, module: str | None = None) -> PluginSpec:
    """Return the spec for *name* — or a generic default for an
    auto-discovered package that has no spec row.

    Discovery stays open: a future package under ``slife.plugins.*`` with a
    ``server.py`` but no ``PLUGIN_SPECS`` entry gets a generic child spec
    (always starts, no ctx field, no hooks, DIRECT route) — the same contract
    a spec'd plugin gets, minus the harness glue.
    """
    if name in PLUGIN_SPECS:
        return PLUGIN_SPECS[name]
    return PluginSpec(
        name=name,
        module=module or f"slife.plugins.{name.replace('-', '_')}.server",
    )


def health_check_name(name: str) -> str:
    """System-health check function name for a plugin *name*.

    ``"job-coding"`` → ``"check_job_coding"`` (a check function is a Python
    identifier, so the public hyphen name is underscore-normalised).
    """
    return f"check_{name.replace('-', '_')}"


def mcp_child_reserved_names() -> frozenset[str]:
    """Plugin names the mcp gateway must reserve against external servers.

    External MCP servers registered through the gateway may not take one of
    these names, or their tools would collide / misroute with a built-in
    plugin's namespace (an external ``"mcp-gateway"`` server would even
    shadow the gateway's WRAPPER route).  Derived from :data:`PLUGIN_SPECS`
    — never hand-written, so every built-in plugin (including
    ``job-coding``) is always covered.
    """
    return frozenset(PLUGIN_SPECS)
