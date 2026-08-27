# mcp-plugin

Standalone MCP gateway — persistent connections to external MCP servers, with a
CLI to configure and test them. Ships with [Slife](https://github.com/juzcn/slife)
as its built-in MCP plugin but has **no dependency on it**. Depends only on
`fastmcp`, `mcp`, `httpx`, `json5`, and `credstore` (for OAuth token storage).

## Install

```bash
pip install mcp-plugin
# or bundled with Slife:
uv tool install git+https://github.com/juzcn/slife.git
```

Verify: `mcp-plugin list`

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
        "."
      ],
    },
    github: {
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
}
```

## CLI

| Command | Description |
|---------|-------------|
| `mcp-plugin` | Overview of configured servers |
| `mcp-plugin list` | All servers + their tools |
| `mcp-plugin list <server>` | Tools of one server |
| `mcp-plugin set <server>` | Interactive add/configure a server |
| `mcp-plugin remove <server>` | Remove a server from config (takes effect at next server start) |
| `mcp-plugin test` | Test every server |
| `mcp-plugin test <server>` | Test one server |

`set` accepts `--transport stdio|http`, `--command`, `--url`, `--args`,
`--env` (`KEY=VALUE`), `--enabled/--no-enabled`, and `--auth oauth` prompts.

## Plugin contract

An MCP-plugin distribution exposes a module that hosts its own FastMCP server
(transport: streamable HTTP on an auto-assigned port, port signaled to stdout as
`{"port": N}`). Slife discovers it via `plugins.external` in `slife.json5` and
spawns `python -m <module>`. The management tools (`mcp_set`, `mcp_remove`,
`mcp_set_enabled`, `mcp_list`, `mcp_list_tools`, `__mcp_call_tool`,
`__mcp_connection_status`) are kept separate from the servers they manage.