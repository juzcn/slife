# Slife

**Terminal-based AI agent** — a function-calling loop with minimum harness. Chat with an LLM that calls tools, remembers every turn, and orchestrates other agents.

```
You: "Find all TODO comments and create GitHub issues"
  → LLM calls search_content("TODO")
  → LLM calls github__create_issue(...) for each one
  → LLM: "Created 7 issues. All linked above."
```

One TUI window around an LLM tool loop: 54 native tools in 12 categories, external MCP servers, always-on memory with hybrid search, inline images, runtime model switching across three API backends, and an agent-to-agent mesh — everything presented to the LLM as uniform OpenAI-style function definitions.

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

Switch at runtime: `list_models` → `switch_model(ref="bailian/qwen3.8-max")`.

**Secrets never reach the LLM.** User input, tool-call arguments, and every tool result pass through a pattern-based sanitizer before entering the conversation — API key shapes (`sk-*`, `ghp_*`, Bearer tokens, …) are auto-masked.

## Features

### Tools

All unified as OpenAI function definitions. The LLM sees no difference between native and MCP tools.

**54 native tools in 12 categories** — auto-discovered from `slife/tools/`:

| Category | Tools |
|----------|-------|
| System | `system_health`, `check_memdb`, `check_wechat`, `check_memfiles`, `check_mcp` |
| Execution | `execute_shell`, `run_python_script`, `install_python_package` |
| Skills | `list_skills`, `use_skill`, `add_skill`, `remove_skill`, `skill_set`, `check_skills_dir` |
| CLI | `cli_list_tools`, `cli_add_tool`, `cli_remove_tool`, `cli_set_tool`, `cli_check_installed` |
| REST API | `rest_api_list`, `rest_api_add`, `rest_api_remove`, `rest_api_set` |
| A2A | 13 tools — agent discovery, task routing, subagent lifecycle, broadcast |
| Config | `config_env_set`, `config_env_get`, `config_env_remove`, `native_tool_set` |
| Models | `list_models`, `add_model`, `remove_model`, `switch_model`, `switch_to_nvidia_free` |
| Credentials | `credential_check`, `inject_credential`, `uninject_credential` |
| MemFiles | `save_content_or_files`, `expose_file`, `include_image` |
| Display | `show_image` |
| Meta | `list_tools`, `check_async`, `cancel_async`, `clear_context` |

Every tool additionally accepts two harness meta-parameters: `_timeout` (per-call override) and `_async` (run in background, poll with `check_async`).

**Five managed categories** (MCP / Skills / CLI / REST API / Models) support `list` / `add` / `remove` / `set` — all `add` tools are idempotent upserts.

Plus built-in **MemDB** tools: `memory_search`, `memory_open`, `memory_summarize`, `memory_count`, `memory_list_recent`, `memory_check_embedding`, `memory_set_embedding`, `memory_set_enabled`.

### Memory — Always On

Every conversation turn is permanently recorded in SQLite (`~/.slife/<agent>.db`). Hybrid search across four modes:

| Mode | Best for |
|------|----------|
| `grep` | Exact strings — error messages, file paths, code |
| `fts5` | Topic / keyword search with ranked snippets |
| `hybrid` | Semantic recall (FTS5 + vector → RRF merge) |
| `time` | Browse by date |

Embedding backends: local GGUF (BGE-M3, offline), HuggingFace transformers, or OpenAI-compatible API. Keyword search works without any embedding backend.

### Image & Vision

Attach images with `@path` / `@url` syntax (quotes supported for paths with spaces), displayed inline in the terminal:

```
Check this screenshot @D:\Downloads\error.png
```

Two-tier rendering: **Sixel** (full-colour on Windows Terminal / WezTerm / iTerm2 / Kitty) → **HalfcellImage** (coloured Unicode half-blocks on any true-colour terminal) → text placeholder. Vision-capable models receive local files as base64 data URIs and HTTP(S) URLs as-is; the `include_image` tool lets the agent attach images mid-conversation, and `expose_file` publishes any local file as a public HTTPS link via the ngrok tunnel.

### Plugins

Four built-in plugins as independent child processes:

| Plugin | Role |
|--------|------|
| **slife-mcp** | Gateway for external MCP servers (stdio + HTTP) |
| **slife-memdb** | Diary database with hybrid search |
| **slife-wechat** | Bidirectional WeChat messaging |
| **slife-memfiles** | File server + ngrok tunnel (free tier: 1 agent — only the first agent gets the tunnel) |

External MCP servers configured in `slife.json5` → `mcp.servers`. Any stdio or HTTP MCP server works — no Slife SDK required. Per-server option `require_approval: true` adds a human approval gate before each of its tool calls.

All plugins run with a **watchdog** that auto-restarts them on crash (exponential backoff 1s→30s, max 3 retries). The MCP wrapper watchdog also reconnects external servers after restart. Runtime health checks — `check_memdb`, `check_wechat`, `check_memfiles`, `check_mcp` — monitor application-level state and are surfaced via `system_health`; the watchdog is purely process-level.

### A2A — Agent-to-Agent

Two working transports plus local workers, unified behind one tool surface: **MQTT** (remote peers over a Mosquitto broker — presence, heartbeat, task routing), **Subagent** (local child-process workers over JSON-RPC, always available), and an experimental **HTTP Streamable** transport. All messages — human, WeChat, MQTT, subagent results — flow through a single inbox queue and are processed one turn at a time.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+C` | Quit |
| `Esc` | Cancel agent loop |
| `Ctrl+L` | Focus input |
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
