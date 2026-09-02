# mcp-plugin — MCP gateway to external MCP servers

**mcp-plugin is a standard [MCP](https://modelcontextprotocol.io) server** that
connects to *external* MCP servers — stdio, SSE, or Streamable HTTP — keeps
those connections alive, and exposes their tools to a host as one MCP endpoint.
It manages everything through MCP itself: add/configure/remove servers, search
and load their tools, call them, check status. Any MCP client can drive it.

> Status: **Streamable HTTP** transport. Ships with
> [Slife](https://github.com/juzcn/slife) as its built-in MCP gateway, but has
> **no dependency on it** — usable standalone or from any MCP client.

## Features

- **One endpoint, every server** — connect any number of external MCP servers
  (stdio / SSE / Streamable HTTP), keep the connections alive with auto-reconnect.
- **Tools are MCP tools** — server management (`mcp_set`, `mcp_remove`, …),
  discovery (`mcp_tool_search`), loading (`mcp_tool_load`) and calling all go
  through the MCP protocol.
- **No CLI** — configuration lives in `mcp-plugin.json5` (self-hosted) and the
  feature surface is 100% MCP tools.
- **Tool catalog in memory** — built live from live connections at load and on
  every reconnect; never persists, never drifts from what the runtime can use.
- **Semantic + keyword search** — hybrid (semantic + FTS5) tool discovery that
  degrades automatically to keyword when no embedding endpoint is available.
- **Standalone config** — path + secret handling mirror the ecosystem
  conventions (`$VAR` → env → credential store).

## Install

Requires **[uv](https://docs.astral.sh/uv)**.

```bash
uv tool install --python 3.13 mcp-plugin                            # standalone
uv tool install --python 3.13 'mcp-plugin[credstore]'               # + credential store (secret placeholders, OAuth)
# or bundled with Slife (its built-in MCP gateway):
uv tool install --python 3.13 git+https://github.com/juzcn/slife.git
```

`pip install mcp-plugin` (into a Python 3.13 environment) works when uv is not
available.

Your external MCP servers need their own runtimes: each `servers.<name>` entry
spawns its `command:` as a subprocess, so the command must be installed and on
`PATH` (`npx` → Node.js, `bun`/`bunx` → Bun, `uvx` → uv). A server whose
command is missing fails at connect time with an "executable not found" error.

`credstore` (`mcp-plugin[credstore]`) is optional — only needed when a secret
uses a `${PLACEHOLDER}` that isn't in your shell env, or you use OAuth.

## Run

The server is a **Streamable HTTP MCP server**. Two serving modes:

| Mode | Command | Client learns the URL how? |
|---|---|---|
| Fixed port | `python -m mcp_plugin.server --port 8123` | Statically: `http://127.0.0.1:8123/mcp` |
| Auto-assigned (slife plugin) | `python -m mcp_plugin.server` | From the stdout signal `{"port": N}` |

Both modes emit the `{"port": N}` signal once the server is ready to serve MCP.

### Connect from a client

**From Slife** — it spawns the gateway automatically (`plugins.external` →
`mcp_plugin.server`), reads the port signal, and passes its own context (e.g.
the active embedding endpoint) through the standard `initialize` handshake.

**From any other MCP client** — run with a fixed port, then register the server
by URL:

```bash
python -m mcp_plugin.server --port 8123
```

```jsonc
// client config (e.g. claude_desktop_config.json / MCP_SETTINGS)
{
  "mcpServers": {
    "mcp-plugin": {
      "type": "http",
      "url": "http://127.0.0.1:8123/mcp"
    }
  }
}
```

## Components

All management is **MCP tools** (there is no CLI).

| Tool | What it does |
|---|---|
| `mcp_set` | Add or update an external server connection (upsert). stdio via `command` + `args`, or http via `url`. Persists to `mcp-plugin.json5`. |
| `mcp_set_enabled` | Runtime enable/disable of a server — enable reconnects + loads tools, disable disconnects + unloads. |
| `mcp_remove` | Remove a server: stop its process, unregister its tools, persist the removal. |
| `mcp_list` | List configured servers (transport, command/url, enabled). |
| `mcp_list_tools(server)` | List a connected server's live tools (single read — the catalog is the live pool). |
| `mcp_tool_search(query, mode, limit, server, include_disabled)` | Search the tool catalog — `hybrid` / `fts5` / `grep`. |
| `__check` | Live per-server status (internal — probed by `system_health`). |
| `__mcp_get_tool(full_name)` | One tool's live schema + enabled status (internal — consumed by `mcp_tool_load`). |
| `__mcp_call_tool(server, tool_name, arguments)` | Call a tool on a connected server (internal — invoked by per-tool proxies). |

Tool loading on the host side is **on-demand by default** — the model discovers
tools with `mcp_tool_search` and loads one with `mcp_tool_load`; a disabled
tool is refused at load/`call` time. Set `auto_load: true` on a server to
bulk-register its tools on connect.

## Configuration

The server reads and writes `mcp-plugin.json5`, located by precedence:

1. `MCP_PLUGIN_FILE=<path>` (env-var override)
2. `./mcp-plugin.json5` (dev — when the current directory is the Slife source root)
3. `~/.mcp-plugin/mcp-plugin.json5` (default, standalone)

A missing file is treated as **first run** (empty config — no servers,
keyword-only search); it is only created on the first write. A file that exists
but cannot be parsed raises a clear error instead of being overwritten.

```json5
// mcp-plugin.json5
{
  servers: {
    filesystem: {
      enabled: false,                  // optional; absent = enabled
      command: "npx",
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."],
    },
    github: {
      auto_load: true,                 // optional; absent/false = on-demand tools
      command: "npx",
      args: [
        "-y", "anyapi-mcp-server",
        "--name", "github",
        "--spec", "https://api.github.com/github-raml",
        "--base-url", "https://api.github.com",
        "--header", "Authorization: Bearer ${GITHUB_TOKEN}",
      ],
    },
    remote: {
      url: "https://example.com/mcp",              // http server (SSE/streamable auto-detected)
      headers: { Authorization: "Bearer ${REMOTE_TOKEN}" },
      auth: { type: "oauth", client_id: "${OAUTH_CLIENT_ID}" },
    },
  },

  // Optional — semantic tool search. Absent ⇒ keyword/grep fallback.
  embeddings: {
    base_url: "http://127.0.0.1:17347/v1",   // any OpenAI-compatible /v1 endpoint
    api_key: "local",                        // "" | plaintext | "${VAR}"
    model: "bge-m3",                         // optional; endpoint's active model otherwise
  },
}
```

**Server entry fields:** `command`/`args` (stdio), `url`/`headers` (http),
`env`, `description`, `enabled`, `source` (provenance), `auth` (OAuth),
`os_paths` (inject `--allow-path`), `auto_load`.

**Secrets** — every secret field (`env`, `headers`, `auth.client_id`/
`client_secret`, embeddings `api_key`) accepts `""` (empty), plaintext, or a
`${VAR}` placeholder resolved at use time in the order **shell env → credstore
→ literal**. An unresolvable `api_key` placeholder degrades to empty — a
literal `Bearer ${VAR}` is never sent.

## Embeddings (semantic search)

`mcp_tool_search` defaults to `mode="hybrid"` — semantic + keyword merged.
Semantic search is on **only when** an embedding endpoint is usable; in every
other case it degrades **automatically** to keyword (`fts5` BM25, with a LIKE
fallback for CJK), never failing:

- embeddings **not configured** → `fts5`
- endpoint **unreachable** / model **failed to load** / index **still building** → `fts5`, upgrading back automatically when ready

`mode="grep"` (exact substring) never involves embeddings and always works. The
result's `mode` field reports what actually ran and a `hint` names the reason.

**Embeddings precedence.** As a standard Streamable HTTP MCP server, a
connecting client can pass its own embedding endpoint through the official
`initialize` handshake's `clientInfo` — that **wins** when present (Slife sends
the agent's active embedding endpoint, so the catalog embeds against the same
server as Slife's memory plugins).  When the client passes no embedding params,
the `embeddings` section above is used; with neither, search is keyword-only.
The index drains automatically after the first handshake — there is no offline
rebuild command.

## Usage example

```text
1. mcp_set(name="github", command="npx",
            args=["-y","anyapi-mcp-server","--name","github",
                  "--spec","https://api.github.com/github-raml",
                  "--base-url","https://api.github.com"])
   → connected, tools listed

2. mcp_tool_search("search repositories")   → finds github__… tools

3. mcp_tool_load("github__search")           → registers the tool

4. github__search(query="...")               → calls through the gateway
```

## Notes & limitations

- The tool catalog is **in-memory** and rebuilt live from connections — nothing
  persists, so a search always reflects exactly what the runtime can use.
- An external server whose tool surface changes mid-session is re-synced on its
  next (re)connect.
- Enable/disable is **server-granular** (`mcp_set_enabled`); there is no
  per-tool toggle.
- Server names are unique and must not collide with reserved built-in names.

## License

MIT (see the Slife repository).