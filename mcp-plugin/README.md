# mcp-plugin

Standalone MCP gateway — persistent connections to external MCP servers, with a
CLI to configure and maintain them. Ships with [Slife](https://github.com/juzcn/slife)
as its built-in MCP plugin but has **no dependency on it**. Depends only on
`fastmcp`, `mcp`, `httpx`, `json5`, `aiosqlite`, and `credstore` (for OAuth token
storage).

## Install

```bash
pip install mcp-plugin
# or bundled with Slife:
uv tool install git+https://github.com/juzcn/slife.git
```

Verify: `mcp-plugin`

## Config

Server definitions live in `mcp-plugin.json5`, located by precedence:

1. `MCP_PLUGIN_FILE=<path>` (env var — Slife exports this to the same directory
   as `slife.json5` when it launches the plugin)
2. `./mcp-plugin.json5` (dev — when the current directory is the Slife source
   root, i.e. its `pyproject.toml` has `project.name == "slife"`)
3. `~/.mcp-plugin/mcp-plugin.json5` (default, standalone use)

This is the same resolution credstore uses for its `credentials.crypt`.

Keys in `env` and `auth.client_id`/`client_secret` support these references,
resolved in order **shell env → credstore → literal**:

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
    api_key: "local",                  // optional
    model: "bge-m3",                   // optional; endpoint's active model used otherwise
  },
}
```

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
| `mcp-plugin remove <server>` | Remove a server from config (takes effect at next server start) |
| `mcp-plugin build` | Rebuild the tool catalog DB + index from live connections |

`set` accepts `--transport stdio|http`, `--command`, `--url`, `--args`,
`--env` (`KEY=VALUE`), `--enabled/--no-enabled`, and `--auth oauth` prompts.

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
`mcp_embeddings_set`, `mcp_embeddings_remove`,
`mcp_semantic_status`, `__mcp_call_tool`, `__mcp_connection_status`,
`__mcp_get_tool`) are kept separate from the servers they manage.

### Tools

- `mcp_tool_search(query, mode="hybrid", limit, server, include_disabled)`
  — search the catalog. `mode`: `hybrid` (semantic + keyword), `fts5` (BM25),
  or `grep` (exact substring). Hybrid degrades to `fts5` automatically when no
  embeddings endpoint is configured or the index is still building. Results
  carry `full_name`, `server`, `name`, `description`, `enabled`, snippet and a
  score; `include_disabled=true` (default) surfaces disabled tools too.
- `mcp_tool_load(full_name)` (host side) — register one tool into the LLM's
  tool list. Refuses disabled tools (a tool is disabled iff its server is
  disabled — enable/disable is server-level only, via `mcp_set_enabled`).
- `mcp_embeddings_set(base_url, model, api_key)` / `mcp_embeddings_remove` —
  manage the `embeddings` section and reindex.
- `mcp_semantic_status` — configured/backend/model/dimension/ready/state/backlog.
