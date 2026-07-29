# Slife

**Terminal-based AI agent** — a function-calling loop with minimum harness. Chat with an LLM that calls tools, remembers everything, and orchestrates other agents over MQTT.

```
┌─────────────────────────────────────────────────────────────┐
│  Terminal UI (Textual)                                      │
│  ────────────────────────────────────────────────────────── │
│  Agent Loop — LLM + Tools + Stream + Memory + A2A + Inbox  │
│  ┌───────────┬──────────┬───────────┬─────────────────────┐ │
│  │ MCP Proxy │ A2A Mesh │ Subagents │ Built-in Plugins    │ │
│  │ (gateway) │ (MQTT)   │ (workers) │ Memory · MCP · WX   │ │
│  └───────────┴──────────┴───────────┴─────────────────────┘ │
│  Permanent Memory — hybrid search (grep + FTS5 + semantic)  │
└─────────────────────────────────────────────────────────────┘
```

## Install

**Zero prerequisites.** The install script auto-installs uv and Node.js if needed.

### Install script (recommended)

**macOS / Linux / WSL:**

```bash
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh | bash
```

**Windows PowerShell:**

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/install.ps1 | iex"
```

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

# Windows PowerShell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/uninstall.ps1 | iex"
```

Uninstall removes the binaries. User data (`~/.slife/`, `~/.credstore/`) is listed but **not removed** — delete manually for a full reset.

## Quick Start

```bash
credstore set-password              # first time only — encrypted backup
credstore set DEEPSEEK_API_KEY       # store API key (masked input)
slife
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

```json5
env: {
  DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}",   // → resolved from keyring at runtime
}

models: {
  providers: {
    deepseek: {
      base_url: "https://api.deepseek.com",
      api_key: "${DEEPSEEK_API_KEY}",
      models: [{ model: "deepseek-v4-pro", name: "DeepSeek V4 Pro", reasoning: true }],
    },
  },
},
active_model: "deepseek/deepseek-v4-pro",
```

**Secrets never reach the LLM context.** All tool output is sanitized before reaching the model — API key patterns are auto-masked.

## Features

### Tools

All unified as OpenAI function definitions. The LLM sees no difference between native, MCP, or A2A tools.

| Category | Description |
|----------|-------------|
| **Native** | System info, Python execution, env/config management, skill loading, CLI discovery |
| **MCP / REST** | Filesystem, shell, code search, web fetch, any MCP server (stdio or HTTP) |
| **Skills** | On-demand plugins loaded via `list_skills` / `use_skill` |
| **CLI** | Auto-discovered external commands, persisted across restarts |
| **A2A** | Agent discovery, task routing, subagents, broadcast (13 tools) |

### Memory — Always On

Every conversation turn is permanently recorded. Hybrid search across four modes:

| Mode | Best for |
|------|----------|
| `grep` | Exact strings — error messages, file paths, code |
| `fts5` | Topic / keyword search with ranked snippets |
| `hybrid` | Semantic recall (FTS5 + vec0 vector search, RRF merge) |
| `time` | Browse by date |

Embedding backends: local GGUF (BGE-M3, ~300 MB, offline), HuggingFace transformers, or OpenAI-compatible API. Keyword search works without any embedding backend.

### Plugins

Three built-in plugins run as independent processes on Streamable HTTP transport:

| Plugin | Role |
|--------|------|
| **slife-mcp** | Gateway for external MCP servers (stdio + HTTP) |
| **slife-memory** | Diary database with hybrid search |
| **slife-wechat** | Bidirectional WeChat via iLink ClawBot |

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

Not all tools are in every request. Three categories use lightweight summaries:

| Category | Browse | Load |
|----------|--------|------|
| Memory | `memory_search` | `memory_open` |
| Skills | `list_skills` | `use_skill` |
| MCP | `mcp_list_tools` | `mcp_set_disclosure("eager")` |

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

**Windows** — no C++ compiler by default, so use a pre-built wheel. Pick the one that matches your setup:

| Your setup | Wheel | Notes |
|------------|-------|-------|
| No compiler, any GPU or none | `v0.3.34-vulkan` | Safest — uses Vulkan if GPU present, falls back to CPU |
| NVIDIA GPU + CUDA 12 | `v0.3.34-cu132` | CUDA 12.x |
| NVIDIA GPU + CUDA 11 | `v0.3.34-cu125` | CUDA 11.x |
| AMD GPU | `v0.3.34-hip-radeon` | ROCm |

```powershell
uv tool install "slife[gguf]" --reinstall
# Then install your chosen wheel into slife's venv:
$py = (uv tool list --show-paths 2>$null | Select-String 'slife v' | Out-String) -replace '.*\((.*?)\).*', '$1\Scripts\python.exe'
uv pip install --python $py "llama-cpp-python @ https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.34-vulkan/llama_cpp_python-0.3.34-py3-none-win_amd64.whl"
```

**First use** — download a GGUF model and enable it:

```bash
curl -LO https://huggingface.co/ChristianAzinn/bge-m3-gguf/resolve/main/bge-m3-Q4_K_M.gguf
```

Then launch slife and tell it: `enable local embeddings with bge-m3-Q4_K_M.gguf`

## Development

```bash
git clone https://github.com/juzcn/slife.git
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
