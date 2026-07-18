# Slife

**Terminal-based AI agent** — chat with an LLM that can execute shell commands, read and write files, search the web, call REST APIs, connect to MCP servers, spawn subagents for parallel work, communicate with other Slife instances over MQTT, and remember everything permanently.

```
┌────────────────────────────────────────────────────────────┐
│  Terminal UI (Textual)                                     │
│  ─────────────────────────────────────────────────────────  │
│  Agent Service — LLM + Tools + Loop + MCP + A2A + Inbox   │
│  ┌──────────┬─────────────┬──────────┬──────────────────┐  │
│  │ MCP Tool │ A2A + MQTT  │ Subagent │ Built-in Plugins │  │
│  │  Proxy   │ Mesh        │ Workers  │ ┌────┬────┬────┐ │  │
│  │          │             │          │ │MCP │Mem │WX  │ │  │
│  └──────────┴─────────────┴──────────┴─┴────┴────┴────┘─┘  │
│  Permanent Memory — hybrid search (grep + FTS5 + semantic)  │
└────────────────────────────────────────────────────────────┘
```

## Quick Start

```bash
uv sync                                      # install dependencies
uv run slife                                 # launch the TUI
```

The default config (`slife.json5`) ships with pre-configured MCP servers (filesystem, web fetch, DuckDuckGo search).  Store your API key and launch:

```bash
credstore set-password                        # first time only — sets up encrypted backup
credstore set DEEPSEEK_API_KEY               # masked input, no echo
uv run slife
```

## How It Works

Slife is a **function-calling loop**. You type a message → the LLM decides what tools to call → Slife executes them and returns results → the LLM responds → repeat.

```
You: "Find all TODO comments and create GitHub issues for them"
  → LLM calls execute_shell("rg TODO")
  → LLM calls github__create_issue(...) for each one
  → LLM: "Created 7 issues. All linked in the description above."
```

## Configuration

Slife uses a **two-layer** configuration model:

| Layer | Storage | What goes here |
|-------|---------|----------------|
| **Secrets** | OS keyring (credstore) | API keys, tokens, passwords — encrypted at OS level |
| **Config** | `slife.json5` → `env:` | `${VAR}` references + non-secret values (EDITOR, LANG, etc.) |

```json5
// slife.json5
env: {
  DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}",   // → resolved from keyring at runtime
  EDITOR: "code",                             // → plain value, no secret
}

models: {
  providers: {
    deepseek: {
      base_url: "https://api.deepseek.com",
      api_key: "${DEEPSEEK_API_KEY}",          // ← ${VAR} syntax throughout
      models: [{ model: "deepseek-v4-pro", name: "DeepSeek V4 Pro", reasoning: true }],
    },
  },
},
active_model: "deepseek/deepseek-v4-pro",
```

`${ENV_VAR}` and `${ENV_VAR:-default}` syntax works everywhere — values resolve at runtime via shell → keyring → config.

### Storing Secrets

Never paste API keys into `slife.json5` or the chat.  Use the terminal:

```bash
credstore set DEEPSEEK_API_KEY       # masked input — no echo, no shell history
```

The agent registers the reference for you — just say "add a DeepSeek key" and it calls `config_env_set`, which writes `${DEEPSEEK_API_KEY}` to the config and tells you to run the command above.

## Features

### Tools

All tools are unified as OpenAI function definitions — the LLM sees no difference between a native shell command, an MCP tool, or a REST API endpoint.

| Category | Examples | Location |
|----------|----------|----------|
| **Native** | `execute_shell`, `run_python_script`, `get_os_info` | `slife/tools/*.py` |
| **MCP / REST** | `filesystem__read_file`, `fetch__get`, `serper__search` | Via slife-mcp proxy |
| **Skills** | On-demand plugins with `list_skills` / `use_skill` | `skills/` directory |
| **CLI** | Auto-discovered external commands, persisted with `cli_add_tool` | Runtime registration |
| **A2A** | 13 protocol tools — discovery, routing, lifecycle, broadcast | `slife/tools/a2a.py` |

### Memory

Every conversation turn is permanently recorded.  Hybrid search (grep + FTS5 + semantic via vec0) lets the LLM recall past work.  Memory runs as a built-in plugin (`slife/plugins/memory/`) — a separate process so crashes never race with writes.

```
memory_search("ConnectionError")            → exact error trace
memory_search("MCP config", mode="fts5")    → topic search
memory_search("that bug fix", mode="hybrid")→ semantic recall
memory_search(mode="time", since="2026-07") → browse by date
```

User isolation via `--user alice`. Embedding via local GGUF (offline) or OpenAI-compatible API.  See [DESIGN.md § Permanent Memory](DESIGN.md#permanent-memory-slife-memory) for the full architecture.

### Plugins

Three built-in plugins ship with Slife, all using the same MCP stdio protocol:

| Plugin | Role |
|--------|------|
| **slife-mcp** | Proxy for external MCP servers (stdio + HTTP) — 10 management tools |
| **slife-memory** | Diary database with hybrid search (FTS5 + vec0 RRF) |
| **slife-wechat** | Bidirectional WeChat messaging via iLink ClawBot API |

**Third-party plugins** are standard MCP servers configured in `slife.json5` →
`mcp.servers`. They auto-connect on startup and their tools are discovered and
registered automatically. Any MCP-compatible server — in Python, Node.js, Go,
Rust, or any other language — works as a Slife plugin.

```json5
// Example: add a custom MCP server
mcp: {
  servers: {
    "my-plugin": {
      command: "uv", args: ["run", "python", "-m", "my_plugin.server"],
      env: { API_KEY: "${API_KEY}" },
      description: "My custom MCP server."
    },
  },
}
```

See [DESIGN.md § Third-Party Plugins](DESIGN.md#third-party-plugins) for the full plugin contract and configuration reference.

### A2A — Agent-to-Agent

Two transports, one interface: **MQTT** (remote peers, enable with `--agent <id>`) and **Subagent** (local child processes, always available).  The unified inbox serializes human keyboard, WeChat, MQTT, and subagent messages through a single queue — only one AgentLoop runs at a time.

### Progressive Disclosure

Not all tools are in every LLM request.  Three categories use lightweight summaries first:

| Category | Browse | Load |
|----------|--------|------|
| Memory | `memory_search` / `memory_list_recent` | `memory_open` |
| Skills | `list_skills` | `use_skill` |
| MCP | `mcp_list_servers` / `mcp_list_tools` | `mcp_set_disclosure("eager")` |

## Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+C` (in input) | Quit |
| `Ctrl+C` (elsewhere) | Copy (terminal-native) |
| `Esc` | Cancel agent loop |
| `Ctrl+L` | Focus input field |
| `Home` / `End` | Scroll to top / bottom |
| Any key | Auto-focus input + type |

## CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--agent <id>` | (off) | Enable A2A — join the MQTT mesh with this identity |
| `--user <id>` | `default` | Memory isolation key — separate diary per user |

## Requirements

- Python ≥ 3.13
- [uv](https://docs.astral.sh/uv/) — Python package manager
- Node.js — only if using npx-based MCP servers
- Windows + GGUF embeddings: see [DESIGN.md § Embeddings](DESIGN.md#embeddings-2) for pre-built wheel instructions

## Design

Slife is a **minimum-harness agent**.  The harness only does what the LLM physically cannot: execute tools, maintain conversation state, stream responses, and persist memory.  Everything else — reasoning, planning, tool selection, error recovery — is the LLM's job.

See [DESIGN.md](DESIGN.md) for the full architecture, component-level documentation, and design rationale.

## License

MIT
