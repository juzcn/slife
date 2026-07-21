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

## Install

**Zero prerequisites.**  The install script auto-installs Python 3.13, uv, and Node.js if needed — then installs slife in an isolated environment.  No git, no C++ compiler required.

### Option 1: Install Script (Recommended)

**macOS / Linux / WSL:**

```bash
curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh | bash
```

**Windows PowerShell:**

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife/main/install.ps1 | iex"
```

The script checks your Python version, installs [uv](https://docs.astral.sh/uv/) if needed, downloads the latest slife, and installs it in an isolated environment.  [Inspect the script](install.sh) before piping if you prefer.

### Option 2: uv tool install (requires git)

```bash
uv tool install git+https://github.com/juzcn/slife.git
```

### Option 3: pipx (requires git)

```bash
pipx install git+https://github.com/juzcn/slife.git
```

### Option 4: Try Before Installing

```bash
uvx --from git+https://github.com/juzcn/slife.git slife
```

No install — downloads, caches, and runs slife in a temporary environment.

After installation, the `slife` and `credstore` commands are available globally:

| Command | Location |
|---------|----------|
| `slife` | `~/.local/bin/slife` |
| `credstore` | `~/.local/bin/credstore` |
| Package files | `~/.local/share/uv/tools/slife/` |
| User data | `~/.slife/` (auto-created on first run) |

### Uninstall

```bash
uv tool uninstall slife
```

User data (config, memory DB, WeChat sessions, credentials backup) lives in `~/.slife/`. In development (when a `slife.json5` exists in the current directory), data stays in the project directory for easy debugging. Delete manually if desired:

```bash
rm -rf ~/.slife                            # all user data (production)
credstore delete DEEPSEEK_API_KEY          # remove a stored secret
credstore list                             # list all stored credentials
```

### Optional Extras

| Extra | Package | What it enables |
|-------|---------|-----------------|
| `embeddings` | `llama-cpp-python` | Local GGUF embeddings for semantic memory search (offline, no API cost). Without it, FTS5 keyword search still works. |

MQTT support (`paho-mqtt`) is now included by default — A2A agent mesh auto-activates when Mosquitto is detected.

```bash
# Install with embeddings extra (only optional extra left):
uv tool install "slife[embeddings]" --reinstall
```

#### Setting Up Local Embeddings

After installing `slife[embeddings]`, download a GGUF model and configure it:

```bash
# 1. Download a GGUF embedding model (BGE-M3, Q4_K_M quantized, ~300 MiB)
curl -LO https://huggingface.co/ChristianAzinn/bge-m3-gguf/resolve/main/bge-m3-Q4_K_M.gguf

# 2. Launch slife and tell the agent to enable it:
slife
# > enable local embeddings with bge-m3-Q4_K_M.gguf
```

The agent calls `memory_set_embedding` which writes the config and reloads the embedder — **no restart needed**.  Verify with:

```bash
slife
# > check embedding status
```

**Windows users**: `llama-cpp-python` needs a pre-built wheel (no C++ compiler required).  The Vulkan variant works on any GPU and falls back to CPU:

```bash
uv tool install "slife[embeddings]" --reinstall
# Then install the platform wheel into the tool's venv:
uv tool run --from slife pip install "llama-cpp-python @ https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.34-vulkan/llama_cpp_python-0.3.34-py3-none-win_amd64.whl"
```

Alternative CUDA wheels: `v0.3.34-cu132`, `v0.3.34-cu125`; AMD: `v0.3.34-hip-radeon`.

#### Setting Up the MQTT Mesh

After installing `slife[mqtt]`, run a [Mosquitto](https://mosquitto.org/) broker and launch with an agent identity:

```bash
# Terminal 1 — start the broker (or use your existing one)
mosquitto -p 1883

# Terminal 2 — launch slife with an agent identity
slife --agent my-agent
```

Configure broker address in `~/.slife/slife.json5` if not using defaults (`localhost:1883`):

```json5
mqtt: {
  broker: { host: "my-broker.local", port: 1883 },
}
```

## Quick Start

Store your API key and launch:

```bash
credstore set-password                # first time only — sets up encrypted backup
credstore set DEEPSEEK_API_KEY        # masked input, no echo
slife
```

The default config (`slife.json5`) ships with pre-configured MCP servers (filesystem, web fetch, DuckDuckGo search).

## How It Works

Slife is a **function-calling loop**. You type a message → the LLM decides what tools to call → Slife executes them and returns results → the LLM responds → repeat.

```
You: "Find all TODO comments and create GitHub issues for them"
  → LLM calls execute_shell("rg TODO")
  → LLM calls github__create_issue(...) for each one
  → LLM: "Created 7 issues. All linked in the description above."
```

## Configuration

Slife uses a **two-layer** configuration model with **enforced secret protection**:

| Layer | Storage | What goes here |
|-------|---------|----------------|
| **Secrets** | OS keyring (credstore) | API keys, tokens, passwords — encrypted at OS level |
| **Config** | `~/.slife/slife.json5` → `env:` | `${VAR}` references + non-secret values (EDITOR, LANG, etc.) |

**Plaintext API keys are rejected at startup.**  `api_key` fields must use
``${VAR}`` references (resolved from the OS keyring at runtime) or ``keyring:``
URIs.  The ``config_env_set`` tool also **rejects values that look like secrets**
(API key prefixes, high-entropy blobs, or key names containing KEY/SECRET/TOKEN)
— use ``config_secret_register`` for those instead.

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

## Credential Management

Slife ships with **[credstore](credstore/README.md)** — a standalone cross-platform secret manager backed by the OS keyring with AES-encrypted file backup.  It has its own [full documentation](credstore/README.md).

Quick reference:

```bash
credstore set-password                # first-time setup
credstore set DEEPSEEK_API_KEY        # store (masked atomic dual-write)
credstore inject DEEPSEEK_API_KEY     # persist to registry (Win) or profile (Unix)
credstore get DEEPSEEK_API_KEY        # retrieve, masked output
credstore list                        # list all stored keys
credstore status                      # backend status
```

| Command | Description |
|---------|-------------|
| `set-password` | Init cryptfile, set master key |
| `set KEY` | Atomic dual-write (cryptfile → keyring, rolls back on failure) |
| `get KEY` | Retrieve (keyring, masked) |
| `get KEY -p` | Retrieve (dual-query, plaintext) |
| `delete KEY` | Remove from both stores |
| `list` | List all stored keys |
| `inject KEY` | Persist to system env — registry (Windows) or profile (Unix) |
| `uninject KEY` | Remove from system env |
| `reset-keyring` | Restore keyring from cryptfile backup |
| `reset-backup` | Sync keyring → cryptfile |
| `status` | Backend status |

See **[credstore/README.md](credstore/README.md)** for disaster recovery, Python API, and advanced usage.

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

On restart, recent turns are automatically restored to the chat view — user messages, assistant responses, and tool call results all reappear.  (Transient UI state such as per-tool-call iteration counters is not preserved.)

```
memory_search("ConnectionError")            → exact error trace
memory_search("MCP config", mode="fts5")    → topic search
memory_search("that bug fix", mode="hybrid")→ semantic recall
memory_search(mode="time", since="2026-07") → browse by date
```

Agent isolation via `--agent alice`. Each agent gets its own DB (`<agent_id>.db`) in the data directory. Embedding via local GGUF (offline) or OpenAI-compatible API.  See [DESIGN.md § Permanent Memory](DESIGN.md#permanent-memory-slife-memory) for the full architecture.

### Plugins

Slife has a **plugin system** built on MCP stdio transport (JSON-RPC over
stdin/stdout).  A plugin is an independent child process using FastMCP as a
server framework — if it crashes, Slife continues.  Three built-in plugins ship
with Slife:

| Plugin | Role | Connection |
|--------|------|------------|
| **slife-mcp** | Gateway for external MCP servers (stdio + HTTP) — 10 management tools | Via slife-mcp proxy |
| **slife-memory** | Diary database with hybrid search (FTS5 + vec0 RRF) | Direct stdio |
| **slife-wechat** | Bidirectional WeChat messaging via iLink ClawBot API | Direct stdio |

**Built-in plugins are not standard MCP services** — they are Slife-specific
child processes that borrow MCP stdio as their IPC mechanism.  They cannot be
consumed by arbitrary MCP clients.  slife-memory and slife-wechat connect
directly to Slife; only slife-mcp acts as a gateway to external servers.

**External MCP servers** (filesystem, fetch, search APIs, etc.) are standard
MCP-compatible programs connected through the slife-mcp gateway.  They are
configured in `slife.json5` under `mcp.servers`:

```json5
mcp: {
  servers: {
    "my-server": {
      command: "uv", args: ["run", "python", "-m", "my_server"],
      env: { API_KEY: "${API_KEY}" },
      description: "My MCP server.",
    },
  },
}
```

> **Note:** Automatic plugin discovery and management (hot-loading plugins from
> directories, plugin marketplace, etc.) is planned for the next development
> phase.  Currently all three plugins are built-in and loaded at startup;
> external MCP servers are configured manually in `slife.json5`.

See [DESIGN.md § Plugin Architecture](DESIGN.md#plugin-architecture) for the full plugin contract and configuration reference.

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
| `--agent <id>` | `slife` | Agent identity — memory isolation key & A2A mesh identity |

## Requirements

The install script handles everything automatically.  Nothing to install beforehand.

| Component | Status |
|-----------|--------|
| Python ≥ 3.13 | Auto-installed via uv if missing |
| [uv](https://docs.astral.sh/uv/) | Auto-installed if missing |
| Node.js LTS | Auto-installed via winget (Windows) / apt, brew, dnf, pacman (Linux) if missing |
| `llama-cpp-python` | Optional — `slife[embeddings]` for local GGUF embeddings |
| `paho-mqtt` | Included — A2A MQTT mesh (auto-activates when Mosquitto is detected) |

**Node.js** is used by the fetch MCP server (`mcp-server-fetch`) for
Readability.js-powered article extraction.  If unavailable, fetch falls back to
pure-Python extraction — fully functional but with slightly lower article quality.
The install script auto-installs Node.js when missing; the runtime checks at
startup and reports status via `system_health`.

## Development

### Quick Start

```bash
git clone https://github.com/juzcn/slife.git
cd slife
uv sync --all-extras
uv run slife
```

### Dev Mode vs Production

Dev mode is detected automatically — when `pyproject.toml` has `[project] name == "slife"`, data files stay in the project directory. Production installs use `~/.slife/`.

| Aspect | Dev Mode | Production |
|--------|----------|------------|
| Config file | `./slife.json5` | `~/.slife/slife.json5` |
| Memory DB | `./slife.db` | `~/.slife/slife.db` |
| Credential store | System keyring (shared) | System keyring (shared) |
| Cryptfile | `./credentials.crypt` | `~/.credstore/credentials.crypt` |
| Logs | `./logs/` | `~/.slife/logs/` |

credstore works identically in both modes — secrets are always in the OS keyring, not in the project directory.

### First Run (Dev)

```bash
# 1. Set up credstore (one-time, creates encrypted backup)
uv run credstore set-password

# 2. Store API keys (masked input, no echo — paste + Enter)
uv run credstore set DEEPSEEK_API_KEY

# 3. Launch
uv run slife
```

The default `slife.template.json5` is copied to `slife.json5` on first run. The template ships with pre-configured MCP servers (filesystem, web fetch, DuckDuckGo search, Serper, Tavily, GitHub, Amap Maps). Edit `slife.json5` to customize providers, models, and MCP servers.

### Configuring API Keys (Dev)

Since plaintext keys are rejected system-wide, register secrets first:

```bash
# Store in OS keyring
uv run credstore set DEEPSEEK_API_KEY
uv run credstore set GITHUB_TOKEN

# Register in slife.json5 (or let the agent call config_secret_register)
# The ${VAR} syntax resolves from keyring at runtime
```

Then in `slife.json5`:
```json5
env: {
  DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}",
  GITHUB_TOKEN: "${GITHUB_TOKEN}",
}
models: {
  providers: {
    deepseek: {
      api_key: "${DEEPSEEK_API_KEY}",
      // ...
    },
  },
}
```

### Project Structure

```
slife/
├── slife/                    # Main application package
│   ├── agent/                # Agent loop, system prompt, LLM client
│   ├── tools/                # Tool definitions (credential, shell, MCP, etc.)
│   │   ├── credentials.py    # credential_check, inject/uninject
│   │   ├── config_env.py     # config_env_set/get/remove, config_secret_register
│   │   └── base.py           # Tool ABC + require_params helper
│   ├── plugins/              # Built-in plugins (memory, mcp, wechat)
│   ├── config.py             # Config loading + ${VAR} resolution
│   ├── paths.py              # Canonical filesystem paths (dev vs prod)
│   └── tui/                  # Textual terminal UI
├── credstore/                # Standalone credential manager (bundled, not PyPI)
│   └── credstore/
│       ├── _store.py         # CredentialStore + module-level API
│       ├── _backend.py       # System keyring + cryptfile backends
│       ├── __main__.py       # CLI (set, get, list, inject, etc.)
│       └── _tty.py           # Cross-platform masked terminal input
├── skills/                   # Skill definitions (on-demand agent plugins)
├── tests/                    # Test suite (pytest)
├── slife.json5               # Dev config (git-ignored)
├── slife.template.json5      # Default config template
└── pyproject.toml            # Project metadata + dependencies
```

### Running Tests

```bash
# All tests
uv run pytest

# Specific test files
uv run pytest tests/test_credentials.py -v
uv run pytest tests/test_config_env.py -v

# credstore tests
uv run pytest credstore/tests/ -v

# With coverage
uv run pytest --cov=slife --cov=credstore --cov-report=term-missing
```

### Running Individual Tools for Debugging

You can exercise tool logic directly without the full TUI:

```python
import asyncio
from pathlib import Path
from slife.tools.credentials import CredentialCheckTool

async def main():
    tool = CredentialCheckTool(config_path=Path("slife.json5"))
    result = await tool.execute(key="DEEPSEEK_API_KEY")
    print(result)

asyncio.run(main())
```

### Credstore CLI in Dev

```bash
# All credstore commands work identically in dev mode
uv run credstore status                # Backend status
uv run credstore list                  # List stored keys
uv run credstore get DEEPSEEK_API_KEY  # Retrieve (masked)
uv run credstore delete SOME_OLD_KEY   # Remove a credential
```

### Design Docs

See **[DESIGN.md](DESIGN.md)** for full architecture — agent loop, tool system, memory plugin, MCP gateway, A2A mesh, and credential security model. See **[credstore/README.md](credstore/README.md)** for credstore internals and disaster recovery.

## Design

Slife is a **minimum-harness agent**.  The harness only does what the LLM physically cannot: execute tools, maintain conversation state, stream responses, and persist memory.  Everything else — reasoning, planning, tool selection, error recovery — is the LLM's job.

See [DESIGN.md](DESIGN.md) for the full architecture, component-level documentation, and design rationale.

## License

MIT
