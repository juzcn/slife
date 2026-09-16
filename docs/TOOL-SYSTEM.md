# The Unified Tool System

> Authoritative design of Slife's tool catalog (the "Tool System" — DESIGNER_NOTES §8.5). Covers the six-category `tools.json5` model, the shared `tools.db` catalog, the load/unload threshold, the search surface, the per-turn injection chain, and MCP/REST-API integration. Reader: a developer working on tool discovery, loading, eviction, or the mcp-gateway reconcile. The everyday tool inventory lives in the [README](../README.md#tools); the plugin-side server contract in [PLUGIN_CONTRACT.md](PLUGIN_CONTRACT.md).

---

## 1 · Why a unified system

Before the rework: `tool_search` could only find MCP tools; every MCP server was loaded or unloaded **as a whole**; native tools always flooded the tool list. Three symptoms (§8.5):

- search could not address the other tool kinds (builtin / job / rest-api / skill / cli);
- every configured server was connected eagerly AND all of its tools injected, so connect cost and context cost both scaled with the config rather than with use;
- loading was not granular — one MCP server's dozens of tools all entered the context at once, blowing up the schema box the model must read every turn.

The goal is one unified model: **every function tool** (builtin, job, mcp, rest-api) is a row in one shared catalog with a **loaded / unloaded** state; the LLM discovers with one `tool_search`, loads with one `func-tool-load`, and a **threshold** manages how many loaded tools fit in the context. The catalog (`tools.db`) is the single source of truth — with one schema per tool that the injection chain reads directly.

Key properties:

- **Unified search + load.** `tool_search` spans every category in the catalog (all six `tools.json5` sections); `func-tool-load` loads any function tool by name.
- **Threshold-managed.** A configurable cap (`tool_load.threshold`, default 100) bounds how many function tools are injected; the harness evicts the oldest-by-usage at turn boundaries. Never evicted — and the only things injected before the model asks: the whitelist (harness pair + 5 meta tools + 2 pinned) and anything marked `autoload` in `tools.json5`.
- **Granular.** Load/unload is per-tool, not per-server. A connected MCP server with 50 tools injects only the ones the model loaded.
- **DB-driven injection.** The schema injected into the LLM comes from the catalog's `schema` column — never re-fetched from the live MCP server or parsed from tool code.

---

## 2 · Configuration — `tools.json5`

The tool system is configured entirely in `tools.json5` (sibling of `slife.json5` in the data dir). It carries **one section per category** plus the **`tool_load`** policy section:

```
builtin:   [{name, enabled, autoload, ...overrides}]
mcp:       {servers: {<name>: {command|url, args, env, enabled, autoload, source, ...}}}
rest-api:  {<name>: {spec_url, base_url, api_key, enabled, autoload, ...}}   # OpenAPI via mcp-openapi-proxy
job:       [{name, enabled, autoload}]                          # jobs are files in jobs/
cli:       {<name>: {command, description, enabled, autoload}}  # cli_list enumerates
skill:     [{name, enabled, autoload}]

tool_load: {threshold: 100}
```

- Sections are the *only* knobs; the `tools:` array of `slife.json5` is retired. `tools.json5` is **the authority** — at startup the host mirrors its `mcp`/`rest-api` sections into the catalog db (`sync_config_servers`), and every `cli_set` / `rest_api_set` / `job-write` / etc. persists there and re-syncs.
- Every entry carries the same two policy flags, siblings of each other: **`enabled`** (false = off, mirrored onto the row's `enabled` column and reported by search as `disabled`) and **`autoload`** (true = injected from session start, never evicted). `autoload` is per *tool* wherever a tool has a name of its own (`builtin` / `job`), and per *server* in `mcp` / `rest-api` — an external tool's name is not knowable before its server connects, so the flag covers the server's whole tool set. In `skill` / `cli` it is accepted and inert: those rows have no load state to seed.
- The gateway reads the **merged server view**: `mcp.servers` ∪ `rest-api`, with a legacy top-level `servers` fallback so a pre-section file keeps working (`_servers_dict`).

---

## 3 · The shared catalog — `tools.db`

`SLIFE_TOOLS_DB` (dev default: `<cwd>/tools.db`; tests point it at a per-test temp file). SQLite with WAL, cross-process safe; main agent, subagents and the mcp-gateway child all read the **same file**. One store class (`CatalogStore`) owns all SQL; policy (thresholds, refusal texts, owner checks) lives in `ToolCatalogService`.

### There is no `server` table

Which servers to bring up is decided by `tools.json5` (`enabled`), what is live
right now is answered by the gateway's pool (`mcp_list` / `__check`), and this
db records the RESULT on the tool rows.  A server table would be a third copy
of facts that already have owners — and one that goes stale the moment the
gateway child dies.

The connectivity verdict therefore lives in `tool.status`:

```
status = 'loaded'   -- injected (the model loaded it, or the session seeded it)
       | 'unloaded' -- registered and available, not injected
       | 'error'    -- its server is unusable (not up yet, disconnected,
                       connect failed, or the gateway child died)
       | NULL       -- skill/cli rows: no load concept at all
```

Every transition is written by the HOST as it reconciles:

| event | action |
|---|---|
| catalog init (before any server is up) | every external row → `error` |
| a server connects (or reconnects) | mirror its tool rows — new rows `unloaded`, existing `error` rows reset to `unloaded` |
| a server is down / failed / disabled | that server's rows → `error` |
| the gateway child dies | every `mcp`/`rest-api` row → `error` |
| a server leaves `tools.json5` | its rows are purged |

### `tool` — one row per tool

```
name        -- unique; external tools are "{server}__{tool}", everything else bare
description -- used by search
category    -- builtin | job | mcp | rest-api | skill | cli  (where it came from)
type        -- func | skill | cli  (what kind of thing it is; derived from category)
source_id   -- owning server name for mcp/rest-api, else NULL
schema      -- the tool def {name, description, inputSchema} for func rows;
               the SKILL.md text for skill rows; NULL for cli rows
enabled     -- config disable mirror for LOCAL categories; NULL for mcp/rest-api
               (their availability is the row's own status, not a config flag)
status      -- loaded | unloaded | error for type='func'; NULL for skill/cli
last_loaded -- ISO timestamp, bumped on func-tool-load and on every successful execute (LRU key)
```

`category` and `type` answer different questions: the category is the tool's *provenance* (a builtin module, a job file, an external server, the skills dir, a `tools.json5` cli entry — one per `tools.json5` section), the type is its *kind*. Only `func` has a load state, so `type` is what the load/unload rules read; it is **derived from `category` at write time** in one place (`type_for_category`, used by `CatalogStore.upsert_tool`), so the two columns cannot drift. A db written by an older schema is ALTERed and backfilled at boot (`_migrate`).

**The `schema` column is the row's documentation — what the model reads and what search indexes.** For a func row it is exactly the tool def, and it is exactly what gets injected; its shape is fixed to `{name, description, inputSchema}`:

- `inputSchema` is a plain JSON Schema (per-parameter type + description + `required`). Nothing custom is ever placed *inside* it — a non-standard key would make the schema invalid for the tool-calling wire format.
- No other keys. The source's declared *output* schema (an MCP server's `outputSchema`, fastmcp's `output_schema`) is deliberately **dropped**: the OpenAI function definition has no return slot, and a `-> str` wrapper yields only a meaningless `{result: string}` artifact. Return information belongs in `description` — where native tools already state it ("Run a shell command. Returns stdout + stderr.") and where a plugin tool's docstring summary lands (note: fastmcp *discards* a `Returns:` docstring section, so it must be in the summary to survive).
- It is **never docstring text** — plugin tools enter the catalog as the JSON schema fastmcp derived from their signature/docstring, not as the raw docstring.

`_function_from_schema` re-serializes this column into the OpenAI function definition (`parameters` ← `inputSchema`), so the stored schema and the wire schema are one and the same — a single `descriptor_json` builder writes it at every site (seed, external mirror, plugin mirror).

Only `type='func'` (builtin | job | mcp | rest-api) participates in the load state (the §8.5 status rule: "loaded unloaded only applies to function tools; skill and cli stay null"). The other two types are rows all the same — being in the catalog is what makes them findable by `tool_search`, which is the whole point of one catalog:

- **skill** — one row per skill directory, `status` NULL. The `schema` is the SKILL.md verbatim: a playbook *is* its documentation, so that text is what search indexes (keyword via FTS, semantic via the drainer) and `skill_use` returns it to the model. Rows are mirrored from the skills dir at boot and after every `skill_set` / `skill_remove` / `skill_set_enabled` (`skill_catalog_rows` → `ToolCatalogService.sync_category`); `enabled` mirrors the `skills:` config disable.
- **cli** — one row per `tools.json5` `cli` entry, `status` NULL, `schema` NULL (a CLI has no tool def — the description is what identifies it, and `cli_list` carries the command/install detail). Mirrored from the `cli` section at boot and after every `cli_set` / `cli_remove` / `cli_set_enabled`.

Both mirrors are **upsert + purge**: a skill removed from disk or a CLI removed from the config loses its row in the same pass, so `tool_search` never returns a hit whose source is gone.

### `tool_embeddings`

Chunked vector rows for semantic search (one tool → several chunks; closest-chunk aggregation). The drainer re-embeds on schema change; stale-width vectors are skipped at query time.

**Chunking is the one shared chunker — not a catalog-specific one.** The catalog's `SemanticManager` (`slife/tools/semantic.py`) subclasses memdb's and inherits `_embed_doc`, so a tool schema is embedded through the *same* path as a memdb turn or a memfiles doc: `_chunk_text` splits on paragraph boundaries (`CHUNK_SIZE_CHARS`, one line of overlap), then `_split_chunks_to_token_limit` hard-splits anything still over the model's `max_tokens` at a **1 char/token floor**, and each chunk is embedded in its own request. That floor exists for exactly this shape: a large schema flattens to a long, newline-free, escape-dense params line that tokenizes at 1–2 chars/token — without the split it rode as one chunk, the provider rejected it (bge-m3's 8192 cap), and the tool stayed unembedded forever with the semantic gate locked off. A small schema simply yields a single chunk.

### Effective status

A tool's *effective* status is the raw `tool.status` — there is nothing to join:

| condition | effective |
|---|---|
| function tool, `status='loaded'`, not config-disabled | `loaded` |
| function tool, `status='unloaded'` | `unloaded` |
| function tool, `status='error'` (its server is unusable) | `error` |
| local tool disabled in config (`enabled = 0`) | `disabled` |
| skill / cli (no state) | `n/a` |

One state per fact, no precedence rules: "what the model asked for" is
`loaded`/`unloaded`, "the server is not usable" is `error`, and "the config
turned it off" is `disabled`.  Injection takes exactly the `loaded` rows.

---

## 4 · Discovery & load — the meta surface

The whitelist (`slife/tools/whitelist.py`) protects three always-injected families, never LRU-evictable and not unloadable:

- **harness pair** — `_turn_prompt`, `_check_new_input`, `attach_image` (the loop's mechanism);
- **the tool-system meta tools (5)** — `mcp_set_enabled`, `rest_api_set_enabled`, `tool_search`, `func-tool-load`, `_unload_func_tool`.  There is no connect/disconnect pair: the modern protocol has no session to open or close, so each external family has ONE lifecycle switch (`*_set_enabled`) — enabling (re)connects, and a tool call reconnects lazily if the server died in the meantime.  There is no server-level search either: with the registry gone, `tool_search` (tool-level, with category and status filters) is the discovery surface.
- **pinned (2)** — `skill_use` and `system_health`: not tool-system machinery, but the two calls every session reaches for (read a skill's SKILL.md; one health report over every subsystem check).  Pinned so the squeeze can never cost a search+load round trip to re-learn them.

Together: `ALWAYS_LOADED`. Everything else seeds loaded and can be reloaded, but the squeeze may evict it.

### `tool_search`

Search the whole catalog:

```
query    -- free text over name/description/schema
category -- builtin|job|mcp|rest-api|skill|cli; empty = all
status   -- all|loaded|unloaded|disabled|unavailable; default all
mode     -- hybrid (default) | keyword | grep
```

Three retrieval routes, one row shape (the effective status is computed per row — nothing to join):

- `grep` — exact substring over name/description/category/schema;
- `keyword` — FTS5 BM25, CJK-routed to a LIKE multi-word AND fallback;
- `hybrid` — keyword + semantic KNN (sleepy `tool_embeddings`, closest-chunk per tool) merged by RRF.

### `func-tool-load`

Loads a function tool by **full name** (`{server}__{tool}` for external). Refusals come from the effective status: unknown → "see tool_search"; disabled → "enable it first"; error → "its server is not up right now — check it with mcp_list, then retry". On success the row's status flips `loaded` and the injection snapshot is refreshed. For `mcp`/`rest-api` rows it also **materializes the execution proxy** from the row's schema descriptor (`create_proxy_tools` → registered in the registry) — loading and materialization are the same step, driven by the row.

### `_unload_func_tool`

Self-service unload (the spare-ticket the model can use to free a slot); refused for the whitelist — `skill_use` and `system_health` included. The harness's eviction is the same operation run automatically.

---

## 5 · The load/unload lifecycle

### Boot ordering

1. Host `_init_catalog` opens `tools.db` and runs `seed_inventory`: every registered tool gets a row, and **a NEW row is `loaded` only from the two autoload sources** — the whitelist (`ALWAYS_LOADED`: the loop's own tools, a system-level protection that is not configurable) and the entries marked `autoload: true` in `tools.json5` (user intent) — everything else is born `unloaded`. An EXISTING row keeps whatever the model decided; the sync mirrors *which* tools are registered, never *what is loaded*. The two registry-less categories are mirrored in the same pass (`_mirror_local_rows`: the skills dir → `skill` rows, the `cli` section → `cli` rows). Every external row is then marked `error`, because no server is up yet.
2. The wrapper spawns; `_auto_connect_configured` connects **every server enabled in `tools.json5`** (disabled ones are registered only, so `mcp_list` still matches the config). Nothing is remembered from a previous session — a server that was down when you quit is retried here like any other.
3. Each successful connect publishes `tools/list_changed`; the host's listen stream wakes the reconcile below, which mirrors that server's tool rows (new rows `unloaded`) and clears the `error` mark. `_wire_mcp_glue` also runs one reconcile deterministically the moment the gateway is ready, so startup converges as servers come up — one that never comes up simply stays `error`.
4. `tools.json5` removals are purged in the same pass (config is the authority).

### Per-turn injection

At each turn boundary the loop refreshes a **snapshot**: `snapshot_loaded()` = the rows with `status='loaded'` (and not config-disabled) ∪ `ALWAYS_LOADED`. The function list for the request is built from `rows_for_names(snapshot)` — the catalog `schema` column — via `_function_from_schema`. The registry key (full `{server}__{tool}` name) **always wins** over the descriptor's bare name, so the injected name is exactly what `registry.execute` resolves. A row missing its schema falls back to the materialized instance; no catalog → the whole registry (historical behavior).

### Threshold eviction

At the turn boundary, if loaded count exceeds `tool_load.threshold` (default 100), `evict_to_threshold` drops the excess via `evict_lru` — ordered `(last_loaded IS NULL) DESC, last_loaded ASC, name`, i.e. never-used first, then least-recently-used. Protected from eviction: the same two autoload sources that seed a row `loaded` — `ALWAYS_LOADED` and the `autoload` entries (an autoloaded server contributes every name it owns, resolved by `source_id`). Two writer rules keep the LRU honest:

- `seed_inventory` seeds without touching `last_loaded` (never-used tools sort oldest);
- **every successful `registry.execute` bumps `last_loaded`** (`CatalogStore.touch`), so a tool used this turn is never the next victim. Eviction is main-owner only: subagents inherit the curator's budget and never squeeze it.

Evicted tools stay registered but leave the injection snapshot, and an execution attempt gets the "not loaded — use tool_search + func-tool-load" hint (the registry's A4 gate).

### Plugin & job tool rows

The built-in plugins' own tools (job-coding's per-job tools, the other plugins' bare-name tools) are mirrored into the catalog when the plugin connects and again on every runtime tool-set change (a `job-write` / `job-remove` → `tools/list_changed` → the host's `_rescan_plugin_tools`). The mirror is **upsert + purge**: `_mirror_plugin_tools_catalog` writes the current rows (`job` category for job-coding, `builtin` for the rest) and `_purge_plugin_tool_rows` deletes the rows of tools that vanished (`old_names - new_names` via `CatalogStore.remove_tool`) — so a removed job cannot linger as a stale row that `tool_search` keeps returning.

---

## 6 · External MCP / REST-API integration

Third-party capability enters only as a standard MCP server in `tools.json5`'s `mcp`/`rest-api` sections, connected by the internal **mcp-gateway** plugin (one `MCPServerConnection` per server, in its pool). A REST API is one `mcp-openapi-proxy`-backed server, so it is a normal gateway server with `source.type == "rest_api"` — the catalog distinguishes the two categories from that provenance.

### Protocol era (2026-07-28) and change notifications

Every link negotiates its peer's protocol era at connect time and never
configures it (`slife/mcp/era.py`, driving the SDK's `mode="auto"` policy): a
peer that answers `server/discover` is adopted **modern** (no session, no
handshake, per-request `_meta`), one that answers as legacy keeps the
`initialize` handshake.  So a mixed fleet works unchanged — including the
external servers in `tools.json5`, which drift to the modern protocol on
their own schedule.

The era decides how a change reaches us.  The modern revision removed the
connection-scoped channel: `notifications/tools/list_changed` arrives ONLY on
a `subscriptions/listen` stream the client asked for, and the SDK drops a bare
`ServerSession.send_tool_list_changed()` outright at that era.  Hence:

- **servers we own** (plugin children, and the host server for subagents)
  publish on the server's `SubscriptionBus` (`ToolsChangedNotifier`, replacing
  the retired session-set notifier) and the consuming side keeps one stream
  open per link (`watch_tools_changed`, re-listening after a drop).  The
  harness-side trigger is unchanged: the listen event is funneled into the
  same `on_notification` handler, so `_rescan_plugin_tools` /
  `_sync_mcp_proxies` run exactly as before;
- **a modern external server** gets a per-server listen stream inside the
  gateway wrapper; its cache refresh still rides `on_connected`;
- **a legacy peer** keeps the session channel (`message_handler` → the same
  handler).

Our plugin servers therefore run in **SSE mode** (`json_response=False`): a
listen stream IS a response stream, and a single JSON body per POST has
nowhere to carry one.

### The reconcile (`_sync_mcp_proxies`)

Driven by connect events / `mcp_*` mutations / `tools/list_changed`:

1. **Project the connectivity verdict**: read the wrapper's live `__check`; a server that is `connected` has its `error` marks cleared, every other configured server (down, failed, or disabled) has its tools marked `error`.  A failed probe is *not* a verdict — the rows are left untouched rather than marking every server broken;
2. **auto_load servers** — register their proxies (full diff) and mirror their tool rows (new rows `unloaded`, or `loaded` when the server is marked `autoload`);
3. **on-demand servers** (the default) — mirror their tool rows with **no proxies**: this is what makes `tool_search` find their tools and `func-tool-load` materialize them one at a time.  Only connected servers yield rows (`mcp_list_tools` answers empty otherwise); a disconnected server's rows stay and its `error` mark keeps them out of injection;
4. **drop** registered proxies whose server left the config (`mcp_remove` is the only unregister path — a merely disabled server keeps its proxy, and its rows are marked `error`);
5. **purge the removed server's catalog rows.** Comparing against **tools.json5** (the authority) rather than the pool keeps a transient empty pool — a gateway restart — from wiping a still-configured server's rows.

Registering a tool never loads it: every new row lands `unloaded` and only `func-tool-load` puts it into the injection set.

### Crash survival

When the wrapper child dies, `on_plugin_child_exit` marks **every `mcp`/`rest-api` tool `error`** so none of them keeps injecting a dead transport. The restarted wrapper connects every enabled server again (boot step 2) and the reconcile clears each mark as its server comes up — a crash costs one reconnect, not the session's toolset.

---

## 7 · Consistency & concurrency

- **One source of truth.** The catalog db is the single registry; the agent and every subagent maintain no second in-memory copy (subagents read the same file, write-owner gates apply to mutations). `tools.json5` is the authority for *configuration*; the catalog is the authority for *state*.
- **Config writes are atomic + cross-process locked.** `write_config` writes a temp file + `os.replace`; the read→mutate→write window around it (model switches, embeddings-config edits, cli/rest-api persistence) is wrapped in a cross-process `filelock` (`config_read_modify_write`) so two processes (host + memdb child) can never clobber each other's change. The lock wait is bounded (`storage.filelock`).
- **Rebuilds are live, not offline.** The catalog is synced from the live wrapper on every (re)connect; a schema change drops the stale embedding row and the drainer re-embeds. There is no offline rebuild step.

---

## 8 · Component map

| Module | Responsibility |
|---|---|
| `slife/tools/catalog.py` | `CatalogStore`: SQL, schema (v3 — `type`, no `server` table), FTS5 + semantic KNN, effective status, `evict_lru`, `touch`, `purge_source`/`remove_tool`/`names_by_category`, `mark_source_error` / `mark_all_external_error` / `reset_source_status` |
| `slife/tools/catalog_service.py` | `ToolCatalogService`: policy — seeding, `snapshot_loaded`, `sync_category` (the skill/cli mirror), `load_tool`/`unload_tool` refusal matrix, `evict_to_threshold`, `server_category`, `sync_config_servers`; `descriptor_json`/`tool_descriptor` (the one tool-def builder) |
| `slife/tools/catalog_search.py` | hybrid RRF merge + score annotator (thin adapter over `memdb.search`) |
| `slife/tools/registry.py` | the execution pool; consults the catalog for unloaded-refusal hints; bumps `last_loaded` on successful execute |
| `slife/tools/factory.py` | auto-discovery (gated to `slife.tools` modules) + `enabled`-override filtering |
| `slife/tools/meta_tools.py` | `tool_search`, `func-tool-load`, `_unload_func_tool` |
| `slife/tools/whitelist.py` | harness pair + 5 meta tools + 2 pinned (`ALWAYS_LOADED` — never evicted / not unloadable) |
| `slife/tools/skill.py` | the Skills family + `skill_catalog_rows` / `sync_skill_catalog` (skills dir → `skill` rows) |
| `slife/tools/cli.py` | the CLI family + `cli_catalog_rows` / `sync_cli_catalog` (config section → `cli` rows) |
| `slife/tools/_config_io.py` | json5 read/write, atomic replace, cross-process `config_read_modify_write` lock |
| `slife/plugins/mcp_gateway/*` | the server pool, the boot pass that connects every enabled server, `mcp_list`/`__check`/`mcp_list_tools`, `mcp_set`/`mcp_set_enabled`/`mcp_remove`, the merged config view (it never touches `tools.db`) |
| `slife/agent/loop.py` | per-turn snapshot + injection from the catalog; boundary eviction (`_maybe_evict`) |
| `slife/agent/service.py` | `_init_catalog`, `_mirror_local_rows` (skill/cli rows at boot), `_sync_mcp_proxies` reconcile (incl. the live removal purge), `_sync_catalog_from_config`, `_mirror_plugin_tools_catalog` + `_purge_plugin_tool_rows`, the on-demand row mirror |

---

## 9 · Open items & notes

Folded down from the DESIGNER_NOTES §8.5 checkout list; implemented/deferred as noted:

- **tools.db = the single registry** — done (agent + subagents read the same file; no per-agent copies).
- **Injected schema comes from the db** (`schema` column), never re-read live — done; the stored shape is the strict tool def (§3) and `_function_from_schema` uses the registry key so external names stay correct.
- **`cli_set` / `skill_set` / `rest_api_set` / `job-write/remove` mutate tools.db + clean up** — done:
  - **mcp / rest-api**: `*_set` persists + connects, `*_set_enabled` reconnects/disconnects immediately, `*_remove` tears the connection down (`_pool.remove_server`) **and** the next reconcile purges the server's catalog rows live (step 5) — no stale rows until restart;
  - **job / plugin tools**: registered/unregistered by the plugin, mirrored into the catalog on connect/rescan, and a vanished tool's row is purged (`remove_tool`) — a removed job does not linger in `tool_search`;
  - **cli**: `cli_set` / `cli_remove` / `cli_set_enabled` rewrite `tools.json5` (+ the live `Config.cli_tools` snapshot) and re-mirror the `cli` rows right after (`sync_cli_catalog`) — a new CLI is findable by `tool_search` before the next restart, and a removed one loses its row. No load state (per the §8.5 rule); `cli_list` shows the command/install detail;
  - **skill**: `skill_set` / `skill_remove` / `skill_set_enabled` change the skills dir (or its config disable mirror) and re-mirror the `skill` rows right after (`sync_skill_catalog`). The row's `schema` is the SKILL.md — the skill is searchable by its own text; `skill_use` returns it, `skill_list` enumerates.
- **"Whitelist means?"** — resolved: the always-loaded carve-outs of `whitelist.py` (harness pair + meta surface + pinned `skill_use`/`system_health`), a design constant, not configurable.
- **Server auto-disconnect when its last loaded tool is evicted** — *deferred*: eviction today keeps the server connected (its tools are still searchable/loadable for free). Could reconnect by a simple `func-tool-load`; re-arming on `func-tool-load` would make server eviction safe.
- **`watchdog` / plugin process contract** — lives in [PLUGIN_CONTRACT.md](PLUGIN_CONTRACT.md), not here.