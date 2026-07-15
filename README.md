# Slife

**Terminal-based AI agent** — chat with an LLM that can execute shell commands, read and write files, search the web, call any REST API, connect to MCP servers, spawn subagents for parallel work, communicate with other Slife instances over MQTT, and remember everything permanently.

Think of it as a terminal-native AI pair programmer with a searchable long-term memory.

## Quick Start

```bash
# 1. Install dependencies
uv sync

# 2. Copy and edit config — set your LLM provider's API key
cp slife.json5.example slife.json5

# 3. Run
uv run slife
```

The example config ships with three pre-configured MCP servers (filesystem, web fetch, DuckDuckGo search) that need no API keys — you're productive immediately after setting your model key.

## How It Works

Slife is a **function-calling loop**. You type a message → the LLM decides what tools to call → Slife executes them and returns results → the LLM responds → repeat. There's no orchestration, no hardcoded workflows, no guardrails. The LLM is in control.

```
You: "Find all TODO comments and create GitHub issues for them"
  → LLM calls execute_shell("rg TODO")
  → LLM calls github__create_issue(...) for each one
  → LLM: "Created 7 issues. All linked in the description above."
```

Everything the agent encounters — files, web pages, API responses, errors — is recorded in an immutable, searchable diary. Over time, this becomes a knowledge base of everything you and the agent have worked on together.

## Configuration

Edit `slife.json5`. See `slife.json5.example` for the full annotated template.

The only required setting is a **provider + API key**:

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

`${ENV_VAR}` and `${ENV_VAR:-default}` syntax is supported throughout the config — values are resolved from the environment at startup and injected into `os.environ` so MCP servers and tools can reference them.

## Tools

All tools are unified as OpenAI function definitions — the LLM sees no difference between a native shell command, an MCP tool, or a REST API endpoint. Tools are auto-discovered from `slife/tools/` at startup; use `slife.json5` only to override defaults or disable individual tools.

### Native Tools

| Tool | Description |
|------|-------------|
| `execute_shell` | Execute a shell command with configurable timeout |
| `run_python_script` | Platform-correct Python invocation with JSON arguments |
| `get_os_info` | Return current OS: Windows, Linux, or macOS |
| `config_env_set` / `get` / `remove` | Manage environment variables in slife.json5 + os.environ |
| `cli_add_tool` / `check_installed` / `remove` / `list` | Register, discover, and manage external CLI tools |

### MCP & REST APIs

External MCP servers connect through [slife-mcp](https://pypi.org/project/slife-mcp/) — an **independent proxy process** that manages persistent server connections. Tools are prefixed by server name (e.g. `filesystem__read_file`).

REST APIs connect via [anyapi-mcp-server](https://github.com/quiloos39/anyapi-mcp-server), which converts any OpenAPI spec to callable tools at runtime — no per-endpoint code needed.

Add servers at runtime with `mcp_add_server` or pre-configure them in `slife.json5`. Servers default to **eager** mode (tools loaded at startup). For servers with many tools, use `disclosure: "lazy"` — they connect but don't register tools until the LLM explicitly requests them.

### Skills

On-demand documentation plugins under `skills/`. The LLM loads them only when needed via `list_skills` → `use_skill`. Each skill is a directory with a `SKILL.md` file. Install new skills at runtime with `add_skill` / `remove_skill`.

### CLI Tools

External CLI commands the LLM discovers and registers for future use. After successfully using a new command, the LLM persists it with `cli_add_tool` — it survives restarts. The tools themselves don't execute commands; they manage the discovery registry. Actual execution goes through `execute_shell`.

### Memory & Knowledge Base

Every turn — user message, assistant thinking, tool calls and their outputs, file contents, web pages, API responses, errors — is permanently recorded as an independent, immutable row. No sessions, no lifecycle. Memory is a continuous time-ordered log.

Memory runs as an **independent MCP service** (`slife-memory`), same architecture as slife-mcp. If Slife crashes, turns already saved are safe — no data loss.

**LLM tools:**

| Tool | Description |
|------|-------------|
| `memory_count` | How much you know — total, by time range, or by search query |
| `memory_search` | Four modes: grep, fts5, hybrid, time |
| `memory_list_recent` | Browse recent turns |
| `memory_open` | Load a turn's full messages by rowid |
| `memory_summarize` | Annotate a turn with summary and tags |

**Search modes:**

| Mode | Backend | Use for |
|------|---------|---------|
| `grep` | SQLite LIKE | Exact strings: error messages, code snippets, file paths |
| `fts5` | FTS5 + BM25 | Topic and keyword search |
| `hybrid` | FTS5 + vec0 KNN → RRF | Semantic + keyword fusion |
| `time` | SQLite range scan | Browse by date — when you know when but not what |

**Embedding:** Full turn text (user message + assistant + tool results) is embedded via a configurable backend (local GGUF or OpenAI API). If the text exceeds the model's token limit, embedding is skipped for that turn — keyword search still works, only semantic search misses it. No truncation.

Every restart automatically restores recent turns.

```bash
slife --user alice    # alice's knowledge
slife --user bob      # bob's knowledge (isolated)
slife                 # default user
```

See [DESIGN.md](DESIGN.md#permanent-memory-slife-memory) for the full memory architecture.

## Agent-to-Agent (A2A)

Slife instances can communicate — delegate tasks, share results, coordinate work.

**Two transports, one interface:**

| Transport | Enable | Use case |
|-----------|--------|----------|
| **MQTT** | `--agent <id>` | Remote Slife instances — P2P mesh over MQTT |
| **Subagent** (stdin/stdout) | Always available | Local child processes for parallel work |

Start with `--agent my-agent` to join the MQTT mesh. Remote tasks from other agents stream to your chat view just like locally-typed messages, with the source agent's name as the prompt prefix.

The full A2A protocol toolset includes agent discovery, task routing (sync/async), task lifecycle management, broadcast (scatter/gather), and desktop notifications — 13 tools, all auto-discovered.

Subagents are **ephemeral** — they live only while the parent Slife process is running. When Slife exits, all subagents are terminated. On restart, spawn new ones with `a2a_spawn_subagent`. Remote MQTT peers are likewise discovered fresh on each run — there is no persisted agent registry.

## Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+C` | Quit the application |
| `Ctrl+L` | Clear conversation and start a fresh diary entry |
| `Esc` | Focus the input field |

## CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--agent <id>` | (off) | Enable A2A — join the MQTT mesh with this identity |
| `--user <id>` | `default` | Memory isolation key — separate diary namespace per user |

## Project Structure

```
slife/
  agent/            # LLM client, conversation, function-calling loop, inbox
  a2a/              # A2A: MQTT client, broker lifecycle, identity, TaskStore
  subagent/         # Subagent: spawn, JSON-RPC IPC, process management
  tools/            # All tools — native, skills, CLI, A2A (auto-discovered)
  mcp/              # MCP client (slife side)
  ui/               # Textual TUI
slife_mcp/          # Independent MCP proxy (pip install slife-mcp)
slife_memory/       # Independent memory MCP service (pip install slife-memory)
skills/             # On-demand skill plugins
tests/              # pytest suite
```

## Requirements

- Python ≥ 3.13
- [uv](https://docs.astral.sh/uv/) — Python package manager
- Node.js — only if using npx-based MCP servers

## Design

slife is a **minimum-harness agent**. The harness only does what the LLM physically cannot: execute tools, maintain conversation state, stream responses, and persist memory. Everything else — reasoning, planning, tool selection, error recovery — is the LLM's job.

See [DESIGN.md](DESIGN.md) for the full architecture, design rationale, and component-level documentation.

## License

MIT
