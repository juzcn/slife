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

- **Unified search + load.** `tool_search` spans every category in the catalog (the six `tools.json5` sections — with `cli` having no rows; §3); `tool_load` loads any function tool by name.
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
description -- used by search
category    -- builtin | job | mcp | rest-api | skill | cli
source_id   -- owning server name for mcp/rest-api, else NULL
schema      -- the tool def {name, description, inputSchema} for function tools;
               the loaded SKILL.md text for skill rows  (no cli rows exist)
enabled     -- config disable mirror (mcp/rest-api pass NULL → join governs)
status      -- loaded | unloaded for FUNCTION_CATEGORIES; NULL for skill/cli (no state)
last_loaded -- ISO timestamp, bumped on tool_load and on every successful execute (LRU key)
```

**The `schema` column is the strict tool def — and it is exactly what gets injected.** Its shape is fixed to `{name, description, inputSchema}`:

- `inputSchema` is a plain JSON Schema (per-parameter type + description + `required`). Nothing custom is ever placed *inside* it — a non-standard key would make the schema invalid for the tool-calling wire format.
- No other keys. The source's declared *output* schema (an MCP server's `outputSchema`, fastmcp's `output_schema`) is deliberately **dropped**: the OpenAI function definition has no return slot, and a `-> str` wrapper yields only a meaningless `{result: string}` artifact. Return information belongs in `description` — where native tools already state it ("Run a shell command. Returns stdout + stderr.") and where a plugin tool's docstring summary lands (note: fastmcp *discards* a `Returns:` docstring section, so it must be in the summary to survive).
- It is **never docstring text** — plugin tools enter the catalog as the JSON schema fastmcp derived from their signature/docstring, not as the raw docstring.

`_function_from_schema` re-serializes this column into the OpenAI function definition (`parameters` ← `inputSchema`), so the stored schema and the wire schema are one and the same — a single `descriptor_json` builder writes it at every site (seed, external mirror, plugin mirror).

Only `builtin | job | mcp | rest-api` participate in the load state (the §8.5 status rule: "loaded unloaded only applies to function tools; skill and cli stay null"):

- **skill** — a row is written by `skill_load` (content = the loaded SKILL.md), never carrying a load state. Skills are otherwise managed as files in the skills dir and enumerated live by `skill_list`.
- **cli** — **no catalog row exists**: CLI entries live in `tools.json5`'s `cli` section (plus the live `Config.cli_tools` snapshot) and are enumerated by `cli_list`. The `cli` value is accepted by the `category` CHECK but nothing writes it, so `tool_search(category='cli')` has nothing to return today — CLI is a config surface with no load state, not a catalog category.

### `tool_embeddings`

Chunked vector rows for semantic search (one tool → several chunks; closest-chunk aggregation). The drainer re-embeds on schema change; stale-width vectors are skipped at query time.

**Chunking is the one shared chunker — not a catalog-specific one.** The catalog's `SemanticManager` (`slife/tools/semantic.py`) subclasses memdb's and inherits `_embed_doc`, so a tool schema is embedded through the *same* path as a memdb turn or a memfiles doc: `_chunk_text` splits on paragraph boundaries (`CHUNK_SIZE_CHARS`, one line of overlap), then `_split_chunks_to_token_limit` hard-splits anything still over the model's `max_tokens` at a **1 char/token floor**, and each chunk is embedded in its own request. That floor exists for exactly this shape: a large schema flattens to a long, newline-free, escape-dense params line that tokenizes at 1–2 chars/token — without the split it rode as one chunk, the provider rejected it (bge-m3's 8192 cap), and the tool stayed unembedded forever with the semantic gate locked off. A small schema simply yields a single chunk.

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

### Plugin & job tool rows

The built-in plugins' own tools (job-coding's per-job tools, the other plugins' bare-name tools) are mirrored into the catalog when the plugin connects and again on every runtime tool-set change (a `job-write` / `job-remove` → `tools/list_changed` → the host's `_rescan_plugin_tools`). The mirror is **upsert + purge**: `_mirror_plugin_tools_catalog` writes the current rows (`job` category for job-coding, `builtin` for the rest) and `_purge_plugin_tool_rows` deletes the rows of tools that vanished (`old_names - new_names` via `CatalogStore.remove_tool`) — so a removed job cannot linger as a stale row that `tool_search` keeps returning.

---

## 6 · External MCP / REST-API integration

Third-party capability enters only as a standard MCP server in `tools.json5`'s `mcp`/`rest-api` sections, connected by the internal **mcp-gateway** plugin (one `MCPServerConnection` per server, in its pool). A REST API is one `mcp-openapi-proxy`-backed server, so it is a normal gateway server with `source.type == "rest_api"` — the catalog distinguishes the two categories from that provenance.

### The reconcile (`_sync_mcp_proxies`)

Driven by connect events / `mcp_*` mutations / `tools/list_changed`:

1. **Mirror** `server` rows from the wrapper's live `__check` (runtime/enabled/error/source);
2. **auto_load servers** — register their proxies (full diff) and upsert tool rows with `loaded=True` (one bulk registration keeps the old wholesale behavior for servers opted in via `autoload: true`);
3. **on-demand servers** (the default) — upsert their tool rows with `loaded=False` (**no proxies**): this is what makes `tool_search` find their tools and `tool_load` materialize them one at a time. Only connected servers yield rows (`mcp_list_tools` answers empty otherwise); disconnected servers' rows stay and the effective-status join hides them;
4. **drop** registered proxies whose server left the config (`mcp_remove` is the only unregister path — disable keeps the proxy, the join hides it);
5. **purge the removed server's catalog rows.** A server that left **both** the live pool and `tools.json5` loses its `server` row (which cascades its `tool` rows) on the next reconcile — so `mcp_remove` / `rest_api_remove` clean up *live*, not merely at the next boot's `sync_config_servers` purge. The comparison is against the **config**, not the pool: a transient empty pool (a gateway restart) must never wipe a still-configured server's rows.

Removal is the only deletion path — a merely disabled or disconnected server keeps its rows, and the effective-status join expresses the difference.

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
| `slife/tools/catalog.py` | `CatalogStore`: SQL, schema, FTS5 + semantic KNN, effective-status join, `evict_lru`, `touch`, `remove_server`/`remove_tool`, `session_start` / `mark_all_servers_down` |
| `slife/tools/catalog_service.py` | `ToolCatalogService`: policy — seeding, `snapshot_loaded`, `load_tool`/`unload_tool` refusal matrix, `evict_to_threshold`, `server_category`, `sync_config_servers`; `descriptor_json`/`tool_descriptor` (the one tool-def builder) |
| `slife/tools/catalog_search.py` | hybrid RRF merge + score annotator (thin adapter over `memdb.search`) |
| `slife/tools/registry.py` | the execution pool; consults the catalog for unloaded-refusal hints; bumps `last_loaded` on successful execute |
| `slife/tools/factory.py` | auto-discovery (gated to `slife.tools` modules) + `enabled`-override filtering |
| `slife/tools/meta_tools.py` | `tool_search`, `tool_load`, `_unload_function_tool`, `skill_load` |
| `slife/tools/whitelist.py` | harness pair + 11 meta tools (never evicted / not unloadable) |
| `slife/tools/_config_io.py` | json5 read/write, atomic replace, cross-process `config_read_modify_write` lock |
| `slife/plugins/mcp_gateway/*` | the server pool, boot eager-connect, `mcp_list`/`__check`/`mcp_list_tools`, `mcp_set`/`connect/disconnect`/`search`, the merged config view |
| `slife/agent/loop.py` | per-turn snapshot + injection from the catalog; boundary eviction (`_maybe_evict`) |
| `slife/agent/service.py` | `_init_catalog`, `_sync_mcp_proxies` reconcile (incl. the live removal purge), `_sync_catalog_from_config`, `_mirror_plugin_tools_catalog` + `_purge_plugin_tool_rows`, the on-demand row mirror |

---

## 9 · Open items & notes

Folded down from the DESIGNER_NOTES §8.5 checkout list; implemented/deferred as noted:

- **tools.db = the single registry** — done (agent + subagents read the same file; no per-agent copies).
- **Injected schema comes from the db** (`schema` column), never re-read live — done; the stored shape is the strict tool def (§3) and `_function_from_schema` uses the registry key so external names stay correct.
- **`cli_set` / `skill_set` / `rest_api_set` / `job-write/remove` mutate tools.db + clean up** — done:
  - **mcp / rest-api**: `*_set` persists + connects, `*_set_enabled` reconnects/disconnects immediately, `*_remove` tears the connection down (`_pool.remove_server`) **and** the next reconcile purges the server's catalog rows live (step 5) — no stale rows until restart;
  - **job / plugin tools**: registered/unregistered by the plugin, mirrored into the catalog on connect/rescan, and a vanished tool's row is purged (`remove_tool`) — a removed job does not linger in `tool_search`;
  - **cli**: mutations update `tools.json5` (+ the live `Config.cli_tools` snapshot) only — CLI has **no load state** (per the §8.5 rule), so there is no catalog row to sync; discovery is `cli_list` (live);
  - **skill**: `skill_set`/`skill_remove` manage the skills dir; a catalog row exists per `skill_load` (skills have no load state either). Discovery is `skill_list` (live).
- **"Whitelist means?"** — resolved: the always-loaded carve-outs of `whitelist.py` (harness pair + meta surface), a design constant, not configurable.
- **Server auto-disconnect when its last loaded tool is evicted** — *deferred*: eviction today keeps the server connected (its tools are still searchable/loadable for free). Could reconnect by a simple `tool_load`; re-arming on `tool_load` would make server eviction safe.
- **`watchdog` / plugin process contract** — lives in [PLUGIN_CONTRACT.md](PLUGIN_CONTRACT.md), not here.