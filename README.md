# Slife

**Terminal-based AI agent** — a function-calling loop with minimum harness. Chat with an LLM that calls tools, remembers everything, and orchestrates other agents over MQTT.

```
┌─────────────────────────────────────────────────────────────┐
│  Terminal UI (Textual)                                      │
│  ────────────────────────────────────────────────────────── │
│  Agent Loop — LLM + Tools + Stream + Memory + A2A + Inbox  │
│  ┌───────────┬──────────┬───────────┬─────────────────────┐ │
│  │ MCP Proxy │ A2A Mesh │ Subagents │ Built-in Plugins    │ │
│  │ (gateway) │ (MQTT)   │ (workers) │ Memory·MCP·WX·Media │ │
│  └───────────┴──────────┴───────────┴─────────────────────┘ │
│  Permanent Memory — hybrid search (grep + FTS5 + semantic)  │
│  Credstore — OS keyring + AES cryptfile backup              │
│  Win · Mac · Linux (SecretService / keyutils) · WSL         │
└─────────────────────────────────────────────────────────────┘
```

## Install

**Zero prerequisites.** The install script auto-installs uv and Node.js if needed.

### Install script (recommended)

**macOS / Linux / WSL:**

```bash
# GitHub (global)
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh | bash

# Gitee  (China mainland)
curl -fsSL https://gitee.com/juzcn/slife/raw/main/install.sh | bash
```

**Windows PowerShell:**

```powershell
# GitHub (global)
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/install.ps1 | iex"

# Gitee (China mainland)
powershell -ExecutionPolicy Bypass -Command "irm https://gitee.com/juzcn/slife/raw/main/install.ps1 | iex"
```

> 💡 The script auto-falls back to Gitee if GitHub is unreachable.  China mainland users can also download directly from Gitee to skip the GitHub probe entirely.

### Try without installing

```bash
uvx --from git+https://github.com/juzcn/slife.git slife
```

### Update

Re-run the install script — it auto-preserves optional packages (llama-cpp-python, sentence-transformers) from the previous install.

### Uninstall

```bash
# macOS / Linux / WSL
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/uninstall.sh | bash

# Gitee (China mainland)
curl -fsSL https://gitee.com/juzcn/slife/raw/main/uninstall.sh | bash

# Windows PowerShell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/uninstall.ps1 | iex"

# Gitee (China mainland)
powershell -ExecutionPolicy Bypass -Command "irm https://gitee.com/juzcn/slife/raw/main/uninstall.ps1 | iex"
```

Uninstall removes the binaries. User data (`~/.slife/`, `~/.credstore/`) is listed but **not removed** — delete manually for a full reset.

## Quick Start

```bash
credstore set-password              # first time only — encrypted backup
credstore set DEEPSEEK_API_KEY       # store API key (masked input)
slife
```

**Sharing keys across providers:** if multiple services use the same API key, copy it instead of re-entering:

```bash
credstore copy DEEPSEEK_API_KEY BAILIAN_API_KEY
credstore copy DEEPSEEK_API_KEY ANTHROPIC_AUTH_TOKEN
```

The default config ships with pre-configured MCP servers: filesystem + shell, code search, web fetch, search APIs.

## How It Works

Slife is a **function-calling loop**: you type → the LLM decides what tools to call → Slife executes them → the LLM responds → repeat.

```
You: "Find all TODO comments and create GitHub issues for them"
  → LLM calls search_content("TODO")
  → LLM calls github__create_issue(...) for each one
  → LLM: "Created 7 issues. All linked above."
```

Every turn is permanently recorded. On restart, recent conversations are restored.

## Configuration

Two-layer model — secrets in OS keyring, config in JSON5:

| Layer | Storage | Contents |
|-------|---------|----------|
| **Secrets** | OS keyring (credstore) | API keys, tokens — encrypted at OS level |
| **Config** | `~/.slife/slife.json5` → `env:` | `${VAR}` references + non-secret values |

Credstore supports **Windows** (Credential Manager), **WSL** (PowerShell bridge to Windows CredMan), **macOS** (Keychain), **Linux desktop** (D-Bus SecretService), and **headless Linux** (kernel keyutils). No configuration needed — the best available backend is auto-selected.

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
    // Bailian token-plan — Anthropic protocol
    // bailian: {
    //   base_url: "https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic",
    //   api_key: "${BAILIAN_API_KEY}",
    //   api: "anthropic-messages",
    //   models: [{ model: "qwen3.8-max", name: "Qwen3.8 Max", reasoning: true, input: ["text","image"], context_window: 983616, max_tokens: 131072 }],
    // },
  },
},
active_model: "deepseek/deepseek-v4-pro",
```

**Three first-class API backends** — no conversion layer, each works natively:

| `api` field | Backend | Providers |
|-------------|---------|-----------|
| `openai-completions` | OpenAI / DeepSeek / Ollama | Chat Completions endpoint |
| `anthropic-messages` | Claude / Bailian (Qwen) | Messages endpoint |
| `openai-responses` | OpenAI | Newer Responses endpoint |

Switch models at runtime with native tools:
```
list_models                    → see all configured models
switch_model(ref="bailian/qwen3.8-max")  → switch active model
add_model(provider="...", model="...", ...)  → register a new model
```

**Secrets never reach the LLM context.** All tool output is sanitized before reaching the model — API key patterns are auto-masked.

## Features

### Tools

All unified as OpenAI function definitions. The LLM sees no difference between categories.

**Nine native categories** — one `.py` file per category in `slife/tools/`, auto-discovered:

| Category | File | Tools |
|----------|------|-------|
| System | `system.py` | `check_embedding`, `check_wechat`, `system_health`, `check_mcp_servers` |
| Execution | `exec.py` | `execute_shell`, `run_python_script`, `install_python_package` |
| Skills | `skill.py` | `check_skills_dir`, `list_skills`, `use_skill`, `add_skill`, `remove_skill`, `skill_set` |
| CLI | `cli.py` | `cli_check_installed`, `cli_add_tool`, `cli_remove_tool`, `cli_list_tools`, `cli_set_tool` |
| REST API | `rest_api.py` | `rest_api_add`, `rest_api_remove`, `rest_api_list`, `rest_api_set` |
| A2A | `a2a.py` | 13 tools — agent discovery, task routing, subagent lifecycle, broadcast |
| Config | `config.py` | `config_env_set`, `config_env_get`, `config_env_remove`, `native_tool_set` |
| Models | `models.py` | `list_models`, `add_model`, `remove_model`, `switch_model` |
| Credentials | `credentials.py` | `credential_check`, `inject_credential`, `uninject_credential` |
| Meta | `meta.py` | `list_tools`, `check_async`, `cancel_async`, `clear_context` |
| Display | `meta.py` | `prepare_image` — load & prepare images, serve via ngrok for vision |

**Five managed categories** (MCP / Skills / CLI / REST API / Native) support a standard **list / add / remove / set** surface. All `add` tools are idempotent upserts. `mcp_set_server` additionally supports `disclosure="lazy"|"eager"` for tool lazy-loading.

Plus built-in **Memory** plugin (`memory_search`, `memory_open`, …).

### Image & Vision

Inline image rendering via ``textual-image`` with a two-tier strategy: **Sixel**
(full-colour) on whitelisted terminals (Windows Terminal, WezTerm, iTerm2, Kitty),
**HalfcellImage** (coloured Unicode half-block characters) everywhere else,
and a text placeholder as final fallback. Users attach images with `@path` syntax;
the agent can display images with the `prepare_image` tool:

```
Check this screenshot @D:\\Downloads\\error.png and tell me what's wrong
```

``prepare_image`` supports both **local files** and **remote URLs**.
In either case the image is cached and written as a BLOB to the ``diary_images``
SQLite table — the single source of truth for image data in permanent memory.

**URL injection (no base64).** Images are served to vision-capable LLMs via a
dedicated **media server** — a lightweight plain-HTTP plugin that reads BLOBs
from SQLite and returns raw image bytes. An **ngrok tunnel** exposes the media
server to the public internet, so LLM APIs (OpenAI, Anthropic, NVIDIA NIM)
fetch images as lightweight ``https://`` URLs instead of inline base64 data URIs.
When the tunnel is not active, image injection is silently skipped — no base64
ever enters the LLM context.

The ``prepare_image`` tool returns the public URL directly, so any vision-capable
tool (e.g. NVIDIA NIM VLM, browser screenshot tools) can consume it:

### Memory — Always On

Every conversation turn and displayed image is permanently recorded (including BLOBs in ``diary_images``). Saves unconditionally — even on cancel or error. Hybrid search across four modes:

| Mode | Best for |
|------|----------|
| `grep` | Exact strings — error messages, file paths, code |
| `fts5` | Topic / keyword search with ranked snippets |
| `hybrid` | Semantic recall (FTS5 + vec0 vector search, RRF merge) |
| `time` | Browse by date |

Embedding backends: local GGUF (BGE-M3, ~300 MB, offline), HuggingFace transformers, or OpenAI-compatible API. Keyword search works without any embedding backend.

### Plugins

**Four built-in plugins** run as independent processes on Streamable HTTP or plain HTTP transport:

| Plugin | Transport | Role |
|--------|-----------|------|
| **slife-mcp** | Streamable HTTP | Gateway for external MCP servers (stdio + HTTP) |
| **slife-memory** | Streamable HTTP | Diary database with hybrid search |
| **slife-wechat** | Streamable HTTP | Bidirectional WeChat via iLink ClawBot |
| **slife-media** | Plain HTTP | Serves image BLOBs via ngrok tunnel for vision APIs |

External MCP servers (filesystem, fetch, search, any OpenAPI spec) are configured in `slife.json5` and auto-connected at startup. Third-party MCP servers need no Slife SDK — any stdio or HTTP MCP server works.

### A2A — Agent-to-Agent

Two transports, one interface:

| Transport | Use case |
|-----------|----------|
| **MQTT** | Remote peers over Mosquitto broker |
| **HTTP Streamable** | Direct agent-to-agent |
| **Subagent** | Local child processes (always available) |

Unified inbox serializes human, WeChat, MQTT, and subagent messages through a single queue.

### Progressive Disclosure

Not all tools are in every request. Three categories use lightweight summaries before loading full tool schemas:

| Category | Browse | Load |
|----------|--------|------|
| Memory | `memory_search` | `memory_open` |
| Skills | `list_skills` | `use_skill` |
| MCP | `mcp_list_servers` / `mcp_list_tools` | `mcp_set_server(enabled=True)` / `mcp_set_server(disclosure="eager")` |

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
| `--agent <id>` | Agent identity — memory isolation key + A2A mesh name (default: `slife`) |

## Optional Extras

| Extra | Enables |
|-------|---------|
| `slife[gguf]` | Local GGUF embeddings (offline) |
| `slife[transformer]` | HuggingFace transformer embeddings (~2 GB) |
| `slife[embeddings]` | Both of the above |

**Linux / macOS** — builds from source (C compiler is standard on these platforms):

```bash
uv tool install "slife[gguf]" --reinstall
```

**Windows** — no C++ compiler by default.  Install the pre-built wheel directly into slife's venv (does NOT reinstall slife):

Pick the wheel that matches your setup:

| Your setup | Wheel | Notes |
|------------|-------|-------|
| No compiler, any GPU or none | `v0.3.34-vulkan` | Safest — uses Vulkan if GPU present, falls back to CPU |
| NVIDIA GPU + CUDA 12 | `v0.3.34-cu132` | CUDA 12.x |
| NVIDIA GPU + CUDA 11 | `v0.3.34-cu125` | CUDA 11.x |
| AMD GPU | `v0.3.34-hip-radeon` | ROCm |

```powershell
$py=((uv tool list --show-paths 2>$null|sls 'slife v'|Out-String)-replace'.*\((.*?)\).*','$1\Scripts\python.exe').Trim();uv pip install --python $py "llama-cpp-python @ https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.34-vulkan/llama_cpp_python-0.3.34-py3-none-win_amd64.whl"
```

**First use** — download a GGUF model and enable it:

```bash
curl -LO https://huggingface.co/ChristianAzinn/bge-m3-gguf/resolve/main/bge-m3-Q4_K_M.gguf
```

Then launch slife and tell it: `enable local embeddings with bge-m3-Q4_K_M.gguf`

## Development

```bash
git clone https://github.com/juzcn/slife.git        # or: https://gitee.com/juzcn/slife.git (China mainland)
cd slife
uv sync --all-extras

uv run credstore set-password        # first time
uv run credstore set DEEPSEEK_API_KEY
uv run slife
```

Dev mode auto-detects: data files stay in the project directory. Production installs use `~/.slife/`.

```bash
# Run tests
uv run pytest

# With coverage
uv run pytest --cov=slife --cov=credstore --cov-report=term-missing
```

## Architecture

Slife is a **minimum-harness agent**. The harness only does what the LLM physically cannot: execute tools, maintain conversation state, stream responses, persist memory. Everything else — reasoning, planning, tool selection, error recovery — is the LLM's job.

See **[DESIGN.md](DESIGN.md)** for the full architecture: agent loop, tool system, plugin contract, MCP gateway, memory database, A2A mesh, credential security model, and project structure.

## License

MIT
