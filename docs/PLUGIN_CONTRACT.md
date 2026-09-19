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

- **Child plugins** are the only true plugins.  Each ships a `server.py` with
  a `main()`; the harness spawns it as a child process
  (`sys.executable -m <module>`), connects over MCP **Streamable HTTP**,
  registers its tools, and supervises it.  Most live under `slife.plugins.*`;
  **`local-embed` is the exception** — it ships from its own workspace package
  (`local_embed.server`) because it is also runnable standalone, and it is the
  one plugin with a `fixed_port` (its config pins the port a static
  embeddings `base_url` points at).
- The **MCP gateway** (`mcp-gateway`) is one of those child plugins.  It is the
  *gateway to external MCP servers*: it self-hosts `tools.yaml`,
  connects third-party MCP servers, and re-exposes their tools as
  `{server}__{tool}` proxies.  It is the only plugin whose spec has
  `gateway=True`.
- **Not plugins** (never discovered, never spawned, not in the registry):
  - the main agent's in-process **host server** (`slife/mcp/host_server.py`,
    "slife-as-plugin") — the running agent exposes its live `ToolRegistry`
    over MCP on an OS-assigned free port (no fixed well-known address;
    published as `SLIFE_HOST_PORT` so concurrent agents never collide on
    one host).  It runs **in** the main process and is deliberately placed
    outside `slife.plugins.*` so discovery can't spawn it;
- Third-party capability enters *only* as a standard MCP server registered in
  `tools.yaml` through the `mcp-gateway`.  There is no
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
    fixed_port: bool = False    # the child binds a port it must KNOW first
```

The current built-ins (in `PLUGIN_SPECS` order):

| name | module | ToolContext field | notes |
|---|---|---|---|
| `local-embed` | `local_embed.server` | `local_embed_client` | `fixed_port`; first, because memdb/memfiles embed against it when it is the active provider |
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
- ToolContext field name is per-plugin (the historical non-uniformities are
  kept: `a2a → a2a_mcp_client`, `job-coding → job_coding_client`).
- port env: `plugin_port_env(name)` → `SLIFE_{NAME}_PORT` (dashes→underscores),
  e.g. `SLIFE_MCP_GATEWAY_PORT`, `SLIFE_JOB_CODING_PORT`.  This is the key the
  parent publishes and subagents read to share a plugin.
- system-health check function: `health_check_name(name)` →
  `check_<name>` (e.g. `check_mcp_gateway`).
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

A plugin is ready when the harness's connect-time protocol negotiation
completes — the server only answers after its own FastMCP lifespan finished,
so the completed negotiation *is* the ready signal (`mark_initialized`).
That negotiation is era-dependent and the harness never configures it: a
modern plugin (ours — the SDK answers `server/discover`) is adopted at
2026-07-28, a legacy one gets the `initialize` handshake (`slife/mcp/era.py`).  The
per-plugin serving requirement is encoded in the lifespan (memdb/memfiles
require a usable store); subordinate/external dependencies (the gateway's
external servers, sharefile's tunnel, wechat's login, media providers, a2a's
broker, embedding backends) are **not** readiness conditions — they surface
via their own status tools and never gate readiness.

A *required* plugin (`plugins.required` in `slife.yaml`; the shipped config
sets `["memdb", "memfiles"]`) failing to become ready **aborts startup**
instead of limping on.  The spawn hang-guard is bounded by the registry's
`ready.plugin_start` = **60 s** (a 30 s cap previously misfired on slow
machines), and the service opens for user input only once every plugin spawn
has converged (ready / skipped / failed — `_startup_converged`) so input can
never race ahead of plugin startup.

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
     bare-name proxies, filter `__`-internal tools, mirror them into the
     shared catalog (`category='plugin'`, `source_id=<plugin>`; job-coding's
     `job-<function>` tools instead land as `category='job'`), clear any
     `error` mark, `mark_initialized`;
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
exit it unregisters the plugin's exact registered bare-name tools (plus any
registry tools bound to the dead client), disconnects the dead client, and
**restarts through the full uniform start** (spawn + ctx re-point +
after-ready) with exponential backoff — `_WATCHDOG_BACKOFF_INITIAL` (1 s) →
2 s → 4 s → … capped at `_WATCHDOG_BACKOFF_MAX` (30 s) — up to
`_WATCHDOG_MAX_RESTARTS` (5) consecutive failures, after which the watchdog
gives up and logs.  A restart re-runs `_arm_watchdog`'s restart path with
`allow_gate=False` and then tells every live subagent sharing the plugin its
new port (`worker/plugin_restart`).  The restart counter resets **only when
the crashed child had stayed up ≥ `_WATCHDOG_STABLE_UPTIME` (60 s)** — a fast
boot-loop is deliberately NOT reset, so a crashing plugin accumulates toward
the cap.  The poll/restore tasks are reaped (`cancel_tasks`) before each
respawn so a restart never stacks a second loop.  Restart state is recorded
through `slife.health` under the `watchdog` component, keyed per plugin —
surfaced by `check_watchdog`.  Subagents do **not** have their own watchdog:
they connect to the main agent's plugin processes via HTTP, so a subagent
crash only kills the subagent, never the shared infrastructure.

### Stop

`PluginLifecycle.stop()` is uniform: it cancels the watchdog, cancels the
plugin's supervised tasks (it knows about `poll_task`/`restore_task` itself —
callers never pass a flag), disconnects the client, and stops the child.  The
app's shutdown and a subagent's teardown both call the same registry-wide
stop; the per-name `start_wechat`/`start_a2a`/`stop_memdb`/… methods no
longer exist.

**Stopping a plugin stops its whole tree, and a dead slife stops it too.**
The registry owns one process per plugin, but a plugin may spawn children of
its own — the sharefile tunnel's `cloudflared`, every external MCP server the
gateway runs — and those are reachable only through it.  Both stop ladders
therefore kill the tree rather than the child (`taskkill /F /T` on Windows,
where `terminate()` is a single-process TerminateProcess; on POSIX the
descendants are read from `ps` *before* anything is signalled — a dead
parent's children are reparented to init — and swept after the child is
reaped, because a process-group kill cannot reach them: a plugin's own child
is deliberately spawned in its own session, which is what puts it outside the
group).  The stop path, however, only runs while
slife is alive to run it: a hard-killed parent (Ctrl+C, Task Manager, a
crash) unwinds no Python at all, so each spawned child is additionally
assigned to a **kill-on-close job object** at spawn — before it can spawn
anything of its own — and the kernel then terminates whatever is still inside
when slife dies, for any reason.  The assignment happens in the uniform spawn
(`MCPWrapperProcess.start`), so no plugin carries cleanup code for it.

### Subagents share the parent's plugins

A subagent (worker) never spawns its own plugins.  It runs the same manifest
loop over `discover_plugins()`: for each plugin with a published
`SLIFE_<NAME>_PORT` it `connect_plugin_http(name, port)` (spec-driven: ctx
re-point; gateway workers also reconcile external proxies; wechat/a2a workers
never start poll/drain — that stays with the main agent).  When the parent
restarts a plugin, the worker reconnects on the new port.  On exit the worker
disconnects every shared client (`stop_all_plugins`).

---

## The child environment

The harness hands the child its identity and its serving ports through the
process environment (`create_subprocess_exec(env=…)`) — there is no other
in-band channel for these before the first request:

| Variable | Purpose |
|----------|---------|
| `SLIFE_SESSION_ID` / `SLIFE_AGENT_NAME` | Log correlation, agent identity |
| `SLIFE_DATA_DIR` / `SLIFE_CONFIG_DIR` / `SLIFE_LOG_DIR` | Directory overrides |
| `SLIFE_PLUGIN_NAME` | Which plugin this child is |
| `SLIFE_{NAME}_PORT` | Published port of each plugin (`LOCAL_EMBED` / `MCP_GATEWAY` / `MEMDB` / `WECHAT` / `MEMFILES` / `A2A` / `MEDIA` / `JOB_CODING` / `SHAREFILE`). Key is the uppercased plugin name with dashes normalised to underscores (`job-coding` → `SLIFE_JOB_CODING_PORT`) — via `plugin_port_env`. Subagents read this env to share the parent's plugins. `local-embed` normally publishes the port its own config pins (its `fixed_port`), so it is the same value in a spawned child and a standalone one. When that port is already served by another local-embed the child **adopts** it and serves MCP on an OS-assigned port instead, publishing that — the embeddings `base_url` is unaffected either way, since it points at the config's port, not at this one. See [local-embed → Adopting a running service](../local-embed/README.md#adopting-a-running-service). |
| `SLIFE_SHAREFILE_URL` | Public tunnel URL (set inside the sharefile plugin process) |

**WSL note:** custom env vars set via `create_subprocess_exec(env=…)` are NOT
forwarded to Windows `.exe` processes through WSL interop (`WSLENV` is only
read by the WSL `/init` at session start).  All MCP server runtimes on WSL
must therefore be Linux-native binaries — the install script enforces this by
detecting `/mnt/*` paths.

---

## Health (`system_health`)

`system_health` (`slife/tools/system.py`) enumerates plugin checks **from the
registry**: for each spec with `health=True` it runs `check_<name>` and binds
that check to the plugin's `ctx_field` client.  The non-plugin checks are
appended by hand — `check_tool_catalog` (the catalog service is an in-process
context field, not a plugin), `check_embeddings` and `check_watchdog`.  Each
`check_*` encodes its subsystem's semantics (probes the plugin's `__check`
internal tool, reads config, etc.) — the *enumeration* is derived, the
implementations stay bespoke.

**The entry contract a check must keep.**  Every check returns flat entries of
`component` / `level` / `key` / `value` / `hint`, where **`value` is the fact
and `hint` is what to do about it**:

- `value` must be self-contained — it is what the healthy section of the report
  prints, and a healthy entry is rendered as `value` alone;
- `hint` is rendered **only** for `warning`/`error` entries, so an `ok`/`info`
  entry must not carry one (a fact parked there is invisible; the suite fails
  on it);
- neither may be a sentence that restates the component or the key — the
  renderer prints those already;
- a remedy must name a tool that exists (`mcp_list`, `wechat_login`,
  `embeddings_model_set`, …) — never a `check_*` function, which is internal;
- `info` means "intentionally off" (a disabled server): it is not a problem;
- any extra key is machine-only and never rendered.

The report itself is plain text (verdict → problems → one line per healthy
component).  The renderer collapses entries agreeing on
`(level, value, hint)` into a single fact with a key list, and a startup record
from `health.record` is dropped when a live entry covers the same
`(component, key)` — so a producer must name its component after the live check
that re-reports it.

---

## The child-process contract (`server.py`)

A plugin's `server.py` must:

1. bind a free port (`bind_free_port()`);
2. signal the parent once ready — `run_plugin_server(mcp)` (or with
   `sockets=[sock]`) wraps the lifespan and emits the port only **after** the
   app is ready to serve MCP; a plugin must never signal early;
3. start FastMCP on Streamable HTTP with the pre-bound socket;
4. expose `@mcp.tool`s — bare names = public, `__`-prefixed = internal.
   A public tool becomes a catalog row the moment the child is ready,
   so it is findable by `tool_search` (born `unloaded`: searchable, not
   injected, until `func-tool-load`).
   (called programmatically via `call_tool("__…")`, never exposed to the LLM);
   heavy post-readiness work goes through `warm_after_ready`;
5. be importable: `python -m <module>`.

There is no base class and no SDK — the contract is the `server.py` +
`run_plugin_server` shape plus the spec row.
