# The Unified Tool System

> Authoritative design of Slife's tool catalog (the "Tool System" — DESIGNER_NOTES §8.5). Covers the seven-category `tools.yaml` model, the shared `tools.db` catalog, the load/unload threshold, the search surface, the per-request injection chain, and MCP/REST-API integration. Reader: a developer working on tool discovery, loading, eviction, or the mcp-gateway reconcile. The everyday tool inventory lives in the [README](../README.md#tools); the plugin-side server contract in [PLUGIN_CONTRACT.md](PLUGIN_CONTRACT.md).

---

## 1 · Why a unified system

Before the rework: `tool_search` could only find MCP tools; every MCP server was loaded or unloaded **as a whole**; builtin tools always flooded the tool list. Three symptoms (§8.5):

- search could not address the other tool kinds (builtin / job / rest-api / skill / cli);
- every configured server was connected eagerly AND all of its tools injected, so connect cost and context cost both scaled with the config rather than with use;
- loading was not granular — one MCP server's dozens of tools all entered the context at once, blowing up the schema box the model must read every turn.

The goal is one unified model: **every function tool** (builtin, job, plugin, mcp, rest-api) is a row in one shared catalog with a **loaded / unloaded** state; the LLM discovers with one `tool_search`, loads with one `func_tool_load`, and a **threshold** manages how many loaded tools fit in the context. The catalog (`tools.db`) is the single source of truth — with one schema per tool that the injection chain reads directly.

Key properties:

- **Unified search + load.** `tool_search` spans every category in the catalog (every `tools.yaml` category section); `func_tool_load` loads any function tool by name.
- **Threshold-managed.** A configurable cap (`tool_load.threshold`, default 100) bounds how many function tools are injected; the harness evicts the oldest-by-usage at turn boundaries. Never evicted — and the only things injected before the model asks: the whitelist (3 harness tools + 5 meta tools + 2 pinned) and anything marked `autoload` in `tools.yaml`.
- **Granular.** Load/unload is per-tool, not per-server. A connected MCP server with 50 tools injects only the ones the model loaded.
- **DB-driven injection.** The schema injected into the LLM comes from the catalog's `schema` column — never re-fetched from the live MCP server or parsed from tool code.

### Vocabulary — system, job, external

Three families, by **who owns the tool** — the category column is the *provenance* inside its family:

| family | owner | categories | what it is |
|---|---|---|---|
| **system** | the developer | `builtin`, `plugin` | slife ships it: a module in `slife/tools/`, or a built-in plugin's own tool (memdb's `turn_recall`, the gateway's `mcp_set`, job-coding's `job-write`) |
| **job** | the user | `job` | code the user wrote themselves: a public function in `jobs/`, exposed by the job-coding plugin as `job-<function>` |
| **external** | a third party | `mcp`, `rest-api` | someone else's server, reached through the mcp-gateway (`{server}__{tool}`) |

`skill` and `cli` belong to none of the three families: nothing owns them (no server, no plugin — `source_id` is `n/a`), there is nothing to spawn and nothing to register, and the row is the whole of the thing (a playbook file, a `tools.yaml` entry).  They are **tools** all the same — *tool* is the broad word here, and the agent reaches them the way it reaches any other, through search and then by using them (a skill IS its SKILL.md, a cli row the command it names); they carry the same `enabled` switch, and the sync line's `total` counts them.  What they do not have is a runtime *instance* or a load state — that, and nothing else, is what `FUNCTION_CATEGORIES` splits on, so `func_tool_load` refuses them and the registry never holds one (§6 reads `total` off the rows for exactly this reason).

The split is what `SERVER_CATEGORIES` encodes on the code side (`mcp`/`rest-api` are the external ones — the only rows a gateway death marks `error`, the only sources the config purge owns), and it is why `system_tools_list` lists the system family only: a job is inventoried by `job-list`, and an external tool's schema already rides every request.

---

## 2 · Configuration — `tools.yaml`

The tool system is configured entirely in `tools.yaml` (sibling of `slife.yaml` in the data dir). It carries **one section per category** plus the **`tool_load`** policy section:

```
builtin:   [{name, enabled, autoload, ...overrides}]
plugin:    [{name, enabled, autoload}]                          # a built-in plugin's own tool
mcp:       {servers: {<name>: {command|url, args, env, enabled, autoload, source, ...}}}
rest-api:  {<name>: {command, args, env, enabled, autoload, source, ...}}   # OpenAPI via mcp-openapi-proxy
job:       [{name, enabled, autoload}]                          # jobs are files in jobs/ (tool: job-<name>)
cli:       {<name>: {command, description, enabled, autoload}}  # cli_list enumerates
skill:     [{name, enabled, autoload}]

tool_load: {threshold: 100}
```

- A `rest-api` entry has **exactly the same shape as an `mcp.servers` entry** — it *is* a standard MCP server (a `uvx mcp-openapi-proxy` instance) that lives in the other section. **The section is the whole fact**: an entry in `rest-api` is a REST API, one under `mcp.servers` is not, and nothing is tagged for it (`config.is_rest_api`). The OpenAPI settings ride the proxy's env, not config keys — `OPENAPI_SPEC_URL` / `SERVER_URL_OVERRIDE` / `API_KEY` (a `${VAR}` ref, resolved env → credstore). `rest_api_set` is the convenience wrapper that builds that entry; it is not a different format.
- Sections are the *only* knobs; the `tools:` array of `slife.yaml` is retired. `tools.yaml` is **the authority** — the host mirrors the external sections into the catalog db on every reconcile (each server's rows in ONE batched pass, plus a purge of the servers that left the file), and every `cli_set` / `rest_api_set` / `job-write` / etc. persists there and re-syncs.
- Every entry carries the same two policy flags, siblings of each other: **`enabled`** (false = off, mirrored onto the row's `status` column and reported by search as `disabled`) and **`autoload`** (true = injected from session start, never evicted). `autoload` is per *tool* wherever a tool has a name of its own (`builtin` / `job`), and per *server* in `mcp` / `rest-api` — an external tool's name is not knowable before its server connects, so the flag covers the server's whole tool set (there is no per-tool `autoload` for the external families). In `skill` / `cli` it is accepted and inert: those rows have no load state to seed. Unlike every other mirror decision, `autoload` also **overrides** an existing row's state — see §5.
- The gateway reads the **merged server view**: `mcp.servers` ∪ `rest-api`, with a legacy top-level `servers` fallback so a pre-section file keeps working (`_servers_dict`).

---

## 3 · The shared catalog — `tools.db`

`SLIFE_TOOLS_DB` (dev default: `<cwd>/tools.db`; tests point it at a per-test temp file). SQLite with WAL, cross-process safe; main agent, subagents and the mcp-gateway child all read the **same file**. One store class (`CatalogStore`) owns all SQL; policy (thresholds, refusal texts, owner checks) lives in `ToolCatalogService`.

### There is no `server` table

Which servers to bring up is decided by `tools.yaml` (`enabled`), what is live
right now is answered by the gateway's pool (`mcp_list` / `__check`), and this
db records the RESULT on the tool rows.  A server table would be a third copy
of facts that already have owners — and one that goes stale the moment the
gateway child dies.

Two questions land on the row, in two columns:

```
status      = 'enabled'  -- the ordinary state
            | 'disabled' -- tools.yaml switched it off: per SERVER for
            |               mcp/rest-api (all of its tools move together —
            |               there is no per-tool enable), per entry otherwise
            | 'error'    -- its owner is unusable right now.  Many causes —
                            not up yet, disconnected, connect failed, the
                            gateway child died, a SKILL.md that cannot be read,
                            … — and nothing in the code guesses which one

load_status = 'loaded'   -- injected (the model loaded it, or the session seeded it)
            | 'unloaded' -- registered and available, not injected
            | 'n/a'      -- skill/cli rows: no load concept at all
```

**One column, three exclusive values.** `enabled` / `disabled` / `error` are
one column because they answer one question — what state is this row in — and
they do not coexist: a row is in exactly one of them.  So an `error` row is not
an enabled row, a `disabled` row is never `error`, and `*_set_enabled` — the
`disabled` remedy — is not a remedy for `error`.

Two writers move that value, each owning one transition: config writes
`disabled ↔ enabled`, the runtime writes `enabled → error` / `error → enabled`,
and each transition is guarded (`_config_status_move`; see `mark_source_error` /
`mark_source_connected` / `set_source_enabled`).  So a server switched off while
it was down is `disabled` — off is not down, and the model must be able to tell
the two apart — and coming back up does not resurrect a tool the config switched
off.

`load_status` is the db's whole reason to exist, and no verdict is ever written
into it: writing the connectivity mark there destroyed the load state it landed
on, so a blip (or the boot sweep) reset every tool the model had loaded.  Every
transition is written by the HOST as it reconciles:

| event | action |
|---|---|
| catalog init (before any server is up) | every external row marked `error` — the persisted load state stands |
| a server connects (or reconnects) | mirror its tool rows — new rows `unloaded` (or `loaded` for an `autoload` entry); the `error` marks clear |
| a server is down / failed | that server's rows marked `error` |
| a server is switched off in `tools.yaml` | its rows keep everything they had; `status = disabled`, and it gets **no** `error` verdict (off is not down) |
| the gateway child dies | every `mcp`/`rest-api` row marked `error` |
| a skill's SKILL.md cannot be read | that row is mirrored `error`; reading it again after a fix puts it back to `enabled` |
| a server leaves `tools.yaml` | its rows are purged |
| a builtin leaves the code (deleted or renamed) | its row is purged by the boot seed — the family's membership is that seed's list (the registry + the `tools.yaml`-disabled builtins), so a tool the code dropped has no claimant left |

**A row leaves only by DELETION.** Every removal path is a `DELETE` of the row — `remove_tool` (one name), `remove_tools` (a set, one statement), `purge_source` (every row of a server, `WHERE source_id = ?`), `purge_source_except` (the tools one server stopped publishing), the `purge` branch of `reconcile` (a category mirror), and the boot seed's builtin sweep. The row's `tool_embeddings` chunks go with it **explicitly** — no delete leans on the FK cascade being enabled. Nothing is ever "marked removed", and a set going is ONE statement: that is what makes a server's hundreds of rows, or a whole vanished family, cheap to drop, and every one of them books into the startup's op window as **移除**. A tool that is merely switched off keeps its row (`status = disabled` — a config fact, not a death), so the two answers never blur.

The write side is the mirror image: `reconcile` reads the rows it needs **once**, then decides per name — a name the db does not hold INSERTs, a name it holds is compared in memory across the five config-derived columns and written only if one moved, everything else is `skipped`. **新增** and **更新** are therefore distinct at the db layer, not inferred afterwards (§6's counting).

### `tool` — one row per tool

```
name        -- unique; external tools are "{server}__{tool}", a job is
               "job-" + its function name (job-translate), system tools bare,
               and the two source-fed families are NAMESPACED: a skill row is
               "skill:<dir>", a cli row is "cli:<entry>".  A name is the row's
               identity — the primary key, the embeddings' foreign key, the
               key every search result is merged by — so two families cannot
               share one, and sharing is not a mistake to prevent:
               `browser-harness` is a CLI *and* the skill documenting it.
description -- used by search
category    -- builtin | job | plugin | mcp | rest-api | skill | cli — where it
               came from AND what kind of thing it is.  There is no `type`
               column projecting it: a derived column is a second thing to
               write and keep in sync on every insert, update and migration,
               and every question it answered is a membership test over
               FUNCTION_CATEGORIES (see §5)
source_id   -- owning server name for mcp/rest-api, owning PLUGIN name for
               plugin/job rows, 'n/a' for builtin (no separate component owns them)
               — and for skill/cli too: neither is owned by a server
schema      -- the tool def {name, description, inputSchema} for func rows;
               the SKILL.md text for skill rows; a synthesized
               {name, description} for cli rows.  This column is not only the
               injected definition — it is also the semantic index's DOCUMENT,
               so an empty one keeps the row out of the index and out of any
               search that reaches the catalog through it.  'n/a' means "no
               schema text" and is excluded from embedding
status      -- enabled | disabled | error, every category (see above).  A
               caller with no opinion does not write the column at all
load_status -- loaded | unloaded for a function category; 'n/a' for
               skill/cli.  NOT a status column — see `status`
last_loaded -- ISO timestamp, bumped on func_tool_load, on an autoload
               override, and on every successful execute (LRU key)
```

The two running state columns are deliberately separate questions: `status` is
what `tools.yaml` says (or what the runtime found), `load_status` is what the
model decided. **No column is nullable** — "not applicable" is a value (`'n/a'`,
or `''` for a never-loaded `last_loaded`), never NULL, so every read is a plain
comparison instead of an `IS NULL` branch that a caller can forget. The effective
status (§5) joins them — `status` when it is not `enabled`, otherwise
`load_status` — and that split is why a one-second disconnect no longer costs the
session its loaded set.

The category is the whole answer, and it is stored once. It says where a tool came from (a builtin module, a job file, a built-in plugin's own tool, an external server, the skills dir, a `tools.yaml` cli entry) **and** what kind of thing it is, because those are the same fact here: a skill row is a file, a cli row is a config entry, and everything else is a function tool with a load state. **There is no `type` column.** A `func | skill | cli` projection of the category had to be written on every insert, compared on every update and backfilled on every migration — for a question (`FUNCTION_CATEGORIES` membership) that costs nothing to re-ask. It is also checked at open now: `_check_columns` reports an unexpected column as a stale file, so a leftover from a dropped revision cannot sit in the db unmaintained.

`plugin` and `job` are the two categories the built-in **job-coding** plugin feeds, split by name: a `job-<function>` tool is a job file's function (`job`), everything else the plugin exposes — `job-write` / `job-list` / `job-run` / `job-remove` — is the plugin's own (`plugin`), as is every other built-in plugin's tool (`catalog_service.plugin_category`).

Schema revisions: a v2/v3 file is ALTERed and backfilled at boot (`_migrate`). **v4 (the `plugin` category) and v8 (the three-state `status`, which collapsed the `enabled` / `unavailable` pair) are deliberately not migrated** — widening a `CHECK` needs the table rebuilt, and this db is derived data (every row comes from the registry, `tools.yaml`, the skills dir or the plugin children), so the file is DELETED and rebuilt instead. `_check_categories` / `_check_columns` read the live DDL at every open and report a stale file through `system_health` rather than letting its writes fail silently.

**The `schema` column is the row's documentation — what the model reads and what search indexes.** For a func row it is exactly the tool def, and it is exactly what gets injected; its shape is fixed to `{name, description, inputSchema}`:

- `inputSchema` is a plain JSON Schema (per-parameter type + description + `required`, plus `additionalProperties: false` on every harness-authored schema — `Tool.__init_subclass__` adds it unless the tool states its own answer). Nothing custom is ever placed *inside* it — a non-standard key would make the schema invalid for the tool-calling wire format, and `additionalProperties` is a standard one. A closed schema is what `validate_args` enforces at dispatch (DESIGN.md → *Schema Authoring*). A **remote** row (`mcp` / `rest-api`) keeps whatever its server declared, so its closure is not ours to assume.
- No other keys. The source's declared *output* schema (an MCP server's `outputSchema`, fastmcp's `output_schema`) is deliberately **dropped**: the OpenAI function definition has no return slot, and a `-> str` wrapper yields only a meaningless `{result: string}` artifact. Return information belongs in `description` — where builtin tools already state it ("Run a shell command. Returns stdout + stderr.") and where a plugin tool's docstring summary lands (note: fastmcp *discards* a `Returns:` docstring section, so it must be in the summary to survive).
- It is **never docstring text** — plugin tools enter the catalog as the JSON schema fastmcp derived from their signature/docstring, not as the raw docstring.

`_function_from_schema` re-serializes this column into the OpenAI function definition (`parameters` ← `inputSchema`), so the stored schema and the wire schema are one and the same — a single `descriptor_json` builder writes it at every site (seed, external mirror, plugin mirror).

Only a function category (builtin | job | plugin | mcp | rest-api) participates in the load state (the §8.5 rule: loaded/unloaded applies to function tools only; skill and cli carry `'n/a'`). The other two categories are rows all the same — being in the catalog is what makes them findable by `tool_search`, which is the whole point of one catalog:

- **skill** — one row per skill directory, `load_status` `'n/a'`. The `schema` is the SKILL.md verbatim: a playbook *is* its documentation, so that text is what search indexes (keyword via FTS, semantic via the drainer) and `skill_use` returns it to the model. Rows are mirrored from the skills dir at boot and after every `skill_set` / `skill_remove` / `skill_set_enabled` (`skill_catalog_rows` → `ToolCatalogService.sync_category`); `status` mirrors the `skills:` config disable, or reports `error` for a SKILL.md that cannot be read — the one runtime failure this family has, and the reason a broken file neither hides its siblings nor loses its row.
- **cli** — one row per `tools.yaml` `cli` entry, `load_status` `'n/a'`, `schema` `'n/a'` (a CLI has no tool def — the description is what identifies it, and `cli_list` carries the command/install detail). Mirrored from the `cli` section at boot and after every `cli_set` / `cli_remove` / `cli_set_enabled`; `status` mirrors the entry's own `enabled` flag — nothing is spawned until it runs, so config is all this family can report.

Both mirrors are **upsert + purge**: a skill removed from disk or a CLI removed from the config loses its row in the same pass, so `tool_search` never returns a hit whose source is gone.

### `tool_embeddings`

Chunked vector rows for semantic search (one tool → several chunks; closest-chunk aggregation). **A schema move is the ONE embedding trigger**: the reconcile deletes the row's vectors when its `schema` text changes (and hands a new row over once, so its first vector gets made), and the drainer embeds exactly the rows that have none — so "this row has vectors" and "its schema has not moved since they were made" are the same statement. The drainer embeds `_flatten_schema(schema)`, which drops `enum`/`default`/nesting, so an edit that only touched a dropped field re-embeds a tool that did not really need it — one redundant embedding call, and the price of the invariant. (The alternative was a second comparator that flattened both sides and had to stay in step with the drainer's own predicate forever.) Stale-width vectors are skipped at query time.

**Chunking is the one shared chunker — not a catalog-specific one.** The catalog's `SemanticManager` (`slife/tools/semantic.py`) subclasses memdb's and inherits `_embed_doc`, so a tool schema is embedded through the *same* path as a memdb turn or a memfiles doc: `_chunk_text` splits on paragraph boundaries (`CHUNK_SIZE_CHARS`, one line of overlap), then `_split_chunks_to_token_limit` hard-splits anything still over the model's `max_tokens` at a **1 char/token floor**, and each chunk is embedded in its own request. That floor exists for exactly this shape: a large schema flattens to a long, newline-free, escape-dense params line that tokenizes at 1–2 chars/token — without the split it rode as one chunk, the provider rejected it (bge-m3's 8192 cap), and the tool stayed unembedded forever with the semantic gate locked off. A small schema simply yields a single chunk.

### `meta`

A key/value table for facts about the **catalog itself** — as opposed to a tool
row, which is a fact about one tool.  Two keys today:

- `embedding_model` — the model identity the stored vectors were made with, so a
  model change (even a same-width one) drops a vector space that no longer
  matches;
- `semantic_state` — the semantic index's state (`state` / `reason` /
  `semantic_ready` / model / dimension), **published by whichever process runs
  the drainer**, rewritten on every transition.

`semantic_state` is here rather than in the manager that owns it because the
index is *shared* (its vectors are in this same file), so a process that runs no
drainer — a subagent worker — must still be able to report it.  Without the row
a worker could report nothing but the pending count, and read *"maintained by the
main process"* while the main agent reported that same index as degraded.
`_set_state` is the one writer of the state, so it is also the one publisher:
the row cannot lag the transition it describes.  A reader that finds no row
reports `state: "unknown"` (no drainer has published) — a fact, where
`disabled` would be a claim about a drainer that isn't there.  The pending count
is deliberately **not** published: whoever needs it counts the rows, so a copy
here could not go stale against them.

**Who owns the index, and who may query it.**  These are two grants, not one,
and conflating them is what made every subagent's `tool_search` keyword-only:
a worker owns no drainer, so it was given no semantic surface at all, while the
index it needed was sitting in the database its parent was maintaining.

| grant | main agent | worker |
|---|---|---|
| maintain the rows (`catalog_owner`) — seed, mirror, reconcile, purge, evict | yes | no — never writes the shared file |
| maintain the vectors (`catalog_drainer`) — the drainer | yes | no |
| **query** the index (`ToolCatalogService.semantic_query`) | its own manager | a `SemanticReader` over the same rows |

Both surfaces answer the same three calls (`query_ready` / `reason` /
`embed_query`), so `tool_search` does not branch on which role it is in — and a
worker's answer comes from the index's *published* facts rather than from a
guess: it refuses while the index is still filling, and it refuses when the
published model is not the one this process would embed with (a different vector
space can have the same width and every similarity would still be meaningless).
Both grants live in `slife/agent/roles.py`.


### Effective status

A tool's *effective* status is derived from its own row — there is nothing to
join — with `status` outranking `load_status`:

| condition | effective |
|---|---|
| `status = 'disabled'` (any family) | `disabled` |
| `status = 'error'` (its server / plugin is not usable) | `error` |
| function tool, `load_status='loaded'` | `loaded` |
| function tool, `load_status='unloaded'` | `unloaded` |
| skill / cli (no load state) | `enabled` |

One label per fact: "the config turned it off" is `disabled`, "its owner is not
usable" is `error`, and "what the model asked for" is `loaded`/`unloaded`.  The
first two never overwrite the third, which is what lets a loaded tool come back
loaded — and a row with no load state at all reports its own `status` rather
than a sentinel, because `'n/a'` tells a reader nothing about a row whose state
is perfectly well known.

**Injection takes the `func` rows that are `enabled` and `loaded`.**  That is
the whole predicate (`catalog._INJECTABLE_SQL`), and it is also
`_effective_status`'s rule; the two move together.

---

## 4 · Discovery & load — the meta surface

The whitelist (`slife/tools/whitelist.py`) protects three always-injected families, never LRU-evictable and not unloadable:

- **harness pair** — `_turn_prompt`, `_check_new_input`, `attach_image` (the loop's mechanism);
- **the tool-system meta tools (5)** — `mcp_set_enabled`, `rest_api_set_enabled`, `tool_search`, `func_tool_load`, `_func_tool_unload`.  There is no connect/disconnect pair: the modern protocol has no session to open or close, so each external family has ONE lifecycle switch (`*_set_enabled`) — enabling (re)connects, and a tool call reconnects lazily if the server died in the meantime.  There is no server-level search either: with the registry gone, `tool_search` (tool-level, filtering on the catalog's columns) is the discovery surface.
- **pinned (2)** — `skill_use` and `system_health`: not tool-system machinery, but the two calls every session reaches for (read a skill's SKILL.md; one health report over every subsystem check).  Pinned so the squeeze can never cost a search+load round trip to re-learn them.

Together: `ALWAYS_LOADED`. Everything else seeds loaded and can be reloaded, but the squeeze may evict it.

### `tool_search`

Search the whole catalog:

The filters ARE the catalog's columns — one parameter per column, so the
surface cannot drift from the table:

```
query        -- free text over name/description/schema
category     -- builtin|job|plugin|mcp|rest-api|skill|cli
source_id    -- owning server / plugin name ('n/a' = local)
status       -- enabled|disabled|error
load_status  -- loaded|unloaded|n/a
mode         -- hybrid (default) | keyword (FTS5) | grep (regex)
limit        -- max results
```

A filter the agent does not supply contributes **no clause at all**. Every one
is a real SQL predicate, so filtering happens before the LIMIT — a column filter
used to run in Python *after* the `limit * 2` candidate fetch and silently
returned 7 of the 14 qualifying rows. `status` is ONE string parameter over the
three states the column has; the `enabled` / `unavailable` boolean pair it
replaced asked the same question in two halves.

**An empty query BROWSEs.** With no text to match it returns the rows passing
the filters, in report order — which is also how a family gets enumerated
(`query=""` + `category=cli`).  Running the legs on `""` instead answered with
whatever the semantic index happened to hold, so a row without an embedding
was unreachable and the keyword leg matched nothing at all.

**Results are scored on one scale.** A hybrid result carries `similarity`, a
normalized 0–1 cosine (≈1 identical, ≥0.5 close, 0.1–0.5 weak, <0.1 mostly
unrelated) plus that legend in the payload's `hint` — the same scale
`turn_recall` and `cabinet_search` report, so the numbers are comparable
across the three.  A semantic leg always returns its k nearest, however far
away they are, so without the number "nothing matched" and "the nearest
neighbours are unrelated" look identical.  A keyword-only hit carries no
`similarity`: nothing measured it, and inventing a number would be a lie about
the match.

Three retrieval routes, one row shape (the effective status is computed per row — nothing to join):

- `grep` — **regex** over name/description/category/source_id/schema.  A real grep (`re.search`), so `summ.rize` and `translat(e|or)` match; an invalid pattern is reported, never a silent no-match.  SQLite has no regexp engine, so the match runs in Python after the column filters narrow in SQL — a scan rather than an index seek, which is also why it examines *every* filtered row instead of the first `limit` candidates.
- `keyword` — FTS5 BM25, CJK-routed to a LIKE multi-word AND fallback;
- `hybrid` — keyword + semantic KNN (sleepy `tool_embeddings`, closest-chunk per tool) merged by RRF.

### `func_tool_load`

Loads a function tool by **full name** (`{server}__{tool}` for external). Refusals come from the effective status: unknown → "see tool_search"; disabled → "enable it first"; error → "its server is not up right now — check it with mcp_list, then retry". On success the row's status flips `loaded` and the tool is in the **very next LLM request** — the loop re-reads the loaded set before every request (§5 · Per-request injection), so a load takes effect on the next call, not the next turn. For `mcp`/`rest-api` rows it also **materializes the execution proxy** from the row's schema descriptor (`create_proxy_tools` → registered in the registry) — loading and materialization are the same step, driven by the row.

What a load does **not** do is unlock anything: it never gates a call. A tool with an execution instance is callable whether or not the model loaded it, so load-and-call in one message is legitimate — the load is what puts the tool's **schema** in front of the model, which is what makes the arguments read rather than guessed.

### `_func_tool_unload`

Self-service unload (the spare-ticket the model can use to free a slot in its tool list — it does not make a tool uncallable, which load state never does); refused for the whitelist — `skill_use` and `system_health` included. The harness's eviction is the same operation run automatically. One family is the exception on the execution side: an **external** `mcp`/`rest-api` tool's proxy is unregistered along with it (that proxy holds a live client), so unloading one of those does take its route away — a resource decision, not a gate.

---

## 5 · The load/unload lifecycle

### Boot ordering

1. Host `_init_catalog` opens `tools.db` and runs the session seed (`sync_system_tools` over everything registered): every registered tool gets a row, and **a NEW row is `loaded` only from the two autoload sources** — the whitelist (`ALWAYS_LOADED`: the loop's own tools, a system-level protection that is not configurable) and the entries marked `autoload: true` in `tools.yaml` (user intent) — everything else is born `unloaded`. An EXISTING row keeps whatever the model decided (the sync mirrors *which* tools are registered, never *what is loaded*) — **with the one exception of `autoload`**, which is a standing config statement and re-asserts `loaded` on every pass (§5's writer list). The two registry-less categories are mirrored in the same pass (`_mirror_local_rows`: the skills dir → `skill` rows, the `cli` section → `cli` rows). Every external row is then marked `error`, because no server is up yet.
2. The wrapper spawns; `_auto_connect_configured` brings up **every server enabled in `tools.yaml`** — spawn only, no `tools/list` (that read belongs to the first caller that needs it, and boot has none). Disabled ones are registered but never started — and never read either, because reading IS connecting (`refresh_tools` refuses a switched-off server, the same rule `call_tool` enforces; see §6) — so `mcp_list` still matches the config's `mcp.servers` section (the `rest-api` section is `rest_api_list`'s); a server that was down at boot has the background repair armed. Nothing is remembered from a previous session — a server that was down when you quit is retried here like any other.
3. Each successful connect publishes `tools/list_changed`; the host's listen stream wakes the reconcile below, which mirrors that server's tool rows (new rows `unloaded`) and clears the `error` mark. `_wire_mcp_glue` also runs one reconcile the moment the gateway is ready — **as a background task**, so the plugin start never waits on external servers (a `_sync_mcp_proxies` pass asks every configured server for its list, which for a peer the pool holds nothing for is itself the spawn). Startup converges as servers come up; one that never comes up simply stays `error`.
4. `tools.yaml` removals are purged in the same pass (config is the authority), and so are **builtins the code no longer has**: the seed is the builtin family's whole membership (`own_builtins`), so a class deleted or renamed in code loses its row there — no other pass could ever claim it. A builtin switched off in `tools.yaml` is in the seed's list (its instance comes from `disabled_tool_instances`), so it keeps its row marked `disabled`.

### Per-request injection

Before **every** LLM request the loop refreshes a **snapshot**: `snapshot_loaded()` = the rows with `load_status='loaded'` (and not config-disabled) ∪ `ALWAYS_LOADED`, and builds the request's function list from `rows_for_names(snapshot)` — the catalog `schema` column — via `_function_from_schema`. The registry key (full `{server}__{tool}` name) **always wins** over the descriptor's bare name, so the injected name is exactly what `registry.execute` resolves. A row missing its schema falls back to the materialized instance; no catalog → the whole registry (historical behavior).

Per-request is what makes `func_tool_load` mean anything: a tool loaded in iteration *n* is in iteration *n+1*'s request. The list's order is the registry's (insertion order), so a proxy materialized mid-turn **appends** — the request's prefix up to that point is untouched and the prompt cache survives the load. One request's own retries (a transient transport failure) reuse the list computed for it, so every attempt sends byte-identical tools. Eviction is the one thing still on a turn boundary (§ below).

### Threshold eviction

At the turn boundary, if loaded count exceeds `tool_load.threshold` (default 100), `evict_to_threshold` drops the excess via `evict_lru` — ordered `last_loaded ASC, name` — an empty `last_loaded` (`''`, never loaded) sorts first, then least-recently-used. Protected from eviction: the same two autoload sources that seed a row `loaded` — `ALWAYS_LOADED` and the `autoload` entries (an autoloaded server contributes every name it owns, resolved by `source_id`). Two writer rules keep the LRU honest:

- `seed_inventory` seeds without touching `last_loaded` (never-used tools sort oldest);
- **every successful `registry.execute` bumps `last_loaded`** (`CatalogStore.touch`), so a tool used this turn is never the next victim. Eviction is main-owner only: subagents inherit the curator's budget and never squeeze it.

Evicted tools stay registered and stay **callable** — eviction takes them out of the injection snapshot and nothing else. **Load state governs what a turn injects, never what a call may do**: an evicted tool the model still remembers, or reaches by name, executes like any other. What it loses is its schema, and the next `func_tool_load` restores that.

### Who writes `load_status` — exactly four places

`load_status` is the model's decision, so the code that may overwrite it is a closed list. Everything else about a row's state lives in the `status` column's two lanes ("the owner is down" / "the config switched it off"), which is what keeps a disconnect or a restart from costing the model its set.

| writer | effect |
|---|---|
| **`autoload` config** | a sync re-asserts `loaded` for an `autoload: true` entry — per tool for `builtin`/`job`, per **server** for `mcp`/`rest-api` (no per-tool flag there, one decision over the whole set). This is the only place config wins over the model; skill/cli are ignored (no state to own). A row already `loaded` is not written. |
| **`func_tool_load`** | `load_tool` → `loaded` (and, for `mcp`/`rest-api`, materializes the proxy). It rolls the flip back if materialization fails. |
| **`_func_tool_unload`** | `unload_tool` → `unloaded`; refuses the whitelist and non-function rows. |
| **eviction** | `evict_to_threshold` → `evict_lru` → batch `unloaded` for the oldest-by-`last_loaded` over-threshold rows (never the whitelist or an `autoload` entry). |

The store enforces the rest of the rule mechanically: `reconcile` applies an incoming `load_status` on INSERT only — an existing row keeps its state — unless the row carries `override_status` (the `autoload` projection), in which case the value is written **only when it differs**.

### Plugin & job tool rows

A built-in plugin's tools are mirrored into the catalog **when its child is spawned and ready** (`_spawn_plugin_generic`, the one path every plugin start and watchdog restart takes), when a subagent connects over HTTP to a shared plugin, and again on every runtime tool-set change (a `job-write` / `job-remove` → `tools/list_changed` → the host's `_rescan_plugin_tools`). Each row is `plugin` (the plugin's own tool) or `job` (a job file's function), with `source_id` naming the plugin that owns it; `load_status` follows the same rule as the builtin seed, so a plugin tool is **searchable, not injected, until loaded**.

The mirror is **upsert + purge**: a source-scoped `sync_system_tools(source=<plugin>)` writes the current rows and deletes the rows of tools that vanished (the names the source owns minus the incoming set) — so a removed job cannot linger as a stale row that `tool_search` keeps returning. Two further edges: a plugin that is **skipped** (its enable gate said no) owns no rows, so its rows are purged (`purge_source`); and a plugin whose child **exits** has its rows flagged `error` (`mark_plugin_connected` clears the mark when it is up again — the mark is the runtime lane of the status column, so the load state is never touched).

---

## 6 · External MCP / REST-API integration

Third-party capability enters only as a standard MCP server in `tools.yaml`'s `mcp`/`rest-api` sections, connected by the internal **mcp-gateway** plugin (one `MCPServerConnection` per server, in its pool). A REST API is one `mcp-openapi-proxy`-backed server, so it is a completely ordinary gateway server that happens to sit in the other section — **the section is what makes it one** (`config.is_rest_api` / `rest_api_names`), and the fact crosses to the host as `rest_api` on the `__check` row (`ServerConfig.rest_api`, derived at load). It is deliberately *not* read out of `source`: that field records where a definition was **downloaded** from (`github` / registry / hand), which is a different question.

### Protocol era (2026-07-28) and change notifications

Every link negotiates its peer's protocol era at connect time and never
configures it (`slife/mcp/era.py`, driving the SDK's `mode="auto"` policy): a
peer that answers `server/discover` is adopted **modern** (no session, no
handshake, per-request `_meta`), one that answers as legacy keeps the
`initialize` handshake.  So a mixed fleet works unchanged — including the
external servers in `tools.yaml`, which drift to the modern protocol on
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

Driven by connect events / `mcp_*` mutations / `tools/list_changed` / the gateway's ready glue (in the background):

1. **Project the connectivity verdict**: read the wrapper's live `__check`; a server whose last `tools/list` succeeded has its verdict cleared, every other **switched-on** configured server (down, failed) has its tools marked `error`.  A server switched off in the config gets **no** verdict — it is `disabled`, not down, and the two must stay tellable apart.  A failed probe is *not* a verdict either: the rows are left untouched rather than marking every server broken;
2. **enabled servers** — register their proxies (full diff) and mirror their tool rows (new rows `unloaded`, or `loaded` when the server entry is marked `autoload` — which also re-asserts `loaded` on an existing row, §5).  `auto_load` gates the **seed**, never registration: the registry is the execution pool, so a tool the catalog calls `loaded` must have an instance behind it.  Gating this on `auto_load` was the bug — `load_status` is durable across a restart while the registry is not, so a tool loaded in a previous session sat at `loaded` with no proxy and every call failed "is not loaded" while the catalog insisted it was loaded;
3. **disabled servers** — nothing is asked of them. `enabled: false` means "stays configured but is not connected" (tools.yaml), and asking for a tool list IS connecting (`refresh_tools` → `ensure_session`), so the reconcile leaves them alone and `__mcp_list_tools` refuses one outright. A switched-off server therefore keeps whatever rows it mirrored while it was enabled — the switch projection (step 1) writes `disabled` onto them, so `tool_search` still finds them and re-enabling needs no re-discovery — and a server that has never been enabled simply has no rows. Only a server with a working tool list yields rows (`__mcp_list_tools` answers empty otherwise — and asking is what reads the list, so this step is also what makes a peer list at all); a down server's rows stay and its verdict keeps them out of injection;
4. **drop** registered proxies whose server left the config (`mcp_remove` is the only unregister path — a merely disabled server keeps its proxy, and its rows keep the `disabled` report);
5. **purge the removed server's catalog rows.** Comparing against **tools.yaml** (the authority) rather than the pool keeps a transient empty pool — a gateway restart — from wiping a still-configured server's rows;
6. **re-project the verdict** after the mirrors: steps 2–3 are what ask for the lists, so a peer that listed during this pass was still flagged when step 1 looked.  One more `__check` plus the guarded per-server write (which costs nothing where nothing moved) settles the pass.  That same read is what tells the pass whether the set has **converged** — which servers still have no `tools/list` (see *The boot window*) — and the `CatalogOpDelta` the startup line reports is armed with the catalog, never here: the pass that converges is not the pass that did the work (see *The boot window*).

Two cost rules the pass follows: the per-server mirrors run **concurrently** (each awaits a real `tools/list`, and for a peer the pool holds nothing for, the spawn that makes one possible — sequentially, every server waited on all the servers before it), and each server's rows go over in **one batched `reconcile` + one `purge_source_except`** (a per-tool upsert re-read the whole table per tool).

Registering a tool never loads it: every new row lands `unloaded` and only `func_tool_load` puts it into the injection set — the `autoload` entries excepted, which are born `loaded` and stay that way.

### The boot window

The pass runs in the **background** (the gateway's ready glue — it never holds the plugin start open), so there is a window after startup where the registry holds the builtins but not yet the external tools.  A call in that window fails, and which failure it is depends on whether the tool's catalog row exists yet: `Unknown tool` before the row, the *"is not loaded"* refusal after it.  Nothing is broken; the pass has not finished.

This is the window that makes a durable `load_status` fragile: the catalog remembers `loaded` across a restart, the registry does not, and registering every enabled server's proxies (step 2) is what re-joins them.

The **`tools_synced` line marks the moment the set is usable**, in one shape for every startup:

```
⚙ 工具集同步完成，耗时 28.4s，新增 1585，更新 0，移除 0 — 1585 个工具可用
⚙ Tool set synced in 28.4s, 1585 added, 0 updated, 0 removed — 1585 tools usable
```

A cold start reads the **same figure twice**: every row written was callable, and the one row an `enabled: false` entry contributes is counted on neither side (§*the three counts* below).  A server that never spawned moves neither figure — an empty listing mirrors nothing, so there is no row to be counted (see *the reconcile*, step 2).

It is emitted **once per process**, on the first pass that has **converged**, and **always on failure** — never on a later pass, however much that pass moves: `tools/list_changed` fires on the gateway's own cadence (a server re-registered every 30 s in a live session), so a line each would be a heartbeat rather than news.  Silence therefore keeps meaning *still syncing*.  `total` is what is **usable right now** — every catalog row whose `status` is `enabled`, so `skill` / `cli` count too (they have no load state, but they are tools like any other) and a row switched off in `tools.yaml` or marked `error` does not.  It is deliberately **not** the registry: the registry holds registered *instances*, and the two registry-less families have none — counting it read `1575` for a catalog the same startup had just written `1586` rows into.  (Same population `check_tool_catalog` reports — the rows — one `status` narrower.)  What a turn *injects* is the narrower `load_status` snapshot, so the line promises availability, never injection.

**Convergence is "no enabled server is still starting".**  "No list" alone means three different things, so two more facts off the same `__check` the pass projects connectivity from decide it:

- **`spawn_settled`** — the gateway's boot pass has finished bringing every configured server up (or failed to).  It runs the spawns **concurrently**, so it costs the slowest transport rather than the sum, and each attempt is capped by `ready.connect_startup`; a disabled server is registered without any attempt at all.  Until the flag is set, a server with no transport is one whose spawn is still in flight and **nothing is judged** — a pass woken mid-spawn would mark a server `error` seconds before it came up.  That is also why the boot pass nudges the host **once, after its gather**, instead of once per server.
- **`reachable`** — that peer's transport is up.  Once the boot has settled, no list + no transport is a spawn that failed: `error`, and the line does **not** wait on it (its recovery needs no reservation — the gateway's armed retry re-syncs it if it ever does come up).  No list + a live transport is a server that simply has not answered yet: a REST proxy installing its environment takes tens of seconds, and its first listing can even exceed `ready.list_tools` and succeed on the retry.

So the pending set is: configured, switched on, not `tools_ok`, not waiting on user auth, and reachable.  The pass reports nothing while it is non-empty, and is woken by the late server's own `tools/list_changed` when it arrives.  The wait on a reachable-but-unlisted server is bounded by `ready.tool_sync_wait`, which follows the gateway's re-list backoff — past it that retry has topped out, and the line reports what the set has rather than staying away for the whole session.

The three counts are a **delta of what the startup WROTE to the catalog** — the config-vs-db comparison the reconcile performs, expressed as row operations — and they are printed even when all three are zero, so the line keeps one shape.

The **window is the startup's, not one pass's**.  Armed at the first pass it would report one source's rows rather than the startup's: the pass that converges is the pass whose *last* server finally answered `tools/list`, i.e. the slowest one — a REST proxy installing its environment — so a real cold boot of 1586 rows read `新增 1239`, 1239 being exactly that server.  It opens instead where the catalog opens (before the boot seed, the skill/cli mirror and every plugin's connect), and the line that reports it is the line that closes it.

`CatalogStore` books the writes into a `CatalogOpDelta` armed there: **insert → 新增** (a name the catalog did not hold *and* a row that is not born switched off — an `enabled: false` entry is written, because `tools.yaml` names it and the db must agree, but nothing became callable, so it books nothing on either side), **a config-derived column that moved → 更新**, **delete → 移除** — over every write the startup makes, across all of its passes (`reconcile`, `purge_source`, `purge_source_except`, `remove_tool`, `set_source_enabled`).  A name the config still publishes unchanged is *skipped* and counts nothing, which is why a warm restart reads `新增 0，更新 0，移除 0`: the proof that the tool set did not move.

The delta is deliberately **not** a registry before/after.  The registry begins empty in every process, so a set difference reports the entire external tool set as new — `1465 added` on a restart, which announces a process artefact as a change to the tool set, and a restart is precisely what does not change it.  **Runtime state is not a change either**: `load_status` / `last_loaded` moving, or the runtime lane of `status`, is the model's decision and the connectivity verdict, booked nowhere — otherwise a server coming up would report every one of its tools as *updated*.

**The other side of the window**: once the pass has run, a load takes effect **within the same turn** — no waiting for the next one.  For an enabled server the proxy already exists, so `func_tool_load` is a status flip over an execution instance that is already there, and a call in that same turn reaches the real server (a *tool-level* error, e.g. a missing required parameter, is the proof).  What the load buys is the **schema** in the next request — never the right to call, which an instance alone grants.  The materialize branch still runs, but registering an existing name is an idempotent overwrite, so it is no longer load-bearing — and its own failure modes (an unsynced `schema`, an unavailable MCP client) can no longer make a successful-looking load silently not take.

Not gated on embeddings: the pass calls `wake_indexer`/`on_saved` (a wake, not a wait), so semantic search finishing is irrelevant to tool availability.

### Crash survival

When the wrapper child dies, `on_plugin_child_exit` marks **every `mcp`/`rest-api` tool `error`** so none of them keeps injecting a dead transport. The restarted wrapper connects every enabled server again (boot step 2) and the reconcile clears each mark as its server comes up — a crash costs one reconnect, not the session's toolset.

---

## 7 · Consistency & concurrency

- **One source of truth.** The catalog db is the single registry; the agent and every subagent maintain no second in-memory copy (subagents read the same file; the ownership grants in `slife/agent/roles.py` — `catalog_owner`, `catalog_drainer` — are what make "apply to mutations" true of every mutation, including the skill/cli mirror, which used to sit outside every gate). `tools.yaml` is the authority for *configuration*; the catalog is the authority for *state*.
- **Config writes are atomic + cross-process locked.** `write_config` writes a temp file + `os.replace`; the read→mutate→write window around it (model switches, embeddings-config edits, cli/rest-api persistence) is wrapped in a cross-process `filelock` (`config_read_modify_write`) so two processes (host + memdb child) can never clobber each other's change. The lock wait is bounded (`storage.filelock`).
- **Rebuilds are live, not offline.** The catalog is synced from the live wrapper on every (re)connect; a schema change drops the stale embedding row and the drainer re-embeds. There is no offline rebuild step.

---

## 8 · Component map

| Module | Responsibility |
|---|---|
| `slife/tools/catalog.py` | `CatalogStore`: SQL, schema (v9 — the three-state `status` and no derived `type`, no `server` table), FTS5 + semantic KNN, effective status, `reconcile` (the one delta writer), `evict_lru`, `touch`, `purge_source`/`purge_source_except`/`remove_tool`/`remove_tools`/`names_by_category` (the batch pair shares one `_delete_rows` — the ONE removal shape), `mark_source_error` / `mark_all_external_error` / `mark_source_connected` / `set_source_enabled` (the two lanes of `status`), `count_usable` (the line's `total`), `CatalogOpDelta` / `begin_ops`/`end_ops` (the startup's row-operation counter, armed with the catalog, per §6) |
| `slife/tools/catalog_service.py` | `ToolCatalogService`: policy — the boot seed (`sync_system_tools`, and its `own_builtins` sweep of the builtins the code dropped), `mirror_external_tools` (a server's whole set in one pass), `snapshot_loaded`, `sync_category` (the skill/cli mirror), `load_tool`/`unload_tool` refusal matrix, `evict_to_threshold`, `purge_unconfigured_sources`; `descriptor_json`/`tool_descriptor` (the one tool-def builder) |
| `slife/tools/catalog_search.py` | hybrid RRF merge + score annotator (thin adapter over `memdb.search`) |
| `slife/tools/registry.py` | the execution pool; names a row's state when a called name has no instance here; bumps `last_loaded` on successful execute |
| `slife/tools/factory.py` | auto-discovery (gated to `slife.tools` modules) + `enabled`-override filtering |
| `slife/tools/meta_tools.py` | `tool_search`, `func_tool_load`, `_func_tool_unload` |
| `slife/tools/whitelist.py` | harness pair + 5 meta tools + 2 pinned (`ALWAYS_LOADED` — never evicted / not unloadable) |
| `slife/tools/skill.py` | the Skills family + `skill_catalog_rows` / `sync_skill_catalog` (skills dir → `skill` rows) |
| `slife/tools/cli.py` | the CLI family + `cli_catalog_rows` / `sync_cli_catalog` (config section → `cli` rows) |
| `slife/tools/_config_io.py` | YAML read/write, atomic replace, cross-process `config_read_modify_write` lock |
| `slife/plugins/mcp_gateway/*` | the server pool, the boot pass that **spawns** every enabled server concurrently (no tool read — that belongs to the first caller; it publishes `spawn_settled` and nudges once, when the pass is over), `mcp_list`/`__check` (per-server `reachable`/`tools_ok`…, the facts the sync's convergence gate reads)/`mcp_list_tools`/`__mcp_list_tools`, the family-gated `mcp_set`/`mcp_set_enabled`/`mcp_remove` over their family-blind `__mcp_set`/`__mcp_set_enabled`/`__mcp_remove` twins (what `rest_api_*` drives its servers through), the merged config view + `is_rest_api`/`rest_api_names` (it never touches `tools.db`) |
| `slife/agent/loop.py` | per-request snapshot + injection from the catalog; boundary eviction (`_maybe_evict`) |
| `slife/agent/service.py` | `_init_catalog` (the seed, and the arm of the op window the sync line reports), `_mirror_local_rows` (skill/cli rows at boot), `_sync_mcp_proxies` reconcile (the verdict projection, the batched per-server mirrors, the live removal purge), `_mark_server_connectivity` (projects both lanes of `status`, and reports the still-starting servers the sync line waits on), `_report_tool_sync` (once, on convergence), `_wire_mcp_glue` (the gateway's background ready-glue), `_server_category` |

---

## 9 · Open items & notes

Folded down from the DESIGNER_NOTES §8.5 checkout list; implemented/deferred as noted:

- **tools.db = the single registry** — done (agent + subagents read the same file; no per-agent copies).
- **Injected schema comes from the db** (`schema` column), never re-read live — done; the stored shape is the strict tool def (§3) and `_function_from_schema` uses the registry key so external names stay correct.
- **`cli_set` / `skill_set` / `rest_api_set` / `job-write/remove` mutate tools.db + clean up** — done:
  - **mcp / rest-api**: `*_set` persists + connects, `*_set_enabled` reconnects/disconnects immediately, `*_remove` tears the connection down (`_pool.remove_server`) **and** the next reconcile purges the server's catalog rows live (step 5) — no stale rows until restart;
  - **job / plugin tools**: registered/unregistered by the plugin, mirrored into the catalog on connect/rescan, and a vanished tool's row is purged (`remove_tool`) — a removed job does not linger in `tool_search`;
  - **builtin**: there is **no set/remove tool** — a builtin's membership is the registry (code) plus the `enabled:` switches in `tools.yaml` (edited by hand, applied at the next start), so at runtime the family is stable: nothing adds or removes a builtin row. Its one purge path is the boot seed's sweep (`own_builtins`), which is why a builtin deleted or renamed in code loses its row at the next start, and a `tools.yaml`-disabled builtin — handed to the seed as an instance by `disabled_tool_instances` — keeps its row marked `disabled`;
  - **cli**: `cli_set` / `cli_remove` / `cli_set_enabled` rewrite `tools.yaml` (+ the live `Config.cli_tools` snapshot) and re-mirror the `cli` rows right after (`sync_cli_catalog`) — a new CLI is findable by `tool_search` before the next restart, and a removed one loses its row. No load state (per the §8.5 rule); `cli_list` shows the command/install detail;
  - **skill**: `skill_set` / `skill_remove` / `skill_set_enabled` change the skills dir (or its config disable mirror) and re-mirror the `skill` rows right after (`sync_skill_catalog`). The row's `schema` is the SKILL.md — the skill is searchable by its own text; `skill_use` returns it, `skill_list` enumerates.
- **"Whitelist means?"** — resolved: the always-loaded carve-outs of `whitelist.py` (harness pair + meta surface + pinned `skill_use`/`system_health`), a design constant, not configurable.
- **Server auto-disconnect when its last loaded tool is evicted** — *deferred*: eviction today keeps the server connected (its tools are still searchable/loadable for free). Could reconnect by a simple `func_tool_load`; re-arming on `func_tool_load` would make server eviction safe.
- **`watchdog` / plugin process contract** — lives in [PLUGIN_CONTRACT.md](PLUGIN_CONTRACT.md), not here.