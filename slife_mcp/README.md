# slife-mcp

Standalone MCP proxy service — manages persistent connections to external MCP servers and exposes them through a single MCP endpoint.

## Quick Start

```bash
pip install slife-mcp

# Run — auto-detects mode:
slife-mcp                      # with slife.json5 → HTTP, without → stdio
slife-mcp --port 8888          # HTTP on custom port
slife-mcp --host 0.0.0.0 --port 9876  # HTTP on all interfaces
```

## How It Works

```
MCP clients ←→ slife-mcp ←→ external MCP servers (filesystem, search, fetch, …)
```

slife-mcp acts as a **proxy layer** between MCP clients and external MCP servers. Instead of each client managing its own server connections, slife-mcp maintains persistent connections centrally and exposes all tools through a single endpoint.

Key design decisions:

- **Raw JSON-RPC over subprocess pipes** — no `anyio`, no `ClientSession`. Avoids `TaskGroup` conflicts with FastMCP and keeps the implementation simple and debuggable.
- **Connection pooling** — servers are kept alive across requests. Disconnected servers can be reconnected at runtime without restarting slife-mcp.
- **Stderr capture** — when a server fails to connect, its stderr output is included in the error message so the LLM (or human operator) can understand and fix the problem.
- **Progressive disclosure** — servers default to eager mode (tools loaded at startup), but can be configured as lazy (connected, tools hidden) to keep the tool list lean. Switch between modes at runtime with `mcp_set_disclosure`.

This supports any MCP-compatible server, including:

- **Pre-built MCP servers** — filesystem, web search (Serper, Tavily), fetch, etc.
- **REST API servers** — [anyapi-mcp-server](https://github.com/quiloos39/anyapi-mcp-server) converts OpenAPI specs to MCP tools, making any REST API (GitHub, Jira, GitLab, Slack…) callable as tools.

## Configuration

Create `slife.json5` in your working directory:

```json5
{
  mcp: {
    // wrapper.url — where clients connect to slife-mcp
    wrapper: {
      url: "http://127.0.0.1:9876/mcp",
    },

    // External MCP servers to auto-connect at startup
    servers: {
      filesystem: {
        command: "npx",
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/allowed/path"],
      },
      serper: {
        command: "npx",
        args: ["-y", "serper-search-scrape-mcp-server"],
        env: {
          SERPER_API_KEY: "${SERPER_API_KEY}",
        },
      },
    },
  },
}
```

Environment variable references (`${VAR}` and `${VAR:-default}`) are resolved at runtime.

## Management Tools

Once running, clients can call these tools to manage connections:

| Tool | Description |
|------|-------------|
| `mcp_add_server` | Connect to an external MCP server (activate=false for lazy) |
| `mcp_remove_server` | Disconnect and remove a server |
| `mcp_list_servers` | List all servers with status, tool count, and active state |
| `mcp_check_server` | Check a single server's status and active flag |
| `mcp_list_tools` | List all tools from a server, even if inactive |
| `mcp_set_disclosure` | Switch between eager (tools loaded) and lazy (tools hidden) |
| `mcp_call_tool` | Call a tool on a connected server (arguments as JSON string) |
| `mcp_reload` | Reconnect one or all servers to refresh tool lists |

`mcp_call_tool` expects tool arguments as a JSON string:

```json
{
  "server": "filesystem",
  "tool_name": "read_file",
  "arguments": "{\"path\": \"/tmp/example.txt\"}"
}
```

### Progressive Disclosure

Servers default to eager mode — all tools loaded at startup. For servers with many tools, use `activate: false` when adding or `disclosure: "lazy"` in config:

```json5
servers: {
  "big-api": {
    command: "npx", args: [...],
    disclosure: "lazy",  // connect but don't load tools yet
  }
}
```

Lazy servers connect at startup but don't disclose tools. Clients browse tools with `mcp_list_tools({server: "big-api"})`, then call `mcp_set_disclosure({name: "big-api", disclosure: "eager"})` to load them.

## Transport Modes

| Mode | Trigger | Use case |
|------|---------|----------|
| HTTP | TTY (terminal) with `slife.json5` | Standalone service, shared by multiple clients |
| stdio | Piped stdin (child process) | Spawned by Slife agent as a subprocess |

**Auto-detection logic:**

1. If stdin is **not** a TTY (piped) → **stdio** mode — used when slife spawns slife-mcp as a child process.
2. If stdin **is** a TTY (terminal) → looks for `slife.json5` → reads `mcp.wrapper.url` for host/port → **HTTP** mode.
3. Use `--host` / `--port` CLI flags to override config values.

In HTTP mode, the server listens on the configured host:port and serves the MCP protocol at the `/mcp` path. Multiple MCP clients can connect simultaneously.

In stdio mode, the server reads/writes JSON-RPC messages on stdin/stdout — exactly one client (the parent process).

## Architecture

```
┌──────────┐     HTTP/stdio     ┌───────────────┐     JSON-RPC      ┌──────────────────┐
│  Client  │ ◄────────────────► │   slife-mcp   │ ◄───────────────► │  External MCP    │
│ (Slife)  │                    │   (FastMCP)   │     subprocess     │  servers          │
└──────────┘                    │               │                    │  ┌─ filesystem   │
                                │ ConnectionPool│                    │  ├─ serper       │
                                │  ├─ conn #1   │───────────────────│  ├─ fetch        │
                                │  ├─ conn #2   │                    │  └─ anyapi       │
                                │  └─ conn #3   │                    └──────────────────┘
                                └───────────────┘
```

- **`server.py`** — FastMCP server entry point with 8 management tools. Handles transport auto-detection and config parsing.
- **`connection.py`** — `MCPServerConnection` (per-server lifecycle: spawn, JSON-RPC handshake, tool discovery, call, disconnect) and `ConnectionPool` (collection management).

## Requirements

- Python ≥ 3.13
- `fastmcp` ≥ 2.0.0
- `json5` ≥ 0.15.0

## License

MIT
