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
cp slife.json5.example slife.json5           # copy + edit: set your API key
uv run slife                                 # launch the TUI
```

The example config includes three pre-configured MCP servers (filesystem, web fetch, DuckDuckGo search) — you're productive immediately after setting your model key.

## How It Works

Slife is a **function-calling loop**. You type a message → the LLM decides what tools to call → Slife executes them and returns results → the LLM responds → repeat.

```
You: "Find all TODO comments and create GitHub issues for them"
  → LLM calls execute_shell("rg TODO")
  → LLM calls github__create_issue(...) for each one
  → LLM: "Created 7 issues. All linked in the description above."
```

## Configuration

Edit `slife.json5`. The only required setting is a **provider + API key**:

```json5
models: {
  providers: {
    deepseek: {
      base_url: "https://api.deepseek.com",
      api_key: "${DEEPSEEK_API_KEY}",
      models: [
        { model: "deepseek-v4-pro", name: "DeepSeek V4 Pro", reasoning: true },
      ],
    },
  },
},
active_model: "deepseek/deepseek-v4-pro",
```

`${ENV_VAR}` and `${ENV_VAR:-default}` syntax is supported throughout — values are resolved at startup and injected into `os.environ`.

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

Third-party plugin auto-loading is on the roadmap — the infrastructure is ready.

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
| `Ctrl+C` | Quit |
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
