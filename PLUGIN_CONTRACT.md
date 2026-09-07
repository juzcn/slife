# Plugin Contract

The single, authoritative description of the Slife plugin system — how a
plugin is declared, discovered, started, supervised, shared with subagents,
health-checked, and routed.  **The code is `slife/plugins/spec.py` (the
declarative contract), `slife/agent/plugins.py` (the lifecycle + registry),
and the uniform engine in `slife/agent/service.py`.**  If a statement here and
a design note in `DESIGN.md` ever disagree, this document and the code win.

> This document was split out of `DESIGN.md` (§"The Plugin Contract") when the
> plugin system moved to a **central spec + registry** model.  `DESIGN.md`
> keeps the one-paragraph architecture overview and links here.

---

## Roles: what is and isn't a plugin

- **Child plugins** are the only true plugins.  Each is a Python package under
  `slife.plugins.*` that ships a `server.py` with a `main()`; the harness
  spawns it as a child process (`sys.executable -m <module>`), connects over
  MCP **Streamable HTTP**, registers its tools, and supervises it.
- The **MCP gateway** (`mcp-gateway`) is one of those child plugins.  It is the
  *gateway to external MCP servers*: it self-hosts `mcp-plugin.json5`,
  connects third-party MCP servers, and re-exposes their tools as
  `{server}__{tool}` proxies.  It is the only plugin whose spec has
  `gateway=True`.
- **Not plugins** (never discovered, never spawned, not in the registry):
  - the main agent's in-process **host server** (`slife/mcp/host_server.py`,
    "slife-as-plugin") — the running agent exposes its live `ToolRegistry`
    over MCP on a fixed port (`plugin_server.port`, default 17878).  It runs
    **in** the main process and is deliberately placed outside
    `slife.plugins.*` so discovery can't spawn it;
  - **local-embed** — a manually-started standalone daemon serving
    OpenAI-compatible `/v1/embeddings`.
- Third-party capability enters *only* as a standard MCP server registered in
  `mcp-plugin.json5` through the `mcp-gateway`.  There is no
  `plugins.external` mechanism.

---

## The central spec: one source of truth

Every child plugin is declared by one `PluginSpec` (frozen dataclass) in the
central, ordered table `PLUGIN_SPECS` (`slife/plugins/spec.py`).  Nothing else
in the harness hard-codes a plugin's module, enablement, or glue — every
name-keyed table that used to exist (`AgentService._plugins` seed, the start
if/elif chain, `_HTTP_CONNECT_GLUE`, the health `_CHECK_FUNCTIONS` list, the
tool-adapter route set, the mcp child's reserved names) has been replaced by
lookups into this one table.

```python
@dataclass(frozen=True)
class PluginSpec:
    name: str              # public name, hyphenated: "job-coding", "mcp-gateway"
    module: str            # server entrypoint: "slife.plugins.job_coding.server"
    ctx_field: str | None  # ToolContext attr holding the plugin's live MCP client
    gateway: bool = False       # mcp-gateway only
    host_params: bool = False   # mcp-gateway only (client_info_extra host params)
    enable_method: str | None   # AgentService coroutine name -> bool (gate)
    after_ready_method: str | None  # AgentService coroutine name -> glue
    health: bool = True         # enumerated by system_health
```

The current built-ins (in `PLUGIN_SPECS` order):

| name | module | ToolContext field | notes |
|---|---|---|---|
| `mcp-gateway` | `slife.plugins.mcp_gateway.server` | `mcp_client` | `gateway`, `host_params`; after-ready = mcp glue |
| `memdb` | `slife.plugins.memdb.server` | `memdb_client` | turns DB + semantic search |
| `memfiles` | `slife.plugins.memfiles.server` | `memfiles_client` | private file cabinet |
| `wechat` | `slife.plugins.wechat.server` | `wechat_client` | gate `_gate_wechat`; after-ready poll/restore |
| `sharefile` | `slife.plugins.sharefile.server` | `sharefile_client` | after-ready = tunnel watch |
| `a2a` | `slife.plugins.a2a.server` | `a2a_mcp_client` | gate `_gate_a2a`; after-ready drain |
| `media` | `slife.plugins.media.server` | `media_client` | optional generation |
| `job-coding` | `slife.plugins.job_coding.server` | `job_coding_client` | deterministic jobs |

Naming rules the table normalises:

- public name → hyphenated where the package cannot be: package
  `slife.plugins.job_coding`, public name `job-coding`; package
  `slife.plugins.mcp_gateway`, public name `mcp-gateway`.
- ToolContext field name is per-plugin (two historical non-uniformities are
  kept: `a2a → a2a_mcp_client`, `job-coding → job_coding_client`).
- port env: `plugin_port_env(name)` → `SLIFE_{NAME}_PORT` (dashes→underscores),
  e.g. `SLIFE_MCP_GATEWAY_PORT`, `SLIFE_JOB_CODING_PORT`.  This is the key the
  parent publishes and subagents read to share a plugin.
- system-health check function: `health_check_name(name)` →
  `check_<name>` with dashes→underscores (e.g. `check_mcp_gateway`).
- reserved (mcp child): `mcp_child_reserved_names()` = every built-in plugin
  name — an external server registered through the gateway may not take one.

### Where behavior lives

Enable gates and after-ready glue touch service internals (config, inbox,
`ToolContext`), so they are **methods on `AgentService`**, referenced from the
spec *by name* (`enable_method` / `after_ready_method`) and bound once in
`AgentService.__init__` (`_resolve_plugin_behaviors`, which asserts a spec
never names a missing method).  `spec.py` stays stdlib-only, so it is safe to
import from `tool_adapter`, `tools.system`, and the mcp child process without
pulling in `AgentService`.

### Auto-discovery stays open

`discover_plugins()` (`slife/plugins/__init__.py`) returns every spec-declared
plugin whose `server.py` exists (in `PLUGIN_SPECS` order), then appends any
package under `slife.plugins.*` with a `server.py` that has **no** spec row —
an undeclared future internal plugin is started through the same generic
lifecycle with a default spec (`spec_for`): always starts, no ctx field, no
glue, DIRECT route.  Adding a real built-in is one `PluginSpec` row plus its
`server.py` package.

---

## The registry is the runtime truth

`AgentService.__init__` builds one **`PluginRegistry`**
(`slife/agent/plugins.py`) from `PLUGIN_SPECS`: every declared plugin gets a
`PluginLifecycle` eagerly, before any start/connect.  `AgentService._plugins`
**aliases** `registry.lifecycles` (never rebind it).  A lifecycle owns the
plugin's `client`, `process`, `port`, supervised background tasks
(`poll_task`, `restore_task`), watchdog state, readiness, and the exact
registered tool names.

Start/stop/watchdog/connect/health all iterate the registry — there is no
`if name == "…"` anywhere in the lifecycle engine.

---

## Lifecycle

### Readiness

A plugin is ready when the harness's MCP `initialize` handshake completes —
the server only answers `initialize` after its own FastMCP lifespan finished,
so the returned handshake *is* the ready signal (`mark_initialized`).  The
per-plugin serving requirement is encoded in the lifespan (memdb/memfiles
require a usable store); subordinate/external dependencies (the gateway's
external servers, sharefile's tunnel, wechat's login, media providers, a2a's
broker, embedding backends) are **not** readiness conditions — they surface
via their own status tools and never gate readiness.

### Start (uniform engine)

`start_plugin_server(name, module)` → `_start_plugin_server_impl` is the one
entry for every plugin:

1. idempotent: already running → `STARTED`;
2. **gate** (first start only; watchdog restarts skip it): the spec's
   `enable` hook returns False → `SKIPPED` (an *expected* no-op — wechat
   disabled, a2a without a reachable broker downgrades its config);
3. **uniform start** (`_start_plugin_uniform`):
   - `_spawn_plugin_generic` — spawn the child, set `SLIFE_<NAME>_PORT`,
     connect (host params when `host_params`), register the plugin's tools as
     bare-name proxies, filter `__`-internal tools, `mark_initialized`;
   - re-point `ToolContext.<ctx_field>` at the live client;
   - run the spec's **after-ready** hook (wechat poll/restore, a2a drain,
     sharefile tunnel watch, mcp enrichment);
   - `_arm_watchdog`.
4. spawn/hook failure → `FAILED`.

`PluginStartStatus` semantics are preserved everywhere: `STARTED` /
`SKIPPED` / `FAILED`; only required plugins abort startup.

### Watchdog

Every started plugin (built-in, gateway, or auto-discovered) is supervised by
the same `PluginLifecycle` watchdog (`_watchdog_loop`): on unexpected child
exit it unregisters the plugin's tools, disconnects the dead client, and
**restarts through the full uniform start** (spawn + ctx re-point +
after-ready) with exponential backoff up to `_WATCHDOG_MAX_RESTARTS` (5).
A restart re-runs `_arm_watchdog`'s restart path with `allow_gate=False` and
then tells every live subagent sharing the plugin its new port
(`worker/plugin_restart`).  The poll/restore tasks are reaped
(`cancel_tasks`) before each respawn so a restart never stacks a second
loop.  Restart state is recorded through `slife.health` under the
`watchdog` component, keyed per plugin — surfaced by `check_watchdog`.

### Stop

`PluginLifecycle.stop()` is uniform: it cancels the watchdog, cancels the
plugin's supervised tasks (it knows about `poll_task`/`restore_task` itself —
callers never pass a flag), disconnects the client, and stops the child.  The
app's shutdown and a subagent's teardown both call the same registry-wide
stop; the per-name `start_wechat`/`start_a2a`/`stop_memdb`/… methods no
longer exist.

### Subagents share the parent's plugins

A subagent (worker) never spawns its own plugins.  It runs the same manifest
loop over `discover_plugins()`: for each plugin with a published
`SLIFE_<NAME>_PORT` it `connect_plugin_http(name, port)` (spec-driven: ctx
re-point; gateway workers also reconcile external proxies; wechat/a2a workers
never start poll/drain — that stays with the main agent).  When the parent
restarts a plugin, the worker reconnects on the new port.  On exit the worker
disconnects every shared client (`stop_all_plugins`).

---

## Health (`system_health`)

`system_health` (`slife/tools/system.py`) enumerates plugin checks **from the
registry**: for each spec with `health=True` it runs `check_<name>` and binds
that check to the plugin's `ctx_field` client.  `check_local_embed` and
`check_watchdog` (not plugins) are appended.  Each `check_*` encodes its
subsystem's semantics (probes the plugin's `__check` internal tool, reads
config, etc.) — the *enumeration* is derived, the implementations stay
bespoke.  Startup records that a live check re-reports are de-duplicated.

---

## The child-process contract (`server.py`)

A plugin's `server.py` must:

1. bind a free port (`bind_free_port()`);
2. signal the parent once ready — `run_plugin_server(mcp)` (or with
   `sockets=[sock]`) wraps the lifespan and emits the port only **after** the
   app is ready to serve MCP; a plugin must never signal early;
3. start FastMCP on Streamable HTTP with the pre-bound socket;
4. expose `@mcp.tool`s — bare names = public, `__`-prefixed = internal
   (called programmatically via `call_tool("__…")`, never exposed to the LLM);
   heavy post-handshake work goes through `warm_after_handshake`;
5. be importable: `python -m <module>`.

There is no base class and no SDK — the contract is the `server.py` +
`run_plugin_server` shape plus the spec row.
