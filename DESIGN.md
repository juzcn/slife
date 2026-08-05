# Slife Design

## Philosophy

### Minimum Harness

The harness does only what the LLM physically cannot:

1. **Execute tools** — the LLM requests function calls; the harness runs them and returns results.
2. **Maintain conversation state** — the harness holds the message list and feeds it back each turn.
3. **Stream responses** — the harness delivers tokens to the UI as they arrive.
4. **Persist memory** — every message, thinking block, and tool output is saved immutably.

Everything else — reasoning, planning, tool selection, error recovery, coordination — is the LLM's job.

### Negative Space

What Slife deliberately is not:

- **Not a framework** — no agent composition, pipelines, or orchestration abstractions
- **Not a safety system** — no guardrails, approval gates, or sandboxing beyond the OS
- **Not an automation engine** — no scheduled tasks, background workers, or event triggers

It's a chat window with tools. The LLM is in full control.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  UI (Textual TUI)                                                    │
│  slife/ui/app.py, chat.py, handler.py, tool_display.py               │
├──────────────────────────────────────────────────────────────────────┤
│  Agent Service                                                       │
│  slife/agent/service.py — wires client + tools + loop + plugins      │
│  Manages MCP, MemDB, A2A/MQTT, WeChat, and subagent lifecycles      │
│  Unified inbox serializes human + WeChat + MQTT + subagent messages  │
├──────────────────────────────────────────────────────────────────────┤
│  Agent Loop                              │  MCP Client               │
│  Streaming function-calling              │  Streamable HTTP transport │
│  _context_status + _trim_context         │  OAuth support             │
│  harness notifications                   │  Tool proxy + adapter      │
│  Reasoning (thinking) support            │                           │
├──────────────────────────────────────────┴───────────────────────────┤
│  Tool Registry — unified OpenAI function definitions for all tools   │
│  Native · MemDB · Skills · MCP Proxy · CLI · REST API · A2A         │
├──────────────────────────────────────────────────────────────────────┤
│  Plugins (independent child processes, Streamable HTTP)              │
│  slife-mcp (gateway) · slife-memdb (diary) · slife-wechat          │
│  slife-memfiles (file server + ngrok tunnel)                         │
├──────────────────────────────────────────────────────────────────────┤
│  Platform (slife/platform.py)  │  Config (JSON5)  │  Health checks   │
├──────────────────────────────────────────────────────────────────────┤
│  Credstore — OS keyring + AES cryptfile backup                       │
│  Win · Mac · Linux (SecretService / keyutils) · WSL (PowerShell)     │
└──────────────────────────────────────────────────────────────────────┘
```

## Agent Loop

Single function-calling loop. Every tool is registered as an OpenAI function definition in one `ToolRegistry`. The LLM decides what to call and when.

```
User Input → Conversation.add_user_message()
  → loop: trim oldest turns if > 80% window (inserts _trim_context notification)
    → LLM stream → thinking/text chunks → handler callbacks
    → tool calls? → ToolRegistry.execute() → sanitize_secrets() → loop
    → no tool calls? → response text → return
    → save turn to diary (unconditional — even on cancel/error)
```

- **Streaming**: thinking and text tokens delivered in real-time via `AgentEventHandler` callbacks
- **Tool accumulation**: tool call deltas accumulated across chunks, executed as a batch
- **Tool timeout**: `asyncio.wait_for()` wraps every call (default 60s). Per-call override via `_timeout`
- **Iteration limit**: `max_iterations` (default 30) prevents infinite loops
- **Cancellation**: `Esc` sets a cancel event; loop stops at next iteration boundary
- **Context tracking**: `_last_context_tokens` updated every turn for accurate ceiling detection

### Context Window Management

Active conversation stays within `context_floor`–`context_ceiling` (default 20%–80%):

```
                context_window
┌──────────────────────────────────────────────────────────────┐
│   trimmed (in diary —        │  current context  │  headroom  │
│   recall via memory_search)  │  floor ~ ceiling  │  1-ceiling │
└──────────────────────────────────────────────────────────────┘
```

- **Detect**: uses `_last_context_tokens` (accurate `prompt_tokens` from the last API call)
- **Trim**: oldest complete turns removed; a synthetic `_trim_context` notification inserted after system prompt
- **Restore**: on startup, recent turns loaded from SQLite within `context_floor` token budget
- **Tool result cap**: single tool results capped at 20% of context window

### System Prompt

The prompt is a **runtime spec sheet** — facts the LLM cannot discover from training data or tool schemas. Two-part design:

- **Static** — `system_prompt.j2`, rendered once at startup (model, host, platform, CWD, shell, config paths). Never changes → maximal prompt cache hit rate.
- **Dynamic** — `context_status.j2`, re-rendered before each API call (current time, token usage, context time range, model/CWD/shell change notifications). Injected as synthetic `_context_status` harness-tool pair.

Design principles:
1. **Project-specific only** — if the LLM can infer it from tool schemas or training data, it doesn't belong
2. **Tool schemas over prompts** — usage instructions live in function `description`/`parameters`
3. **No personality or tone** — not a job description
4. **No slash commands** — natural language only; the LLM interprets intent
5. **Static baseline + change notifications** — constants at startup, deltas per-turn

## LLM Backends

Three backends, equal citizens — no conversion layer:

```
LLMClient (thin router)
  ├── OpenAIBackend         api: "openai-completions"
  ├── AnthropicBackend      api: "anthropic-messages"
  └── OpenAIResponsesBackend  api: "openai-responses"
```

- **No fourth format** — internal message format is OpenAI Chat Completions (the format the codebase already uses)
- **Provider dispatch is automatic** — `ModelConfig.api` determines which backend; `LLMClient.__init__` is a pure router
- **Unified streaming** — every backend produces the same `StreamChunk` objects (thinking, content, tool_deltas, usage)

### Model Management

Runtime model management via native tools — no config editing needed:

| Tool | Description |
|------|-------------|
| `list_models` | All configured models grouped by provider |
| `add_model` | Add/update a model (creates provider if new) |
| `remove_model` | Remove by ref; auto-switches if it was active |
| `switch_model` | Switch active model by ref |

## Tool System

### Tool ABC

`Tool` (`slife/tools/base.py`) defines `name`, `description`, `parameters` (JSON Schema), `category`, and `async execute(**kwargs) -> str`. Validation at class definition time via `__init_subclass__`.

### Auto-Discovery

`slife/tools/factory.py` uses `pkgutil.iter_modules` to import every module in `slife/tools.*`, then walks `Tool.__subclasses__()` to discover valid tool classes. A new `.py` file is automatically picked up.

### Tool Categories

All tools unified under `Tool`, registered in a single `ToolRegistry`. The LLM sees only function names and schemas.

| Category | File | Tools |
|----------|------|-------|
| System | `system.py` | `system_health`, `check_embedding`, `check_wechat` |
| Execution | `exec.py` | `execute_shell`, `run_python_script`, `install_python_package` |
| Skills | `skill.py` | `list_skills`, `use_skill`, `add_skill`, `remove_skill`, `skill_set`, `check_skills_dir` |
| CLI | `cli.py` | `cli_list_tools`, `cli_add_tool`, `cli_remove_tool`, `cli_set_tool`, `cli_check_installed` |
| REST API | `rest_api.py` | `rest_api_add`, `rest_api_remove`, `rest_api_list`, `rest_api_set` |
| A2A | `a2a.py` | 13 tools — agent discovery, task routing, subagent lifecycle, broadcast |
| Config | `config.py` | `config_env_set`, `config_env_get`, `config_env_remove`, `native_tool_set` |
| Models | `models.py` | `list_models`, `add_model`, `remove_model`, `switch_model`, `switch_to_nvidia_free` |
| Credentials | `credentials.py` | `credential_check`, `inject_credential`, `uninject_credential` |
| MemFiles | `memfiles.py` | `save_content_or_files`, `expose_file`, `include_image` |
| Display | `display.py` | `show_image` |
| Meta | `meta.py` | `list_tools` |

**Five managed categories** (MCP / Skills / CLI / REST API / Native) support a standard **list / add / remove / set** surface. All `add` tools are idempotent upserts. `mcp_set_server` additionally supports `disclosure="lazy"|"eager"` for tool lazy-loading.

### Timeout Architecture

Single enforcement point at the Agent Loop level. `_timeout` is injected into every tool's JSON Schema as a universal per-call override:

- Tools **without** a native `timeout` parameter → `asyncio.wait_for(timeout=...)`
- Tools **with** a native `timeout` (e.g. `execute_shell`) → mapped to the native argument, no double-wrap

The MCP Client does not apply its own timeout.

## Plugin Architecture

Four built-in plugins run as independent child processes. Communication is via **Streamable HTTP** (MCP protocol) or **plain HTTP** (memfiles). Each plugin binds a free port and signals the parent via stdout (`{"port": N}`).

### The Plugin Contract

1. Bind a free port, signal the parent: `bind_free_port()` → `signal_port(port)`
2. Start FastMCP on Streamable HTTP (or aiohttp for memfiles)
3. Define `@mcp.tool` functions (or HTTP routes for memfiles)
4. Be importable: `python -m <module>.server`

No base class, no import hook, no SDK.

### Built-in Plugins

| Plugin | Transport | Role |
|--------|-----------|------|
| **slife-mcp** | Streamable HTTP | Gateway for external MCP servers (stdio + HTTP). Manages connection lifecycle — spawn, health-check, route tool calls. |
| **slife-memdb** | Streamable HTTP | Diary database. Hybrid search (FTS5 + vec0 vector). Turn persistence, session restore, embedding configuration. |
| **slife-wechat** | Streamable HTTP | Bidirectional WeChat messaging via iLink ClawBot. Poll loop for incoming messages, dispatch for replies. |
| **slife-memfiles** | Plain HTTP | Serves local files via ngrok tunnel for LLM vision APIs. File-backed JSON token registry shared with the server subprocess. |

### slife-mcp — External MCP Gateway

Dual transport:

| Transport | Mechanism | Use |
|-----------|-----------|-----|
| **stdio** | Spawn subprocess, JSON-RPC over pipes | Local MCP servers (npx/uvx/bunx) |
| **http** | POST JSON-RPC via `httpx.AsyncClient` | Remote MCP endpoints |

Both share `MCPServerConnection` — `_request()` dispatches based on `ServerConfig.transport`.

### Subagent MCP Tool Discovery

Subagents share the main agent's plugin servers via environment variables (`SLIFE_MCP_PORT`, etc.). They eagerly discover external MCP tools at startup via `_discover_existing_mcp_tools()` — listing tools from already-connected servers without spawning new processes. This keeps tool naming consistent between main agent and subagents (both have `server__tool` style names).

### MCP Server Lifecycle

```
disabled ──[mcp_set_server enabled=True]──────→ enabled (connected, tools registered)
disabled ──[mcp_set_server disclosure="lazy"]─→ cannot set disclosure on disabled
enabled  ──[mcp_set_server enabled=False]─────→ disabled (disconnected, tools unregistered)
enabled  ──[mcp_set_server disclosure="lazy"]─→ enabled (connected, tools unloaded)
lazy     ──[mcp_set_server disclosure="eager"]─→ enabled (tools loaded)
any      ──[mcp_add_server]───────────────────→ upsert (same config → no-op; changed → restart)
```

All state changes persist to `slife.json5`.

## Memory (MemDB)

Every turn permanently recorded as an independent row — no session concept, a continuous time-ordered log.

### Schema

| Column | Purpose |
|--------|---------|
| `user_message` | What the user said |
| `messages` | Assistant response as OpenAI JSON array (thinking, tool calls, results, text) |
| `summary` | 1–2 sentence gist (LLM-written) |
| `tags` | Comma-separated topic tags |
| `created_at` | ISO 8601 with timezone |
| `channel` | Source: `human`, `wechat`, or remote agent id |
| `who_helped` / `what_model` | Agent identity + model used |
| `token_count` | Tokens consumed by this turn |

Turns saved **unconditionally** (cancel, error, or max-iterations).

### Search

Three indexes: FTS5 (BM25 keyword), sqlite-vec `vec0` (cosine KNN), B-tree on `created_at` (time range).

| Mode | Best for |
|------|----------|
| `grep` | Exact strings — error messages, file paths, code |
| `fts5` | Topic / keyword search with ranked snippets |
| `hybrid` | Semantic recall (FTS5 + vec0 → RRF merge) |
| `time` | Browse by date |

Hybrid mode uses Reciprocal Rank Fusion (RRF, k=60). Without an embedding backend, hybrid degrades to FTS5-only gracefully.

### Embedding

Three backends, configurable at runtime via `memory_set_embedding`:

| Backend | Dep | Default model | Dim |
|---------|-----|---------------|-----|
| GGUF (local) | `llama-cpp-python` | bge-m3 (Q4_K_M) | 1024 |
| Transformer (local) | `sentence-transformers` | BAAI/bge-m3 | 1024 |
| API (OpenAI-compatible) | Provider key | text-embedding-3-small | 1536 |

Configuration stored in `slife.json5` under `memdb.embedding`. Long turns chunked at paragraph boundaries (~500 tokens, 1-paragraph overlap). Model migration drops and recreates the vec0 table automatically; background reindex runs without blocking.

### Session Restore

On startup, recent turns read **directly from SQLite** — no MCP transport, no plugin dependency. UI shows history immediately; plugins start in parallel. Image markers (`[image: <path>]`) re-render from disk if the file still exists.

### Agent Isolation

`--agent alice` creates `~/.slife/alice.db`. `author` is the primary isolation column; `vec0` uses `author` as a partition key — KNN search is automatically scoped to one agent.

## A2A — Agent-to-Agent

Three transports, unified interface:

```
  a2a_list_agents / a2a_send_task
         │
  ┌──────┼──────┐
  │      │      │
 MQTT   HTTP   Subagent
(paho) (mcp)  (JSON-RPC stdin/stdout)
```

| Transport | Backend | Use |
|-----------|---------|-----|
| **MQTT** | paho-mqtt → asyncio.Queue, LWT | Remote peers over Mosquitto broker |
| **HTTP Streamable** | `mcp.client.streamable_http` | Direct agent-to-agent |
| **Subagent** | `asyncio.create_subprocess_exec`, JSON-RPC 2.0 | Local workers (always available) |

### Unified Inbox

All messages flow through a single `asyncio.Queue`:

```
Human keyboard ──→ Inbox.post() ──→ Queue ──→ Inbox.run() ──→ AgentLoop
MQTT inbox msgs ──→ Inbox.post() ──→
WeChat messages  ──→ Inbox.post() ──→
Subagent results ──→ Inbox.post() ──→
```

Messages are processed sequentially — only one AgentLoop runs at a time.

### Subagent Transport

Local child-process workers, always available — no config toggle:

- **headless.py**: Slife without TUI, JSON-RPC 2.0 over stdin/stdout
- **SubagentManager**: spawn/stop/list lifecycle, `max_subagents` limit
- **Memory isolation**: subagents don't connect to memdb server (avoids deadlock)
- **Ephemeral**: no persisted registry. `SLIFE_SUBAGENT_NAME` env var prevents recursive spawning

## Image & Memfiles

### Image Display

Two-tier rendering: **Sixel** (full-colour, whitelisted terminals: Windows Terminal, WezTerm, iTerm2, Kitty) → **HalfcellImage** (coloured Unicode half-blocks, all true-colour terminals) → text placeholder (fallback).

User attaches with `@path` syntax:
```
Check this screenshot @D:\Downloads\error.png and tell me what's wrong
```

### Memfiles — File Serving

A lightweight plain-HTTP server (`slife/plugins/memfiles/server.py`) streams local files to LLM vision APIs via an ngrok tunnel:

1. `expose_file(path)` → registers file with a short hex token → returns `https://xxx.ngrok-free.dev/share/<token>`
2. `include_image(url=...)` → passes URL to multimodal LLM

No BLOBs, no database, no base64 in context. Token→path mappings stored in a JSON registry file shared with the server subprocess — no HMAC, no IPC.

### Ngrok Tunnel

Started at session init via the official ngrok Python SDK (embedded agent — no external binary). `NgrokTunnel` (`slife/memfiles/tunnel.py`) manages the lifecycle with a background monitor that retries failed initial starts.

## UI

Textual TUI with minimal chrome:

- **ChatView** — scrollable message container
- **UserMessage** — prefix-styled user text with optional image attachments
- **AssistantMessage** — streaming text with collapsible thinking blocks
- **ToolCallWidget** — collapsible amber headers with detail
- **StatusBar** — model name, thinking indicator, token count
- **Auto-restore** — rebuilds last session's UI on startup

All user-facing text rendered with `markup=False` to prevent `MarkupError`.

### Progressive Disclosure

Not all tools are in every request. Three categories use lightweight summaries:

| Category | Browse | Load |
|----------|--------|------|
| MemDB | `memory_search` | `memory_open` |
| Skills | `list_skills` | `use_skill` |
| MCP | `mcp_list_servers` / `mcp_list_tools` | `mcp_set_server(enabled=True)` / disclosure |

## Config & Credentials

### Two-Layer Architecture

```
┌──────────────────────────────────────────────┐
│  OS Keyring (credstore)                      │
│  Encrypted at OS level. Survives config.     │
│  credstore set <KEY>    ← masked stdin        │
└──────────────────┬───────────────────────────┘
                   │ ${VAR} reference
                   ▼
┌──────────────────────────────────────────────┐
│  slife.json5 → env: section                  │
│  Plain config. Holds refs, not secrets.      │
└──────────────────────────────────────────────┘
```

### Credstore Backend Matrix

| Platform | Backend | Mechanism |
|----------|---------|-----------|
| **Windows** | WinCredKeyring | Windows Credential Manager (pywin32) |
| **WSL** | WslBackend | PowerShell bridge → advapi32.dll CredReadW/CredWriteW (C# P/Invoke) |
| **macOS** | Keychain | macOS Keychain via `security` CLI |
| **Linux (desktop)** | SecretService | D-Bus Secret Service (GNOME Keyring / KWallet) |
| **Linux (headless)** | KeyutilsBackend | Linux kernel keyring via ctypes |

Auto-selected by priority — no configuration needed.

### Secret Sanitization

Three chokepoints, single pattern-masking engine:
1. **Inbound** — `Conversation.add_user_message()` on every external message
2. **Tool arguments** — `Conversation.add_assistant_message()` on tool_call arguments
3. **Outbound** — `AgentLoop._execute_tools()` on every tool result

API key patterns (`sk-*`, `ghp_*`, Bearer tokens, 32+ char hex/base64) masked with `<MASKED>`.

### Config Sections

`slife.json5` structure:

| Section | Purpose |
|---------|---------|
| `env` | `${VAR}` references resolved at runtime |
| `models.providers` | Provider configs (api_key, base_url, api, models[]) |
| `active_model` | Currently active model ref (`provider/model`) |
| `agent` | `max_iterations`, `context_floor`, `context_ceiling`, `tool_timeout` |
| `tools` | Per-tool overrides (timeout, enabled) |
| `mcp.servers` | External MCP server configs |
| `memdb.embedding` | Embedding backend config (model, gguf_path, backend, dim) |
| `mqtt` | A2A broker config (host, port, heartbeat) |
| `subagent` | `max_subagents`, `task_timeout` |
| `wechat` | `enabled` toggle |
| `cli_tools` | External CLI tool definitions |
| `memory` | (legacy — use `memdb`) |

## Health Checks

At startup, `check_external_deps()` probes system dependencies and reports status via `system_health`:

| Dependency | Use |
|------------|-----|
| **node** | Readability.js article extraction (fetch MCP fallback) |
| **npm** | npx-based MCP servers |
| **bun** | nvidia-nim MCP server (bunx) |
| **uv** | uvx-based MCP servers |

Missing deps are reported as warnings — Slife still starts, affected MCP servers won't work.

## Ngrok Tunnel Monitoring

The memfiles ngrok tunnel uses the official ngrok Python SDK (embedded Rust agent, no external binary). The background monitor (`NgrokTunnel._run_monitor`) polls every 15s — if the executor failed to start the tunnel (e.g. transient TLS error), the monitor retries once. Since the agent is embedded, it cannot silently crash; no continuous health-ping is needed.

## Project Structure

```
slife/
  agent/               # LLM interaction
    loop.py            #   Function-calling loop (streaming, tool execution, context trim)
    service.py         #   Lifecycle manager (plugins, inbox, lifecycle)
    conversation.py    #   Message storage + history (OpenAI-format)
    llm_client.py      #   Backend router (~50 lines)
    system_prompt.py   #   Prompt rendering (static + dynamic Jinja2)
    llm_backends/      #   API backends: openai.py, anthropic.py, openai_responses.py
    inbox.py           #   Unified message queue
    plugins.py         #   Plugin spawn/stop helpers
    multimodal.py      #   Image encoding for vision models
  tools/               # Native tools (auto-discovered)
    base.py            #   Tool ABC
    registry.py        #   ToolRegistry
    factory.py         #   Auto-discovery (pkgutil.iter_modules)
    system.py          #   System health, embedding/wechat check
    exec.py            #   Shell, Python, package install
    skill.py           #   Skill loading (SKILL.md)
    cli.py             #   External CLI tool management
    rest_api.py        #   REST API tool management
    a2a.py             #   Agent-to-agent tools (13 tools)
    models.py          #   Model management (list/add/remove/switch)
    config.py          #   Config env var management
    credentials.py     #   Credential management
    memfiles.py        #   File save/expose/include
    display.py         #   Inline image display
    meta.py            #   list_tools
    _config_io.py      #   JSON5 read/write helpers
  memfiles/            # File serving infra
    token.py           #   File-backed JSON token registry
    tunnel.py          #   Ngrok tunnel lifecycle (official SDK, embedded agent)
  plugins/             # Built-in MCP plugins
    mcp/               #   External MCP gateway (connection pool, stdio/http)
    memdb/             #   Diary database (store, search, embeddings, schema)
    wechat/            #   WeChat messaging (iLink ClawBot client)
    memfiles/          #   Plain-HTTP file server
  mcp/                 # MCP client infra
    client.py          #   Streamable HTTP client
    tool_adapter.py    #   MCPProxyTool (bridges MCP → Tool ABC)
    process.py         #   MCPWrapperProcess (spawn, port handshake)
    oauth.py           #   OAuth 2.0 device-code flow
  a2a/                 # Agent-to-Agent
    transport.py       #   Abstract transport
    mqtt.py            #   MQTT transport (paho-mqtt)
    http.py            #   HTTP Streamable transport
    client.py          #   A2A client
    broker.py          #   Broker discovery
    task_store.py      #   Task state persistence
    card.py            #   Agent card (identity)
    config.py          #   A2A config
    identity.py        #   Agent identity
  subagent/            # Local workers
    headless.py        #   Headless JSON-RPC 2.0 process
    process.py         #   SubagentManager (spawn/stop/list)
    tools.py           #   Subagent tool implementations
  ui/                  # Textual TUI
    app.py             #   Textual App
    chat.py            #   Chat message widgets
    handler.py         #   TUIHandler (bridges events → widgets)
    tool_display.py    #   Tool call display helpers
    image_utils.py     #   Image rendering (Sixel/Halfcell detection)
    restore.py         #   Session restore (rebuilds UI from diary)
    approval_dialog.py #   Tool approval dialog
    slife.tcss         #   Textual CSS
  config.py            # JSON5 config parsing (models, env, plugins, A2A, subagent)
  paths.py             # Filesystem paths (dev vs prod, data dir, DB, memfiles)
  platform.py          # OS detection, shell detection, process lifecycle, notifications
  logfmt.py            # Structured logging + secret sanitization
  server_utils.py      # Plugin lifecycle: port binding, signal, FastMCP helpers
  bootstrap.py         # Logging setup, session init
  health.py            # External dependency checks (node, npm, bun, uv)
  env.py               # Environment variable management
  os_detect.py         # OS detection for install scripts

credstore/
  __init__.py          # Python API (get/set/delete/exists/list)
  __main__.py          # CLI (10 commands)
  _store.py            # CredentialStore
  _backend.py          # Dual-write: system keyring + cryptfile backup
  _platform.py         # WSL detection
  _wsl_backend.py      # PowerShell bridge → Windows Credential Manager
  _keyutils_backend.py # Headless Linux: kernel keyring via ctypes
  _enumerate.py        # Credential enumeration (Win/WSL)
  _resolver.py         # keyring: URI resolution
  _shell.py            # Shell formatting (export/unset)
  _config.py           # Config file loading
  _tty.py              # Masked terminal input

skills/                # On-demand SKILL.md plugins (seeded to ~/.slife/skills/)
```

## License

MIT
