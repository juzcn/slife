# slife-mcp

Standalone MCP proxy service — manages persistent connections to external MCP servers and exposes them through a single MCP endpoint.

## Quick Start

```bash
pip install slife-mcp

# Run — auto-detects mode:
slife-mcp                      # with slife.json5 → HTTP, without → stdio
slife-mcp --port 8888          # HTTP on custom port
```

## How It Works

```
MCP clients ←→ slife-mcp ←→ external MCP servers (filesystem, search, ...)
```

slife-mcp maintains persistent connections to external MCP servers. Clients connect to slife-mcp and get access to all tools from all connected servers through a single endpoint.

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

## Management Tools

Once running, clients can call these tools to manage connections:

| Tool | Description |
|------|-------------|
| `mcp_add_server` | Connect to an external MCP server (activate=false for lazy) |
| `mcp_remove_server` | Disconnect and remove a server |
| `mcp_list_servers` | List all servers with status, tool count, and active state |
| `mcp_list_tools` | List all tools from a server, even if inactive |
| `mcp_check_server` | Check a single server's status and active state |
| `mcp_set_disclosure` | Switch between eager (tools loaded) and lazy (tools on demand) |
| `mcp_call_tool` | Call a tool on a connected server |
| `mcp_reload` | Reconnect to refresh tool lists |

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
| HTTP | TTY (terminal) | Standalone service, shared by multiple clients |
| stdio | Piped stdin | Child process, used by slife agent |

HTTP mode is auto-detected when run from a terminal with `slife.json5` present. stdio mode is used when spawned as a child process.

## License

MIT
