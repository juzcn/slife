# Slife

**Terminal-based AI agent** — a function-calling loop with minimum harness. Chat with an LLM that calls tools, remembers every turn, and orchestrates other agents.

```
You: "Find all TODO comments and create GitHub issues"
  → LLM calls search_content("TODO")
  → LLM calls github__create_issue(...) for each one
  → LLM: "Created 7 issues. All linked above."
```

One TUI window around an LLM tool loop: up to 50 native tools in 14 categories (plus 2 harness tools), five built-in plugin services, always-on memory with hybrid search, inline images, runtime model switching across three API backends, and an agent-to-agent mesh — everything presented to the LLM as uniform OpenAI-style function definitions.

Requires Python 3.13+. Runs on Windows (native & WSL), macOS, and Linux.

## Install

**Zero prerequisites.** The install script auto-installs uv, Node.js, and bun if needed. On WSL, Linux-native versions are installed (Windows executables cannot receive custom env vars via WSL interop). Mosquitto (only needed for the A2A MQTT mesh) is offered interactively.

### macOS / Linux / WSL

```bash
# Global
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh | bash
# China mainland
curl -fsSL https://gitee.com/juzcn/slife/raw/main/install.sh | bash
```

### Windows PowerShell

```powershell
# Global
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/install.ps1 | iex"
# China mainland
powershell -ExecutionPolicy Bypass -Command "irm https://gitee.com/juzcn/slife/raw/main/install.ps1 | iex"
```

### Try without installing

```bash
uvx --from git+https://github.com/juzcn/slife.git slife
```

### Update

Re-run the install script — it auto-preserves optional packages (llama-cpp-python, sentence-transformers) by diffing the previous venv and re-adding them.

### Uninstall

```bash
# macOS / Linux / WSL
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/uninstall.sh | bash
# China mainland
curl -fsSL https://gitee.com/juzcn/slife/raw/main/uninstall.sh | bash

# Windows PowerShell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/uninstall.ps1 | iex"
# China mainland
powershell -ExecutionPolicy Bypass -Command "irm https://gitee.com/juzcn/slife/raw/main/uninstall.ps1 | iex"
```

User data (`~/.slife/`, `~/.credstore/`) is **not removed** — delete manually for a full reset.

## Quick Start

```bash
credstore set-password              # first time — encrypted backup
credstore set DEEPSEEK_API_KEY      # store API key (masked input)
slife
```

To share the same API key across multiple providers:

```bash
credstore copy DEEPSEEK_API_KEY BAILIAN_API_KEY
```

## Configuration

Secrets in the OS keyring, config in JSON5:

| Layer | Storage | Contents |
|-------|---------|----------|
| **Secrets** | OS keyring (credstore) | API keys — encrypted at OS level, plus an encrypted cryptfile backup |
| **Config** | `~/.slife/slife.json5` | `${VAR}` references + non-secret values |

```json5
env: {
  DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}",   // → resolved from keyring at runtime
}

models: {
  providers: {
    deepseek: {
      base_url: "https://api.deepseek.com",
      api_key: "${DEEPSEEK_API_KEY}",
      api: "openai-completions",
      models: [{ model: "deepseek-v4-pro", name: "DeepSeek V4 Pro", reasoning: true }],
    },
  },
},
active_model: "deepseek/deepseek-v4-pro",
```

`${VAR:-default}` fallback syntax is supported. Secrets can also be referenced as `keyring:service/key` URIs.

**Three first-class API backends:**

| `api` field | Backend | Providers |
|-------------|---------|-----------|
| `openai-completions` | OpenAI / DeepSeek / Ollama | Chat Completions |
| `anthropic-messages` | Claude / Bailian (Qwen) | Messages |
| `openai-responses` | OpenAI | Responses |

Switch at runtime: `model_list` → `model_switch(ref="bailian/qwen3.8-max")`.

**Secrets never reach the LLM.** User input, tool-call arguments, and every tool result pass through a pattern-based sanitizer before entering the conversation — API key shapes (`sk-*`, `ghp_*`, Bearer tokens, …) are auto-masked.

## Features

### Tools

All unified as OpenAI function definitions. The LLM sees no difference between native, plugin, and external MCP tools.

**51 native tools in 14 categories** — auto-discovered from `slife/tools/` (up to 49 LLM-visible + 2 harness; `include_image` is dropped when the active model has no vision, and `install_python_package` is disabled by default in the shipped config):

| Category | Tools |
|----------|-------|
| System | `system_health`, `check_memdb`, `check_wechat`, `check_memfiles`, `check_mcp`, `check_a2a`, `check_watchdog` |
| Execution | `execute_shell`, `run_python_script`, `install_python_package` |
| Skills | `skill_list`, `skill_use`, `skill_set`, `skill_remove`, `skill_set_enabled` |
| CLI | `cli_list`, `cli_set`, `cli_remove`, `cli_set_enabled` |
| REST API | `rest_api_list`, `rest_api_set`, `rest_api_remove`, `rest_api_set_enabled` |
| A2A | `a2a_send_task`, `a2a_send_task_async`, `a2a_get_task_result`, `a2a_cancel_task`, `a2a_subscribe_task`, `a2a_list_agents`, `a2a_list_tasks`, `a2a_agent_card`, `a2a_broadcast` |
| Subagent | `spawn_subagent`, `list_subagents`, `stop_subagent`, `subagent_send_task`, `subagent_send_task_async`, `subagent_get_task_result`, `subagent_list_tasks`, `subagent_cancel_task` |
| Config | `config_env_set`, `config_env_get`, `config_env_remove`, `native_tool_set` |
| Models | `model_list`, `model_set`, `model_remove`, `model_switch` |
| Credentials | `credential_check`, `credential_inject`, `credential_uninject` |
| Vision | `include_image` (injects a local image or URL into the conversation) |
| Display | `show_image`, `notify_user` |
| Harness | `_sys_note` (context status), `_sys_trim` (context trim) — auto-invoked, not for LLM use |
| Meta | `list_tools`, `check_async`, `cancel_async`, `clear_context` |

Every tool additionally accepts three harness meta-parameters: `_timeout` (per-call override), `_async` (run in background, poll with `check_async`), and `_approve` (inline approval prompt in the chat — Y approve / N deny / Esc deny).

**Harness tools** come in two tiers. `_`-prefixed native tools (`_sys_note` / `_sys_trim`) are **LLM-visible but reserved**: the agent loop auto-invokes them each turn to maintain context state (report usage %, trim old turns when over the ceiling); they are schema-declared (so the Anthropic / OpenAI-Responses backends accept their call pairs) but the system prompt forbids the LLM from calling them, and both are harmless if it does anyway. `__`-prefixed plugin tools (`__memory_save_turn`, `__mcp_call_tool`, …) are **LLM-invisible** — filtered out of the schema entirely and called programmatically via `client.call_tool()`.

**Five managed categories** (Skills / CLI / REST API / Models / MCP) support `X_list` / `X_set` / `X_remove` (+ `X_set_enabled` where a toggle applies) — all `X_set` tools are idempotent upserts.

**Plugin tools** — registered at runtime as `{server}__{tool}` proxies:

| Server | LLM-visible tools |
|--------|-------------------|
| `mcp` | `mcp_set`, `mcp_set_enabled`, `mcp_remove`, `mcp_list`, `mcp_list_tools` |
| `memdb` | `memdb__memory_list_recent`, `memdb__memory_search`, `memdb__memory_open`, `memdb__memory_summarize`, `memdb__memory_count`, `memdb__memory_check_embedding`, `memdb__memory_set_embedding`, `memdb__memory_set_enabled` |
| `wechat` | `wechat_login`, `wechat_send_message`, `wechat_send_typing`, `wechat_check_messages`, `wechat_check_status`, `wechat_logout` |
| `memfiles` | `memfiles__expose_file`, `memfiles__save_content_or_files` |

Built-in plugin tools that already carry their server as a name prefix (`mcp_set`, `wechat_login`) are registered as-is; the rest are namespaced `{server}__{tool}`. External MCP servers configured in `slife.json5` → `mcp.servers` always appear as `{server}__{tool}` (e.g. `filesystem__read_file`).

### Memory — Always On

Every conversation turn is permanently recorded in SQLite (`~/.slife/<agent>.db`). Hybrid search across four modes:

| Mode | Best for |
|------|----------|
| `grep` | Exact strings — error messages, file paths, code |
| `fts5` | Topic / keyword search with ranked snippets |
| `hybrid` | Semantic recall (FTS5 + vector → RRF merge) |
| `time` | Browse by date |

Embedding backends: local GGUF (BGE-M3, offline), HuggingFace transformers, or OpenAI-compatible API. Keyword search works without any embedding backend. Semantic (hybrid) results are only served once the index is fully built for the current model — while a full reindex runs (new/changed model, restart mid-index), hybrid degrades to keyword-only and resumes automatically when indexing finishes.

Each turn also records two timestamps — the user's input time (`created_at`, the Enter-press moment) and the assistant's completion time (`completed_at`) — shown as dim `[HH:MM]` markers in the chat (user messages and assistant responses respectively). Databases created before `completed_at` are migrated once with `python scripts/migrate_memdb_completed_at.py` (no in-plugin ALTER); fresh databases get the column automatically.

### Autonomous Heartbeat

While idle, the agent gets a periodic autonomous window (every `agent.heartbeat_interval` seconds, default 60) to think or act on its own. It runs as a normal turn (own conversation, saved to memory); the reply contract is real content if it has something worth saying, otherwise a single `.` — the `.` and the `[Heartbeat]` trigger are filtered from the chat, and a real autonomous reply renders as `⚡ 自主`. A precondition for emergent self-initiated behavior.

### Image & Vision

Attach images with `@path` / `@url` syntax (quotes supported for paths with spaces), displayed inline in the terminal:

```
Check this screenshot @D:\Downloads\error.png
```

Two-tier rendering: **Sixel** (full-colour on Windows Terminal / WezTerm / iTerm2 / Kitty) → **HalfcellImage** (coloured Unicode half-blocks on any true-colour terminal) → text placeholder. Vision-capable models receive local files as base64 data URIs and HTTP(S) URLs as-is; the `include_image` tool lets the agent attach images mid-conversation, and `memfiles__expose_file` publishes any local file as a public HTTPS link via the ngrok tunnel (returns a graceful error while the tunnel is offline).

### Plugins

Five built-in plugins as independent child processes:

| Plugin | Role |
|--------|------|
| **slife-mcp** | Gateway for external MCP servers (stdio / SSE / Streamable HTTP) |
| **slife-memdb** | Diary database with hybrid search |
| **slife-wechat** | Bidirectional WeChat messaging |
| **slife-memfiles** | File cabinet + public file sharing over Streamable HTTP (`/share` route on the same port; ngrok tunnel owned by the plugin) |
| **slife-a2a** | A2A mesh channel over MQTT (only starts when the broker is reachable) |

External MCP servers configured in `slife.json5` → `mcp.servers` — any stdio, SSE, or Streamable HTTP MCP server works, no Slife SDK required. For `url`-configured servers, SSE is auto-detected and Streamable HTTP is the fallback; a Streamable response may arrive as a single JSON body or an SSE stream (both handled).

All plugins — built-in and auto-discovered third-party alike — run with a **watchdog** that auto-restarts them on crash (exponential backoff 1s→30s, max 5 restarts). The MCP wrapper watchdog also reconnects external servers after restart. Runtime health checks — `check_memdb`, `check_wechat`, `check_memfiles`, `check_mcp`, `check_a2a`, `check_watchdog` — monitor application-level state and are surfaced via `system_health`; the watchdog is purely process-level.

### A2A — Agent-to-Agent (mesh)

The A2A protocol (JSON-RPC operations and Message/Task/AgentCard data shapes mirroring the official a2a-python reference interface) runs over a pluggable transport **binding** — currently MQTT. The **`a2a` plugin** hosts the LLM-visible tools and the `A2AClient`, and only starts when the broker is reachable:
- **Mesh tools** (one uniform `a2a_` prefix): `a2a_send_task`, `a2a_send_task_async`, `a2a_get_task_result`, `a2a_cancel_task`, `a2a_subscribe_task`, `a2a_list_agents`, `a2a_list_tasks`, `a2a_agent_card`, `a2a_broadcast`.
- **Local workers** are NOT A2A: `spawn_subagent`, `list_subagents`, `stop_subagent`, `subagent_send_task`, `subagent_send_task_async`, `subagent_get_task_result`, `subagent_list_tasks`, `subagent_cancel_task`. A worker runs one task at a time; a sync send to a busy worker is auto-queued as async (task_id returned) and reported.

A2A's only implemented transport binding is MQTT — setting `transport` to any other value disables A2A with a warning instead of crashing startup. All messages — human, WeChat, MQTT, subagent results — flow through a single inbox queue and are processed one turn at a time.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+C` | Quit |
| `Esc` | Cancel agent loop |
| `Ctrl+S` | Switch model (inline picker — type a number, Esc cancels) |
| `Home` / `End` | Scroll to top / bottom |
| `Ctrl+Y` | Copy result (on a tool call) |
| `Enter` / `Space` | Toggle thinking block (on an assistant message) |

## CLI

| Flag | Description |
|------|-------------|
| `--agent <id>` | Agent identity — separate diary database + A2A mesh name (default: `slife`) |

## Optional Extras

| Extra | Enables |
|-------|---------|
| `slife[gguf]` | Local GGUF embeddings via llama-cpp-python (offline, ~300 MB) |
| `slife[transformer]` | HuggingFace transformer embeddings via sentence-transformers (~2 GB) |
| `slife[embeddings]` | Both of the above |

**Linux / macOS** — builds from source:

```bash
uv tool install "slife[gguf]" --reinstall
```

**Windows** — pre-built wheels (no C++ compiler needed); uv is configured to use the llama-cpp-python CPU wheel index. See [install docs](https://github.com/juzcn/slife#optional-extras) for wheel selection and first-use instructions.

## Development

```bash
git clone https://github.com/juzcn/slife.git
cd slife
uv sync --all-extras

uv run credstore set-password
uv run credstore set DEEPSEEK_API_KEY
uv run slife

# Tests
uv run pytest
uv run pytest --cov=slife --cov=credstore --cov-report=term-missing
```

Dev mode auto-detects (via `pyproject.toml` in CWD): data files stay in the project directory. Production installs use `~/.slife/`. CI runs the test suite on Ubuntu, macOS, and Windows with Python 3.13.

## Architecture

See **[DESIGN.md](DESIGN.md)** — philosophy, agent loop, tool system, plugin contract, MCP gateway, memory database, A2A mesh, credential security model, and full project structure.

Known issues and improvement proposals from the latest code review: **[REVIEW.md](REVIEW.md)**.

## License

MIT
