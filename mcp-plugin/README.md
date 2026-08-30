# mcp-plugin

Standalone MCP gateway — persistent connections to external MCP servers, with a
CLI to configure and maintain them. Ships with [Slife](https://github.com/juzcn/slife)
as its built-in MCP plugin but has **no dependency on it**. Depends only on
`fastmcp`, `mcp`, `httpx`, `json5`, and `aiosqlite`; `credstore` is optional
(secret `${PLACEHOLDER}` resolution + OAuth token storage).

## Requirements

Requires **[uv](https://docs.astral.sh/uv)**.  The uv routes below pin
**Python 3.13** (`requires-python` is `>=3.13`, but 3.13 is what CI tests, and
uv would otherwise pick the newest 3.14 it finds).

Your external MCP servers need their own runtimes too: each `servers.<name>`
entry spawns its `command:` as a subprocess, so the command must be installed
and on `PATH` (see the config example below).  Typical ones are `npx`
(Node.js), `bun` / `bunx` (Bun), and `uvx` (uv).  A server whose command is
missing fails at connect time with an "executable not found" error.

Optional: **`credstore`** (`mcp-plugin[credstore]`) — only needed when a
secret uses a `${PLACEHOLDER}` that isn't in your shell env, or you use OAuth
(`auth: {type: "oauth"}`).  Without it, placeholders resolve against the shell
env only, and OAuth fails with a clear error naming the extra.

## Install

```bash
uv tool install --python 3.13 mcp-plugin                      # standalone
uv tool install --python 3.13 'mcp-plugin[credstore]'         # + credential store (secret placeholders, OAuth)
# or bundled with Slife (its built-in MCP gateway):
uv tool install --python 3.13 git+https://github.com/juzcn/slife.git
```

`pip install mcp-plugin` (into a Python 3.13 environment) also works when uv
isn't available.  Re-running a `uv tool install` over an **existing** install
is a no-op unless you pass `--reinstall` — use it to upgrade to a new release.

Verify: `mcp-plugin`

## Config

Server definitions live in `mcp-plugin.json5`, located by precedence:

1. `MCP_PLUGIN_FILE=<path>` (env var — Slife exports this to the same directory
   as `slife.json5` when it launches the plugin)
2. `./mcp-plugin.json5` (dev — when the current directory is the Slife source
   root, i.e. its `pyproject.toml` has `project.name == "slife"`)
3. `~/.mcp-plugin/mcp-plugin.json5` (default, standalone use)

This is the same resolution credstore uses for its `credentials.crypt`.

Secret values — `env` entries, `auth.client_id`/`client_secret`, HTTP
`headers`, and the embeddings `api_key` — accept three forms: `""` (empty: no
value / no `Authorization` header), plaintext (used as-is), or a `${VAR}`
placeholder resolved at use time in order **shell env → credstore → literal**
(`VAR` is both the env-var name and the credstore key).  The embeddings
`api_key` specifically treats an unresolvable placeholder as empty — it never
sends a literal `Bearer ${VAR}` — while other fields keep the unresolved
`${VAR}` literal as the last resort.  `${VAR}` refs may also appear embedded
in `args` and `url` (e.g. `"--header", "Authorization: Bearer ${GITHUB_TOKEN}"`).

```json5
// mcp-plugin.json5
{
  servers: {
    filesystem: {
      enabled: false,                  // optional; absent = enabled
      command: "npx",
      args: [
        "-y",
        "@modelcontextprotocol/server-filesystem",
        ".",
      ],
    },
    github: {
      auto_load: true,                 // optional; absent/false = on-demand tools
      command: "npx",
      args: [
        "-y",
        "anyapi-mcp-server",
        "--name", "github",
        "--spec", "https://api.github.com/github-raml",
        "--base-url", "https://api.github.com",
        "--header", "Authorization: Bearer ${GITHUB_TOKEN}",
      ],
    },
    remote: {
      url: "https://example.com/mcp",
      headers: { Authorization: "Bearer ${REMOTE_TOKEN}" },
      auth: { type: "oauth", client_id: "${OAUTH_CLIENT_ID}" },
    },
  },

  // Optional — semantic tool search. Present + base_url ⇒ active; absent ⇒
  // mcp_tool_search falls back to keyword/grep.  Point it at any OpenAI-
  // compatible /v1 endpoint (the local-embed plugin serves this).
  embeddings: {
    base_url: "http://127.0.0.1:8000/v1",
    api_key: "local",                  // optional; "" | plaintext | "${VAR}" (env → credstore)
    model: "bge-m3",                   // optional; endpoint's active model used otherwise
  },
}
```

The `command`/`args` pairs above are spawned as subprocesses on this machine,
so install the runtime each `command` needs (`npx` → Node.js, `bun`/`bunx` →
Bun, `uvx` → uv); see [Requirements](#requirements).

### Semantic search & automatic degradation

`mcp_tool_search` defaults to `mode="hybrid"` — it merges semantic hits with
keyword hits **only when** the `embeddings` section is configured and the
backend is actually working.  In every other situation it degrades
**automatically** to keyword search (`fts5` BM25, with a LIKE substring
fallback for CJK) — search never fails because embeddings is absent or
broken:

- embeddings **not configured** (no `embeddings` section) → `fts5`
- embeddings **misconfigured** (`base_url` placeholder, wrong endpoint, bad
  auth) → `fts5`
- embeddings endpoint **unreachable** at search time (was reachable at
  startup, is not now) → `fts5`
- embedding model **failed to load** → `fts5`
- semantic index still **building/rebuilding** → `fts5`, upgrading back to
  `hybrid` automatically when indexing finishes

`mode="grep"` (exact substring) never involves embeddings and always works.
The result's `mode` field reports what actually ran (`hybrid` / `fts5` /
`grep`) and a `hint` names the reason, so a caller can always tell search
degraded.  Enable semantic search by fixing the `embeddings` section and
running `mcp-plugin build` (there is no MCP tool for it).

### On-demand vs auto-load

External MCP tools are **on-demand by default**: the host does not bulk-register
them into the LLM's tool list. The agent discovers them with `mcp_tool_search`,
loads one into the tool list with `mcp_tool_load`, and releases them at server
granularity (`mcp_remove` / `mcp_set_enabled(false)`).

Set `auto_load: true` on a server to keep the old behavior — its tools are
bulk-registered whenever the server connects.

Enable/disable is **always batch at the server level** (`mcp_set_enabled`) —
there is no per-tool toggle. A disabled server's tools are indexed but marked
disabled in the catalog, refused at call time, and skipped by `mcp_tool_load`.

## Tool catalog (mcp-plugin.db)

Every connected server's tools are indexed into a SQLite DB (`mcp-plugin.db`,
next to the config, or `$MCP_PLUGIN_DB`): `full_name` (`{server}__{tool}`),
`name`, `description`, and a per-tool `enabled` flag. The catalog persists
across restarts, so `mcp_tool_search` works before any reconnect. Changing
`enabled` persists too; a disabled tool is refused at call time.

## CLI

| Command | Description |
|---------|-------------|
| `mcp-plugin` | Overview of configured servers |
| `mcp-plugin set <server>` | Interactive add/configure a server |
| `mcp-plugin set-embed --base-url <url> [--model <name>] [--api-key <key>]` | Add/update the embeddings section (semantic search) |
| `mcp-plugin remove <server>` | Remove a server from config (takes effect at next server start) |
| `mcp-plugin build` | Rebuild the tool catalog DB + index from live connections |

`set` accepts `--transport stdio|http`, `--command`, `--url`, `--args`,
`--env` (`KEY=VALUE`), `--enabled/--no-enabled`, and `--auth oauth` prompts.

`set-embed` writes/updates the top-level `embeddings` section: `--base-url` is
**required** (a placeholder `\${…}` or empty value leaves semantic search
disabled, keyword fallback only).  `--model` and `--api-key` (alias
`--apikey`) are **optional** — omit one to keep its current value, pass `""`
to clear it.  The `api_key` accepts the same forms as other secrets (`""` /
plaintext / `${VAR}`) and is stored verbatim.  Changes apply at the next
server start, or run `mcp-plugin build` to (re)index now.

### Build

`mcp-plugin build` reconnects every enabled server, re-syncs its tools into the
catalog, rebuilds the FTS index, and (when an `embeddings` section is present)
re-embeds the whole catalog. Use it after hand-editing `mcp-plugin.json5`,
after an external MCP server updates its tools, or after switching the
embeddings model. Unreachable servers are reported, not fatal.

## Plugin contract

An MCP-plugin distribution exposes a module that hosts its own FastMCP server
(transport: streamable HTTP on an auto-assigned port, port signaled to stdout as
`{"port": N}`). Slife discovers it via `plugins.external` in `slife.json5` and
spawns `python -m <module>`. The management tools (`mcp_set`, `mcp_remove`,
`mcp_set_enabled`, `mcp_list`, `mcp_list_tools`, `mcp_tool_search`,
`__mcp_call_tool`, `__mcp_connection_status`,
`__mcp_get_tool`) are kept separate from the servers they manage.

### Tools

- `mcp_tool_search(query, mode="hybrid", limit, server, include_disabled)`
  — search the catalog. `mode`: `hybrid` (semantic + keyword), `fts5` (BM25),
  or `grep` (exact substring). Hybrid degrades to `fts5` automatically when
  semantic search is unavailable — embeddings missing, misconfigured, or still
  building (see [Semantic search & automatic
  degradation](#semantic-search--automatic-degradation)). Results carry
  `full_name`, `server`, `name`, `description`, `enabled`, snippet and a
  score; `include_disabled=true` (default) surfaces disabled tools too.
- `mcp_list_tools(server)` — **double read**: the live connected tools AND the
  persisted catalog (`mcp-plugin.db`). When the catalog is unavailable, or its
  data is out of date vs the live tools (single source of truth = live), the
  response flags it and suggests running `mcp-plugin build` offline to rebuild
  the catalog. While a server is disconnected, the persisted catalog (if any)
  is still returned; a failed live read instead returns an error ("MCP
  unavailable") without touching the catalog.
- `mcp_tool_load(full_name)` (host side) — register one tool into the LLM's
  tool list. Refuses disabled tools (a tool is disabled iff its server is
  disabled — enable/disable is server-level only, via `mcp_set_enabled`).

Semantic search is configured **internally** — edit the `embeddings` section
of `mcp-plugin.json5` and run `mcp-plugin build` to (re)enable indexing; there
is no MCP tool for it.
