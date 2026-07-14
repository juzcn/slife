# slife

Terminal-based AI agent — a function-calling loop with minimum harness. Chat with an LLM that can execute shell commands, search the web, load on-demand skills, connect to MCP servers, call any REST API via OpenAPI specs, spawn subagents for parallel work, and communicate with other slife instances over MQTT.

## Quick Start

```bash
uv sync
cp slife.json5.example slife.json5
# Edit slife.json5 — set your LLM provider's API key
uv run slife
```

The example config includes three pre-configured MCP servers (filesystem, fetch, duckduckgo-search) that need no API keys — you're ready to go after setting your model key.

## Configuration

Edit `slife.json5`. See `slife.json5.example` for the full annotated template — it covers models, env vars, MCP servers, and the commented github REST API template. The minimum you need:

- **Provider + API key** — set `models.providers.<name>.api_key` via `${ENV_VAR}` or inline
- **Active model** — `active_model: "provider/model-id"`
- **MCP servers** — pre-configured with filesystem, fetch, and duckduckgo-search (no auth needed)

## Tools

All tools are unified as OpenAI function definitions — the LLM sees no difference between them.

### Native Functions

Auto-discovered from `slife/tools/`. Use `slife.json5` only to override defaults or disable a tool.

| Tool | What it does |
|------|-------------|
| `execute_shell` | Execute a shell command with configurable timeout |
| `run_python_script` | Platform-correct Python invocation with JSON args |
| `get_os_info` | Return current OS: Windows, Linux, or macOS |
| `config_env_set` / `get` / `remove` | Manage env vars in slife.json5 + os.environ |
| `cli_add_tool` / `check_installed` / `remove` / `list` | Register, check, and discover external CLIs |

### Skills

On-demand documentation plugins under `skills/`. The agent loads them only when needed via `list_skills` / `use_skill`. Each skill is a directory with a `SKILL.md` file. Install new skills at runtime with `add_skill` / `remove_skill`.

### MCP & REST APIs

External MCP servers connect through [slife-mcp](https://pypi.org/project/slife-mcp/) — an independent proxy that manages persistent connections. Tools are prefixed by server name (e.g. `filesystem__read_file`). REST APIs connect the same way via [anyapi-mcp-server](https://github.com/quiloos39/anyapi-mcp-server), which converts any OpenAPI spec to callable tools at runtime.

Add servers at runtime with `mcp_add_server` or pre-configure them in `slife.json5` → `mcp.servers`. Servers default to eager mode (all tools loaded at startup). For servers with many tools, use `disclosure: "lazy"` — the server connects but tools load on demand via `mcp_set_disclosure`, keeping context lean.

### A2A — Agent-to-Agent

Two transports, one interface. The full A2A protocol toolset (14 tools) provides discovery, task routing, lifecycle management, and notifications:

| Tool | Role |
|------|------|
| `a2a_list_agents` | Discover remote MQTT peers |
| `a2a_list_subagents` | List local subagent workers |
| `a2a_send_task` | Send a task and wait for the result (sync) |
| `a2a_send_task_async` | Fire-and-forget, returns task ID for polling |
| `a2a_get_task_result` | Poll task status and result from TaskStore |
| `a2a_list_tasks` | List all tasks with status/agent/transport filters |
| `a2a_cancel_task` | Cancel a pending or in-flight task |
| `a2a_subscribe_task` | Block until a task completes (push or poll) |
| `a2a_push_notification` | Request push delivery of async results |
| `a2a_agent_card` | Introspect a specific agent's status |
| `a2a_spawn_subagent` | Create a local worker with the same LLM + tools |
| `a2a_stop_subagent` | Stop a locally-managed subagent |
| `a2a_notify_user` | Fire a desktop notification to the human operator |
| `a2a_broadcast` | Scatter/gather — send a task to all known agents |

| Transport | Enable | Use case |
|---|---|---|
| **MQTT** | `--name <id>` CLI flag | Remote slife instances (P2P mesh) |
| **Subagent** (stdin/stdout) | Always available | Local child processes for parallel work |

Start with `--name my-agent` to join the MQTT mesh; subagents are always available.

## Tips

- **`/file image.png`** — attach an image for vision models
- **`/exit`** — quit the application
- **`Ctrl+L`** — clear the conversation
- **`Esc`** — focus the input field

## Design

slife is a **minimum-harness agent**. The harness only does what the LLM cannot: execute tools, maintain conversation state, and stream responses. The system prompt contains only project-specific facts not in the LLM's training data. See [DESIGN.md](DESIGN.md) for the full rationale and architecture.

## Project Structure

```
slife/
  agent/            # LLM client, conversation, function-calling loop, inbox
  a2a/              # A2A: MQTT client, broker lifecycle, identity, TaskStore
  subagent/         # Subagent: spawn, JSON-RPC IPC, process management
  tools/            # All tools — native, skills, CLI, A2A (auto-discovered)
    a2a.py          #   14 A2A protocol tools (unified transports)
  mcp/              # MCP client (slife side)
  ui/               # Textual TUI
slife_mcp/          # Independent MCP proxy (pip install slife-mcp)
skills/             # On-demand skill plugins
tests/              # pytest suite (583 tests)
```

## Requirements

- Python ≥ 3.13
- `uv` (Python package manager)
- Node.js (only if using npx-based MCP servers)

## License

MIT
