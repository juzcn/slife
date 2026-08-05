# Slife

**Terminal-based AI agent** — a function-calling loop with minimum harness. Chat with an LLM that calls tools, remembers everything, and orchestrates other agents.

```
You: "Find all TODO comments and create GitHub issues"
  → LLM calls search_content("TODO")
  → LLM calls github__create_issue(...) for each one
  → LLM: "Created 7 issues. All linked above."
```

## Install

**Zero prerequisites.** The install script auto-installs uv, Node.js, and bun if needed.

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

Re-run the install script — it auto-preserves optional packages (llama-cpp-python, sentence-transformers).

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
credstore set DEEPSEEK_API_KEY       # store API key (masked input)
slife
```

To share the same API key across multiple providers:

```bash
credstore copy DEEPSEEK_API_KEY BAILIAN_API_KEY
```

## Configuration

Secrets in OS keyring, config in JSON5:

| Layer | Storage | Contents |
|-------|---------|----------|
| **Secrets** | OS keyring (credstore) | API keys — encrypted at OS level |
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

**Three first-class API backends:**

| `api` field | Backend | Providers |
|-------------|---------|-----------|
| `openai-completions` | OpenAI / DeepSeek / Ollama | Chat Completions |
| `anthropic-messages` | Claude / Bailian (Qwen) | Messages |
| `openai-responses` | OpenAI | Responses |

Switch at runtime: `list_models` → `switch_model(ref="bailian/qwen3.8-max")`.

**Secrets never reach the LLM.** All tool output is sanitized — API key patterns auto-masked.

## Features

### Tools

All unified as OpenAI function definitions. The LLM sees no difference between native and MCP tools.

**12 native categories** — auto-discovered from `slife/tools/`:

| Category | Tools |
|----------|-------|
| System | `system_health`, `check_embedding`, `check_wechat` |
| Execution | `execute_shell`, `run_python_script`, `install_python_package` |
| Skills | `list_skills`, `use_skill`, `add_skill`, `remove_skill`, `skill_set` |
| CLI | `cli_list_tools`, `cli_add_tool`, `cli_remove_tool`, `cli_set_tool` |
| Models | `list_models`, `add_model`, `remove_model`, `switch_model` |
| A2A | 13 tools — agent discovery, task routing, subagent lifecycle, broadcast |
| MemFiles | `save_content_or_files`, `expose_file`, `include_image` |
| Display | `show_image` |

**Five managed categories** (MCP / Skills / CLI / REST API / Native) support `list` / `add` / `remove` / `set` — all `add` tools are idempotent upserts.

Plus built-in **MemDB** tools: `memory_search`, `memory_open`, `memory_summarize`, `memory_count`, `memory_list_recent`, `memory_save_turn`, etc.

### Memory — Always On

Every conversation turn permanently recorded. Hybrid search across four modes:

| Mode | Best for |
|------|----------|
| `grep` | Exact strings — error messages, file paths, code |
| `fts5` | Topic / keyword search with ranked snippets |
| `hybrid` | Semantic recall (FTS5 + vector → RRF merge) |
| `time` | Browse by date |

Embedding backends: local GGUF (BGE-M3, offline), HuggingFace transformers, or OpenAI-compatible API. Keyword search works without any embedding backend.

### Image & Vision

Attach images with `@path` syntax, display inline in the terminal:

```
Check this screenshot @D:\Downloads\error.png
```

Two-tier rendering: **Sixel** (full-colour on Windows Terminal / WezTerm / iTerm2 / Kitty) → **HalfcellImage** (coloured Unicode half-blocks on all true-colour terminals). Vision-capable models receive images via HTTPS URLs through the memfiles tunnel — no base64 in context.

### Plugins

Four built-in plugins as independent processes:

| Plugin | Role |
|--------|------|
| **slife-mcp** | Gateway for external MCP servers (stdio + HTTP) |
| **slife-memdb** | Diary database with hybrid search |
| **slife-wechat** | Bidirectional WeChat messaging |
| **slife-memfiles** | File server + ngrok tunnel for vision APIs |

External MCP servers configured in `slife.json5` → `mcp.servers`. Any stdio or HTTP MCP server works — no Slife SDK required.

### A2A — Agent-to-Agent

Three transports, unified interface: **MQTT** (remote peers over Mosquitto), **HTTP Streamable** (direct agent-to-agent), **Subagent** (local child processes, always available). All messages flow through a single inbox queue.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+C` (in input) | Quit |
| `Ctrl+C` (elsewhere) | Copy |
| `Esc` | Cancel agent loop |
| `Ctrl+L` | Focus input |
| `Home` / `End` | Scroll to top / bottom |

## CLI

| Flag | Description |
|------|-------------|
| `--agent <id>` | Agent identity — memory isolation + A2A mesh name (default: `slife`) |

## Optional Extras

| Extra | Enables |
|-------|---------|
| `llama-cpp-python` | Local GGUF embeddings (offline, ~300 MB) |
| `sentence-transformers` | HuggingFace transformer embeddings (~2 GB) |

**Linux / macOS** — builds from source:

```bash
uv tool install "slife[gguf]" --reinstall
```

**Windows** — pre-built wheels (no C++ compiler needed). See [install docs](https://github.com/juzcn/slife#optional-extras) for wheel selection and first-use instructions.

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

Dev mode auto-detects: data files stay in the project directory. Production installs use `~/.slife/`.

## Architecture

See **[DESIGN.md](DESIGN.md)** — philosophy, agent loop, tool system, plugin contract, MCP gateway, memory database, A2A mesh, credential security model, and full project structure.

## License

MIT
