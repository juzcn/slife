# The Unified Tool System

> Authoritative design of Slife's tool catalog (the "Tool System" — DESIGNER_NOTES §8.5). Covers the six-category `tools.json5` model, the shared `tools.db` catalog, the load/unload threshold, the search surface, the per-turn injection chain, and MCP/REST-API integration. Reader: a developer working on tool discovery, loading, eviction, or the mcp-gateway reconcile. The everyday tool inventory lives in the [README](../README.md#tools); the plugin-side server contract in [PLUGIN_CONTRACT.md](PLUGIN_CONTRACT.md).

---

## 1 · Why a unified system

Before the rework: `tool_search` could only find MCP tools; every MCP server was loaded or unloaded **as a whole**; native tools always flooded the tool list. Three symptoms (§8.5):

- search could not address the other tool kinds (builtin / job / rest-api / skill / cli);
- eager-connecting every configured server paid connect cost for servers nobody used;
- loading was not granular — one MCP server's dozens of tools all entered the context at once, blowing up the schema box the model must read every turn.

The goal is one unified model: **every function tool** (builtin, job, mcp, rest-api) is a row in one shared catalog with a **loaded / unloaded** state; the LLM discovers with one `tool_search`, loads with one `tool_load`, and a **threshold** manages how many loaded tools fit in the context. The catalog (`tools.db`) is the single source of truth — with one schema per tool that the injection chain reads directly.

Key properties:

- **Unified search + load.** `tool_search` covers all six categories; `tool_load` loads any function tool by name.
- **Threshold-managed.** A configurable cap (`tool_load.threshold`, default 100) bounds how many function tools are injected; the harness evicts the oldest-by-usage at turn boundaries. Never evicted: the harness pair + the 11 meta tools (the whitelist), and anything the user lists under `preload`.
- **Granular.** Load/unload is per-tool, not per-server. A connected MCP server with 50 tools injects only the ones the model loaded.
- **DB-driven injection.** The schema injected into the LLM comes from the catalog's `schema` column — never re-fetched from the live MCP server or parsed from tool code.

---

## 2 · Configuration — `tools.json5`

The tool system is configured entirely in `tools.json5` (sibling of `slife.json5` in the data dir). It carries **one section per category** plus the **`tool_load`** policy section:

```
builtin:   [{name, enabled, ...overrides}]
mcp:       {servers: {<name>: {command|url, args, env, enabled, autoload, source, ...}}}
rest-api:  {<name>: {spec_url, base_url, api_key, ...}}        # OpenAPI via mcp-openapi-proxy
job:       [{name, enabled}]                                    # jobs are files in jobs/
cli:       [{name, enabled}]
skill:     [{name, enabled}]

tool_load: {threshold: 100, preload: ["execute_shell", ...]}
```

- Sections are the *only* knobs; the `tools:` array of `slife.json5` is retired. `tools.json5` is **the authority** — at startup the host mirrors its `mcp`/`rest-api` sections into the catalog db (`sync_config_servers`), and every `cli_set` / `rest_api_set` / `job-write` / etc. persists there and re-syncs.
- Per-category disable is a `{name, enabled: false}` list entry; the parsed disable sets (`disabled_builtin`, `disabled_jobs`, `disabled_skills`) are enforced where each category is mirrored.
- The gateway reads the **merged server view**: `mcp.servers` ∪ `rest-api`, with a legacy top-level `servers` fallback so a pre-section file keeps working (`_servers_dict`).

---

## 3 · The shared catalog — `tools.db`

`SLIFE_TOOLS_DB` (dev default: `<cwd>/tools.db`; tests point it at a per-test temp file). SQLite with WAL, cross-process safe; main agent, subagents and the mcp-gateway child all read the **same file**. One store class (`CatalogStore`) owns all SQL; policy (thresholds, refusal texts, owner checks) lives in `ToolCatalogService`.

### `server` — the external-server mirror

```
name         -- mcp or rest-api server name (the {server}__{tool} prefix)
description  -- human-readable
enabled      -- mirrors tools.json5
runtime      -- CONNECTED | DISCONNECTED | CONNECTING | ERROR  (live mirror of the wrapper pool)
error_reason -- last connect failure text
last_runtime -- the session-start snapshot of runtime (the "restart eager-connect set")
source       -- provenance dict JSON; source.type == "rest_api" ⇒ category rest-api
```

### `tool` — one row per tool

```
name        -- unique; external tools are "{server}__{tool}", everything else bare
description -- used by search + (for skill/cli, the tool's own surface)
category    -- builtin | job | mcp | rest-api | skill | cli
source_id   -- owning server name for mcp/rest-api, else NULL
schema      -- the DEEP tool descriptor {name, description, inputSchema} for function tools;
               SKILL.md content for skill rows; NULL for cli
enabled     -- config disable mirror (mcp/rest-api pass NULL → join governs)
status      -- loaded | unloaded for FUNCTION_CATEGORIES; NULL for skill/cli (no state)
last_loaded -- ISO timestamp, bumped on tool_load and on every successful execute (LRU key)
```

Only `builtin | job | mcp | rest-api` participate in the load state (the §8.5 status rule: "loaded unloaded only applies to function tools; skill and cli stay null"). Skill rows are written by `skill_load` (content = the loaded SKILL.md) but never carry a load state; cli rows mirror the cli section with `schema = NULL` — cli tools are always available.

### `tool_embeddings`

Chunked vector rows for semantic search (one tool → several chunks; closest-chunk aggregation). The drainer re-embeds on schema change; stale-width vectors are skipped at query time.

### Effective status

A tool's *effective* status is the raw `tool.status` **joined against its server row** — this is what the refusal matrix and the `tool_search` status filter use:

| condition | effective |
|---|---|
| function tool, `status='loaded'`, server (if any) CONNECTED | `loaded` |
| function tool, `status='unloaded'`, available | `unloaded` |
| server DISCONNECTED / not present | `unavailable` |
| server or config disabled | `disabled` |
| skill / cli (no state) | `n/a` |

Because disable and disconnect live on the **server row**, the same `tool` row expresses them with no per-tool edits — a disabled or disconnected server's tools simply join to `disabled` / `unavailable` and are filtered out of injection.

---

## 4 · Discovery & load — the meta surface

The whitelist (`slife/tools/whitelist.py`) protects two always-injected families, never LRU-evictable and not unloadable:

- **harness pair** — `_turn_prompt`, `_check_new_input`, `attach_image` (the loop's mechanism);
- **the tool-system meta tools (11)** — `mcp_search`, `mcp_connect`, `mcp_disconnect`, `mcp_set_enabled`, `rest_api_connect`, `rest_api_disconnect`, `rest_api_set_enabled`, `tool_search`, `tool_load`, `skill_load`, `_unload_function_tool`.

Together: `ALWAYS_LOADED`. Everything else seeds loaded and can be reloaded, but the squeeze may evict it.

### `tool_search`

Search the whole catalog:

```
query    -- free text over name/description/schema
category -- builtin|job|mcp|rest-api|skill|cli; empty = all
status   -- all|loaded|unloaded|disabled|unavailable; default all
mode     -- hybrid (default) | keyword | grep
```

Three retrieval routes share one row-join (server aliases included so the effective status is computed per result):

- `grep` — exact substring over name/description/category/schema;
- `keyword` — FTS5 BM25, CJK-routed to a LIKE multi-word AND fallback;
- `hybrid` — keyword + semantic KNN (sleepy `tool_embeddings`, closest-chunk per tool) merged by RRF.

### `tool_load`

Loads a function tool by **full name** (`{server}__{tool}` for external). Refusals come from the effective status: unknown → "see tool_search"; disabled → "enable it first"; unavailable → "server not connected — use mcp_connect". On success the row's status flips `loaded` and the injection snapshot is refreshed. For `mcp`/`rest-api` rows it also **materializes the execution proxy** from the row's schema descriptor (`create_proxy_tools` → registered in the registry) — loading and materialization are the same step, driven by the row.

### `_unload_function_tool`

Self-service unload (the spare-ticket the model can use to free a slot); refused for the whitelist. The harness's eviction is the same operation run automatically.

### `skill_load`

Returns the full SKILL.md and registers a discoverable `category=skill` row (status stays NULL); absent skills and a missing skills dir both return the not-found error without writing a phantom row.

---

## 5 · The load/unload lifecycle

### Boot ordering

1. Host `_init_catalog` opens `tools.db`, runs `session_start` (snapshots current runtimes → `last_runtime`), then `seed_inventory` seeds every registered function tool `loaded` (the session default — later squeezes trim).
2. `_sync_catalog_from_config` mirrors `tools.json5`'s `mcp`/`rest-api` sections into `server` rows and purges removed servers (config is the authority; blocking so startup is coherent).
3. The wrapper spawns; `_auto_connect_configured` reads the merged server view and **eager-connects** exactly the set whose `last_runtime = 'CONNECTED'` (the snapshot from the previous session's end). A server with no CONNECTED record — new, previously ERROR/DISCONNECTED — is registered `connect=False`; the health monitor or `mcp_connect` re-arms it. Failed auto-connects write `runtime=ERROR` into the catalog.
4. Connect events fire `notifications/tools/list_changed` → the host `_sync_mcp_proxies` reconcile (below).

### Per-turn injection

At each turn boundary the loop refreshes a **snapshot**: `snapshot_loaded()` = effective-loaded names ∪ `ALWAYS_LOADED`. The function list for the request is built from `rows_for_names(snapshot)` — the catalog `schema` column — via `_function_from_schema`. The registry key (full `{server}__{tool}` name) **always wins** over the descriptor's bare name, so the injected name is exactly what `registry.execute` resolves. A row missing its schema falls back to the materialized instance; no catalog → the whole registry (historical behavior).

### Threshold eviction

At the turn boundary, if loaded count exceeds `tool_load.threshold` (default 100), `evict_to_threshold` drops the excess via `evict_lru` — ordered `(last_loaded IS NULL) DESC, last_loaded ASC, name`, i.e. never-used first, then least-recently-used. Protected from eviction: `ALWAYS_LOADED` ∪ `preload` (the `tool_load.preload` names). Two writer rules keep the LRU honest:

- `seed_inventory` seeds without touching `last_loaded` (never-used tools sort oldest);
- **every successful `registry.execute` bumps `last_loaded`** (`CatalogStore.touch`), so a tool used this turn is never the next victim. Eviction is main-owner only: subagents inherit the curator's budget and never squeeze it.

Evicted tools stay registered but leave the injection snapshot, and an execution attempt gets the "not loaded — use tool_search + tool_load" hint (the registry's A4 gate).

---

## 6 · External MCP / REST-API integration

Third-party capability enters only as a standard MCP server in `tools.json5`'s `mcp`/`rest-api` sections, connected by the internal **mcp-gateway** plugin (one `MCPServerConnection` per server, in its pool). A REST API is one `mcp-openapi-proxy`-backed server, so it is a normal gateway server with `source.type == "rest_api"` — the catalog distinguishes the two categories from that provenance.

### The reconcile (`_sync_mcp_proxies`)

Driven by connect events / `mcp_*` mutations / `tools/list_changed`:

1. **Mirror** `server` rows from the wrapper's live `__check` (runtime/enabled/error/source);
2. **auto_load servers** — register their proxies (full diff) and upsert tool rows with `loaded=True` (one bulk registration keeps the old wholesale behavior for servers opted in via `autoload: true`);
3. **on-demand servers** (the default) — upsert their tool rows with `loaded=False` (**no proxies**): this is what makes `tool_search` find their tools and `tool_load` materialize them one at a time. Only connected servers yield rows (`mcp_list_tools` answers empty otherwise); disconnected servers' rows stay and the effective-status join hides them;
4. **drop** registered proxies whose server left the config (`mcp_remove` is the only unregister path — disable keeps the proxy, the join hides it).

### Crash survival

When the wrapper child dies, the watchdog's `mark_all_servers_down` rewrites every `server.runtime` to DISCONNECTED so their tools stop injecting immediately. The `last_runtime` snapshot taken at session start is untouched, so the **restarted** wrapper eagerly reconnects exactly the previously-connected set — the crash does not defeat eager-connect.

---

## 7 · Consistency & concurrency

- **One source of truth.** The catalog db is the single registry; the agent and every subagent maintain no second in-memory copy (subagents read the same file, write-owner gates apply to mutations). `tools.json5` is the authority for *configuration*; the catalog is the authority for *state*.
- **Config writes are atomic + cross-process locked.** `write_config` writes a temp file + `os.replace`; the read→mutate→write window around it (model switches, embeddings-config edits, cli/rest-api persistence) is wrapped in a cross-process `filelock` (`config_read_modify_write`) so two processes (host + memdb child) can never clobber each other's change. The lock wait is bounded (`storage.filelock`).
- **Rebuilds are live, not offline.** The catalog is synced from the live wrapper on every (re)connect; a schema change drops the stale embedding row and the drainer re-embeds. There is no offline rebuild step.

---

## 8 · Component map

| Module | Responsibility |
|---|---|
| `slife/tools/catalog.py` | `CatalogStore`: SQL, schema, FTS5 + semantic KNN, effective-status join, `evict_lru`, `touch`, `session_start` / `mark_all_servers_down` |
| `slife/tools/catalog_service.py` | `ToolCatalogService`: policy — seeding, `snapshot_loaded`, `load_tool`/`unload_tool` refusal matrix, `evict_to_threshold`, `server_category`, `sync_config_servers` |
| `slife/tools/catalog_search.py` | hybrid RRF merge + score annotator (thin adapter over `memdb.search`) |
| `slife/tools/registry.py` | the execution pool; consults the catalog for unloaded-refusal hints; bumps `last_loaded` on successful execute |
| `slife/tools/factory.py` | auto-discovery (gated to `slife.tools` modules) + `enabled`-override filtering |
| `slife/tools/meta_tools.py` | `tool_search`, `tool_load`, `_unload_function_tool`, `skill_load` |
| `slife/tools/whitelist.py` | harness pair + 11 meta tools (never evicted / not unloadable) |
| `slife/tools/_config_io.py` | json5 read/write, atomic replace, cross-process `config_read_modify_write` lock |
| `slife/plugins/mcp_gateway/*` | the server pool, boot eager-connect, `mcp_list`/`__check`/`mcp_list_tools`, `mcp_set`/`connect/disconnect`/`search`, the merged config view |
| `slife/agent/loop.py` | per-turn snapshot + injection from the catalog; boundary eviction (`_maybe_evict`) |
| `slife/agent/service.py` | `_init_catalog`, `_sync_mcp_proxies` reconcile, `_sync_catalog_from_config`, `_mirror_plugin_tools_catalog`, the on-demand row mirror |

---

## 9 · Open items & notes

Folded down from the DESIGNER_NOTES §8.5 checkout list; implemented/deferred as noted:

- **tools.db = the single registry** — done (agent + subagents read the same file; no per-agent copies).
- **Injected schema comes from the db** (`schema` column), never re-read live — done; `_function_from_schema` uses the registry key so external names stay correct.
- **`cli_set` / `skill_set` / `rest_api_set` / `job-write/remove` mutate tools.db + clean up (server removal tears down the connection)** — done for rows/proxies; `mcp_remove` is the only server teardown path.
- **"Whitelist means?"** — resolved: the always-loaded carve-outs of `whitelist.py` (harness pair + meta surface), a design constant, not configurable.
- **Server auto-disconnect when its last loaded tool is evicted** — *deferred*: eviction today keeps the server connected (its tools are still searchable/loadable for free). Could reconnect by a simple `tool_load`; re-arming on `tool_load` would make server eviction safe.
- **`watchdog` / plugin process contract** — lives in [PLUGIN_CONTRACT.md](PLUGIN_CONTRACT.md), not here.