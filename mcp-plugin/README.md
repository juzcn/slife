# mcp-plugin — an MCP server in front of external MCP servers

**mcp-plugin is itself a [Streamable HTTP](https://modelcontextprotocol.io) MCP
server.**  It connects to *external* MCP servers — stdio, SSE, or Streamable
HTTP, no SDK required on their side — keeps those connections alive, and
exposes everything to a host agent as MCP tools on one endpoint.  Every
feature is an MCP tool: add/configure/remove servers, search and load their
tools, call them, check connection status.  Any MCP client can drive it.

Ships with [Slife](https://github.com/juzcn/slife) as its built-in MCP plugin
but has **no dependency on it**.  Depends only on `fastmcp`, `mcp`, `httpx`,
`json5`, and `aiosqlite`; `credstore` is optional (for `${PLACEHOLDER}` secret
resolution and OAuth token storage).

```
                     external MCP servers (stdio / SSE / streamable http)
    npx anyapi-mcp-server ────┐
    @modelcontextprotocol/… ──┤   persistent connections
    your own server ──────────┤
                              ▼
              ┌───────────────────────────────────┐
  host agent ─┤  mcp-plugin                       │
  (any MCP    │  Streamable HTTP MCP server       │  connect via
   client)    │  http://127.0.0.1:<port>/mcp      │  {"port": N}
  mcp_tool_…  └───────────────────────────────────┘
```

## Tools

All management lives in the server's tool set:

| Tool | What it does |
|---|---|
| `mcp_set` | Add or update an external server connection (upsert). stdio via `command` + `args`, or http via `url`. Persists to `mcp-plugin.json5`. |
| `mcp_set_enabled` | Runtime enable/disable of a server (`mcp_set_enabled(name, enabled)`): enable reconnects + loads tools, disable disconnects + unloads. |
| `mcp_remove` | Remove a server: stop its process, unregister its tools, persist the removal. |
| `mcp_list` | List configured servers (transport, command/url, enabled). |
| `__check` | Live per-server status: running/stopped, tool counts, errors. |
| `mcp_list_tools(server)` | List a server's tools — **double read** of the live connection AND the persisted catalog (`mcp-plugin.db`). |
| `mcp_tool_search(query, mode, limit, server, include_disabled)` | Search the tool catalog — `hybrid` / `fts5` / `grep` (see [Semantic search & automatic degradation](#semantic-search--automatic-degradation)). |
| `__mcp_get_tool(full_name)` | One tool's live schema + enabled status (consumed by the host's `mcp_tool_load`). |
| `__mcp_call_tool(full_name, arguments)` | Call a tool on a connected server (invoked by the host via per-tool proxies). |

The management tools are kept separate from the servers they manage.  Tool
loading on the host side is **on-demand by default** — see [On-demand vs
auto-load](#on-demand-vs-auto-load).

## Run

The server supports **two serving modes** — pick by how your client learns the
URL:

| Mode | Command | Client knows the URL how? |
|---|---|---|
| Fixed port (standalone) | `python -m mcp_plugin.server --port 8123` | Statically — `http://127.0.0.1:8123/mcp` |
| Auto-assigned (default / slife plugin) | `python -m mcp_plugin.server` | From the stdout signal `{"port": N}` |

A client that must point a **static URL** at this server needs the fixed mode
(the same reason slife's embeddings provider fixes local-embed's `:17347`).  A
**host that spawns the process and discovers it** — slife, or any supervisor —
uses auto-assigned: the server binds a free port, then signals `{"port": N}`
to stdout as a single JSON line once it is ready to serve MCP.

```bash
python -m mcp_plugin.server --port 8123    # fixed port, standalone
python -m mcp_plugin.server                 # auto-assigned (+ {"port": N} signal)
```

Both modes still emit the `{"port": N}` signal (fixed mode reports the same
port); the server always binds `127.0.0.1`.

As a **slife external plugin** (the usual hosting), slife spawns
`python -m mcp_plugin.server` itself, reads the port signal, connects on
behalf of the agent, and exports `MCP_PLUGIN_FILE` so the plugin reads
`mcp-plugin.json5` from next to `slife.json5`:

```json5
plugins: { external: [ { name: "mcp-plugin", module: "mcp_plugin.server" } ] }
```

## Install

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

```bash
uv tool install --python 3.13 mcp-plugin                      # standalone
uv tool install --python 3.13 'mcp-plugin[credstore]'         # + credential store (secret placeholders, OAuth)
# or bundled with Slife (its built-in MCP gateway):
uv tool install --python 3.13 git+https://github.com/juzcn/slife.git
```

`pip install mcp-plugin` (into a Python 3.13 environment) also works when uv
isn't available.  Re-running a `uv tool install` over an **existing** install
is a no-op unless you pass `--reinstall` — use it to upgrade to a new release.

## Config: `mcp-plugin.json5`

The server reads and writes `mcp-plugin.json5`, located by precedence:

1. `MCP_PLUGIN_FILE=<path>` (env var — Slife exports this to the same directory
   as `slife.json5` when it launches the plugin)
2. `./mcp-plugin.json5` (dev — when the current directory is the Slife source
   root, i.e. its `pyproject.toml` has `project.name == "slife"`)
3. `~/.mcp-plugin/mcp-plugin.json5` (default, standalone use)

A missing file is treated as **first run** (empty config — the server runs with
no servers and keyword-only search); it is only created on the first write.
A file that exists but cannot be parsed raises `ConfigParseError` rather than
being overwritten.

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
      healthy: false,                  // optional, boolean; set by
                                       // 'mcp-plugin build' (its probe verdict
                                       // of the last run, or default true).
                                       // false = not loaded at startup until a
                                       // later build re-probes successfully;
                                       // mcp_set re-add resets it to true
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
    base_url: "http://127.0.0.1:17347/v1",
    api_key: "local",                  // optional; "" | plaintext | "${VAR}" (env → credstore)
    model: "bge-m3",                   // optional; endpoint's active model used otherwise
  },
}
```

The `command`/`args` pairs above are spawned as subprocesses on this machine,
so install the runtime each `command` needs (`npx` → Node.js, `bun`/`bunx` →
Bun, `uvx` → uv); see [Install](#install).

## On-demand vs auto-load

External MCP tools are **on-demand by default**: the host does not bulk-register
them into the LLM's tool list. The agent discovers them with `mcp_tool_search`,
loads one into the tool list with `mcp_tool_load`, and releases them at server
granularity (`mcp_remove` / `mcp_set_enabled(false)`).

Set `auto_load: true` on a server to keep the old behavior — its tools are
bulk-registered whenever the server connects.

Enable/disable is **always batch at the server level** (`mcp_set_enabled`) —
there is no per-tool toggle. A disabled server's tools are indexed but marked
disabled in the catalog, refused at call time, and skipped by `mcp_tool_load`.

## Semantic search & automatic degradation

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

## Tool catalog (`mcp-plugin.db`)

Every connected server's tools are indexed into a SQLite DB (`mcp-plugin.db`,
next to the config, or `$MCP_PLUGIN_DB`): `full_name` (`{server}__{tool}`),
`name`, `description`, and a per-tool `enabled` flag. The catalog persists
across restarts, so `mcp_tool_search` works before any reconnect. Changing
`enabled` persists too; a disabled tool is refused at call time.

`mcp_list_tools` is a **double read**: the live connected tools AND the
persisted catalog. When the catalog is unavailable, or its data is out of
date vs the live tools (single source of truth = live), the response flags it
and suggests running `mcp-plugin build` offline to rebuild the catalog. While
a server is disconnected, the persisted catalog (if any) is still returned; a
failed live read instead returns an error ("MCP unavailable") without touching
the catalog.  `mcp_tool_load(full_name)` (host side) registers one tool into
the LLM's tool list and refuses disabled tools.

## CLI

The CLI is auxiliary — it covers what the MCP tools do not.  Server
**management** stays in the tools (`mcp_set`, `mcp_remove`, `mcp_set_enabled`).

| Command | Description |
|---------|-------------|
| `mcp-plugin` | Overview of configured servers |
| `mcp-plugin set-embed --base-url <url> [--model <name>] [--api-key <key>]` | Add/update the embeddings section (semantic search) |
| `mcp-plugin build` | Rebuild the tool catalog DB + index from live connections |

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

Build is also the writer of each server's `healthy` flag (a boolean, default
`true`). A server that cannot be connected (missing apikey, outdated version,
or another error) is marked `healthy: false`; the wrapper then registers it but
skips loading it at startup until a later `mcp-plugin build` re-probes it
successfully (which writes `healthy: true` again). This is the flow for a
broken server: fix the cause, re-run `mcp-plugin build`, and the server is
loaded again.