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
- **Not a safety system** — no sandboxing beyond the OS. A single opt-in approval gate exists for external MCP tools (`require_approval` per server), but it is off by default and covers only that one tool class
- **Not an automation engine** — no scheduled tasks, background workers, or event triggers

It's a chat window with tools. The LLM is in full control.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  UI (Textual TUI)                                                    │
│  slife/ui/app.py, chat.py, handler.py, tool_display.py,              │
│  image_utils.py, restore.py, approval_dialog.py                      │
├──────────────────────────────────────────────────────────────────────┤
│  Agent Service                                                       │
│  slife/agent/service.py — wires client + tools + loop + plugins      │
│  Manages MCP, MemDB, A2A/MQTT, WeChat, and subagent lifecycles       │
│  Unified inbox serializes human + WeChat + MQTT + subagent messages  │
├──────────────────────────────────────────────────────────────────────┤
│  Agent Loop                              │  MCP Client                │
│  Streaming function-calling              │  Streamable HTTP transport │
│  Context trim (_sys_trim) + status       │  OAuth device-code flow    │
│  (_sys_note); concurrent tool execution  │  Tool proxy + adapter      │
│  Reasoning (thinking) support            │                            │
├──────────────────────────────────────────┴───────────────────────────┤
│  Tool Registry — unified OpenAI function definitions for all tools   │
│  Native · MemDB · Skills · MCP Proxy · CLI · REST API · A2A          │
├──────────────────────────────────────────────────────────────────────┤
│  Plugins (independent child processes, Streamable HTTP)              │
│  slife-mcp (gateway) · slife-memdb (diary) · slife-wechat            │
│  slife-memfiles (plain-HTTP file server + ngrok tunnel)              │
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
User Input → Conversation.add_user_message()        (secrets sanitized)
  → loop (max_iterations):
    → cancel check
    → _maybe_trim_context()                          (> ceiling → trim to floor)
    → insert _sys_note (context status)
    → LLM stream → thinking/text/tool deltas → handler callbacks
    → tool calls? → ToolRegistry.execute() concurrently (asyncio.gather)
                    → sanitize_secrets() on each result → truncate → loop
    → approval-required tool? → serialized ApprovalDialog before execution
    → no tool calls? → response text → return
    → save turn to diary (unconditional — even on cancel/error/max-iterations)
```

- **Streaming**: thinking and text tokens delivered in real time via `AgentEventHandler` callbacks
- **Tool accumulation**: tool-call deltas accumulated across chunks, executed as a batch
- **Concurrent execution**: all calls in a batch run via `asyncio.gather`; approval dialogs serialize behind a lock
- **Tool timeout**: single enforcement point — `asyncio.wait_for()` wraps every call (default 60 s, `agent.tool_timeout`). Per-call override via `_timeout`; tools with a native `timeout` parameter (`execute_shell`) receive it directly instead of a double wrap
- **Background execution**: per-call `_async: true` schedules the tool as a background task and returns a task id immediately; poll with `check_async`, cancel with `cancel_async`
- **Iteration limit**: `max_iterations` (default 30) prevents infinite loops
- **Cancellation**: `Esc` sets a cancel event; checked before each iteration, after each stream, and before each tool batch
- **Context tracking**: `_last_context_tokens` (actual `prompt_tokens` from the last API call) drives trim decisions

### Context Window Management

Active conversation stays within `context_floor`–`context_ceiling` (default 20%–80% of `context_window`):

```
                context_window
┌──────────────────────────────────────────────────────────────┐
│   trimmed (in diary —        │  current context  │  headroom  │
│   recall via memory_search)  │  floor ~ ceiling  │  1-ceiling │
└──────────────────────────────────────────────────────────────┘
```

- **Detect**: `_last_context_tokens > context_window × context_ceiling` triggers a trim (falls back to a chars÷3 estimate before the first API call)
- **Trim**: `Conversation.extract_oldest_turns()` removes oldest complete turns down to `context_window × context_floor`; a synthetic **`_sys_trim`** harness-tool pair is inserted after the system prompt summarizing what was removed
- **Status**: before every API call a synthetic **`_sys_note`** pair (rendered from `context_status.j2`) is re-inserted after the last user message — current time, token usage, context time range, change notifications (model/CWD/shell/modalities), and any A2A peer presence events since the last turn (online/offline/timeout, drained read-once)
- **Restore**: on startup, recent turns are loaded directly from SQLite within the `context_floor` token budget
- **Tool result cap**: a single tool result is truncated at `tool_result_ceiling × context_window × 3` characters (default 20% of the window; ~3 chars/token heuristic)

### Harness-Only Tool Convention

Tools whose name starts with `_` (underscore) are **harness-only** — they are never exposed to the LLM. This is enforced by the tool registration filter in `PluginLifecycle.spawn()` and the direct registration helpers in `AgentService`.

| Tool | Purpose |
|------|---------|
| `_sys_trim` | Synthetic – inserted after system prompt to mark trimmed turns |
| `_sys_note` | Synthetic – re-inserted before each API call with context status |
| `_memory_save_turn` | memdb plugin – persists a turn to the diary |
| `_memory_get_recent_turns` | memdb plugin – loads recent turns for session restore |
| `_wechat_drain_incoming` | wechat plugin – drains queued incoming WeChat messages |
| `_wechat_dispatch_reply` | wechat plugin – sends a reply and cleans up typing indicator |

The convention is self-enforcing: adding a `_` prefix to any tool name automatically hides it from the LLM surface. No hardcoded name lists are needed. New harness tools should follow this convention from the start.

### System Prompt

The prompt is a **runtime spec sheet** — facts the LLM cannot discover from training data or tool schemas. Two-part design:

- **Static** — `slife/agent/templates/system_prompt.j2`, rendered once at startup: model identity, context policy (floor/ceiling/tool-result %), host platform (OS, arch, shell, python), workspace paths (data/config/logs/db/images/skills), credstore backend name, MCP tool naming prefix, and A2A broker info when configured. Never changes → maximal prompt cache hit rate.
- **Dynamic** — `slife/agent/templates/context_status.j2`, re-rendered before each API call and injected as the `_sys_note` pair: current time + UTC offset and last token usage always; context time range when set; model/CWD/shell/modalities only when changed; pending A2A peer presence events since the last turn (the same lines the TUI shows, drained once).

Design principles:
1. **Project-specific only** — if the LLM can infer it from tool schemas or training data, it doesn't belong
2. **Tool schemas over prompts** — usage instructions live in function `description`/`parameters`
3. **No personality or tone** — not a job description
4. **No slash commands** — natural language only; the LLM interprets intent
5. **Static baseline + change notifications** — constants at startup, deltas per-turn

## LLM Backends

Three backends, equal citizens. The internal message format is OpenAI Chat Completions; each backend owns its own wire conversion (`to_wire_messages()` / `to_wire_tools()`), and all produce the same unified stream:

```
LLMClient (thin router)
  ├── OpenAIBackend           api: "openai-completions"
  ├── AnthropicBackend        api: "anthropic-messages"
  └── OpenAIResponsesBackend  api: "openai-responses"
```

```python
StreamChunk(thinking=…, content=…, tool_deltas=…, usage=…)   # one chunk type, all backends
```

Reasoning ("thinking") support is per-backend:

| Backend | Thinking on | Notes |
|---------|-------------|-------|
| OpenAI Completions | `extra_body.thinking.type = "enabled"` (+ optional `reasoning_effort`) | DeepSeek requires explicit `"disabled"` when off; thinking streamed from `delta.reasoning_content` |
| Anthropic Messages | `thinking.budget_tokens = max(max_tokens // 2, 1024)` | `compat.thinkingFormat: "openai"` (Bailian/Qwen) sends no thinking param — the model always thinks |
| OpenAI Responses | `reasoning.effort` (default `"medium"`) | Streams both `reasoning_text` and `reasoning_summary_text` deltas |

**Prompt caching (Anthropic system blocks):** `AnthropicBackend._oa_msgs_to_anthropic` emits each OpenAI `system` message as an Anthropic system content block and tags the **last** one with `cache_control: {type: "ephemeral"}` — the static base prompt becomes the cache breakpoint, so only the dynamic `_sys_note` footer (a message-stream pair, never a second `system` message) changes per turn. Guarded by `_use_system_cache_control()`: on by default for `api.anthropic.com`, off for Anthropic-compatible providers (Bailian/Qwen) that may reject the field, overridable per model via `compat.cacheControl`.

### Model Management

Runtime model management via native tools — no config editing needed:

| Tool | Description |
|------|-------------|
| `model_list` | All configured models grouped by provider (active marked) |
| `model_add` | Add/update a model (creates provider if new) |
| `model_remove` | Remove by ref; auto-switches if it was active |
| `model_switch` | Switch active model by ref — persists to config and rebuilds the client live |
| `switch_to_nvidia_free` | In-memory-only switch to a free NVIDIA NIM model via the nvidia-nim MCP server |

Model switches fire callbacks that rebuild the LLM client, update loop parameters (vision, context window, modalities), and re-render the system prompt.

## Tool System

### Tool ABC

`Tool` (`slife/tools/base.py`) defines `name`, `description`, `parameters` (JSON Schema), `category`, and `async execute(**kwargs) -> str`. Required fields are validated at class-definition time via `__init_subclass__`. Optional class flags:

- `requires_a2a` — register only when the A2A mesh is active
- `_subagent_skip` — hide from subagent workers
- `requires_approval` — route through the approval dialog before execution
- `_skip_auto_register` — excluded from auto-discovery (used by `MCPProxyTool`)

`from_config(cfg, config)` allows per-tool construction from the `tools:` overrides in `slife.json5` (e.g. `execute_shell` reads its default timeout there).

### Auto-Discovery

`slife/tools/factory.py` uses `pkgutil.iter_modules` to import every module in `slife.tools.*` (skipping `base`/`factory`), then walks `Tool.__subclasses__()` recursively. A new `.py` file is automatically picked up. Filtering applies `enabled: false` overrides, `_subagent_skip` in subagent mode, and `requires_a2a` without a mesh.

### Tool Categories — 54 native tools

All tools unified under `Tool`, registered in a single `ToolRegistry`. The LLM sees only function names and schemas.

| Category | File | Tools |
|----------|------|-------|
| System | `system.py` | `system_health`, `check_memdb`, `check_wechat`, `check_memfiles`, `check_mcp` |
| Execution | `exec.py` | `execute_shell`, `run_python_script`, `install_python_package` |
| Skills | `skill.py` | `skill_list`, `skill_use`, `skill_add`, `skill_remove`, `skill_set`, `skill_check_dir` |
| CLI | `cli.py` | `cli_list_tools`, `cli_add_tool`, `cli_remove_tool`, `cli_set_tool`, `cli_check_installed` |
| REST API | `rest_api.py` | `rest_api_list`, `rest_api_add`, `rest_api_remove`, `rest_api_set` |
| A2A | `a2a.py` | 13 tools — `a2a_list_agents`, `a2a_list_subagents`, `a2a_send_task`, `a2a_send_task_async`, `a2a_get_task_result`, `a2a_cancel_task`, `a2a_list_tasks`, `a2a_subscribe_task`, `a2a_spawn_subagent`, `a2a_stop_subagent`, `a2a_agent_card`, `a2a_notify_user`, `a2a_broadcast` |
| Config | `config.py` | `config_env_set`, `config_env_get`, `config_env_remove`, `native_tool_set` |
| Models | `models.py` | `model_list`, `model_add`, `model_remove`, `model_switch`, `switch_to_nvidia_free` |
| Credentials | `credentials.py` | `credential_check`, `credential_inject`, `credential_uninject` |
| MemFiles | `memfiles.py` | `save_content_or_files`, `expose_file` (tunnel active only), `include_image` |
| Display | `display.py` | `show_image` |
| Meta | `meta.py` | `list_tools`, `check_async`, `cancel_async`, `clear_context` |

**Five managed categories** (MCP / Skills / CLI / REST API / Models) support a standard **list / add / remove / set** surface. All `add` tools are idempotent upserts. `mcp_set_server` additionally supports `disclosure="lazy"|"eager"` for tool lazy-loading.

#### Tool Naming Convention

All managed tools follow `category_verb[_noun]` order:

| Category | Prefix | Examples |
|----------|--------|----------|
| Models | `model_` | `model_list`, `model_add`, `model_switch` |
| Skills | `skill_` | `skill_list`, `skill_use`, `skill_check_dir` |
| CLI | `cli_` | `cli_list_tools`, `cli_add_tool` |
| REST API | `rest_api_` | `rest_api_list`, `rest_api_add` |
| MCP (built-in) | `mcp_` | `mcp_list_servers`, `mcp_add_server` |
| A2A | `a2a_` | `a2a_list_agents`, `a2a_send_task` |
| Config | `config_env_` | `config_env_set`, `config_env_get` |
| Credentials | `credential_` | `credential_check`, `credential_inject` |

Core execution tools (`execute_shell`, `install_python_package`, `run_python_script`) and meta tools (`list_tools`, `clear_context`) use `verb_noun` without a category prefix — they are singleton tools, not categories.

### Registry

`ToolRegistry` is a name-keyed dict with `register` / `unregister` / `unregister_by_prefix` / `get` / `list_tools` / `to_openai_functions` / `execute`. A module-level singleton (`get_registry()`) lets meta-tools introspect without circular imports. Dynamic tools — plugin tools (memdb, wechat), MCP wrapper tools, and external MCP server tools — are registered at runtime as `MCPProxyTool` instances named `"{server}__{tool}"` (e.g. `filesystem__read_file`, `memdb__memory_search`). Harness-only plugin tools (prefixed `_`: `_memory_save_turn`, `_wechat_drain_incoming`, etc.) are filtered out before registration.

### Timeout Architecture

Single enforcement point at the Agent Loop level. `_inject_meta_params()` adds `_timeout` (number) and `_async` (boolean) to **every** function definition sent to the LLM:

- Tools **without** a native `timeout` parameter → `asyncio.wait_for(timeout=…)`
- Tools **with** a native `timeout` (`execute_shell`) → mapped to the native argument, no double-wrap

The MCP client applies no timeout of its own; enforcement stays in one place.

### Approval Gate

Tools flagged `requires_approval` pause execution and push a modal `ApprovalDialog` (Enter = approve, Esc = deny). Dialogs serialize behind a lock.

Two ways to enable approval per tool:

1. **External MCP servers** — set `require_approval: true` on the server config in `mcp.servers.<name>`. All tools from that server require approval.
2. **Native tools** — add `require_approval: true` to the tool's override entry in the `tools:` section of slife.json5:

```json5
tools: [
  {name: "execute_shell", require_approval: true},
  {name: "install_python_package", require_approval: true},
]
```

Default is **off** for all tools.

## Plugin Architecture

Four built-in plugins run as independent child processes. Communication is via **Streamable HTTP** (MCP protocol) or **plain HTTP** (memfiles).

**WSL note:** Custom env vars set via `create_subprocess_exec(env=…)` are NOT forwarded to Windows `.exe` processes through WSL interop. `WSLENV` is only read by the WSL `/init` at session start, not by child processes. Therefore, **all MCP server runtimes on WSL must be Linux-native binaries** — the install script enforces this by detecting `/mnt/*` paths and installing native versions.

### The Plugin Contract

1. Bind a free port: `bind_free_port()` pre-binds `127.0.0.1:0` and keeps the socket — no race between port discovery and server start
2. Signal the parent: `signal_port(port)` writes `{"port": N}` to stdout and closes it
3. Start FastMCP on Streamable HTTP with the pre-bound socket (or aiohttp for memfiles)
4. Define `@mcp.tool` functions (or HTTP routes for memfiles)
5. Be importable: `python -m <module>.server`

No base class, no import hook, no SDK. Plugins are auto-discovered by scanning `slife.plugins.*` for packages with a `server.py`. The parent reads the port line with a 30 s timeout; initial client connection retries 30× at 0.1 s.

### Watchdog (Auto-Restart)

Each plugin runs with a **watchdog** background task that monitors the child process and auto-restarts it on unexpected exit:

| Feature | Detail |
|---------|--------|
| Detection | `await subprocess.wait()` — blocks until the child exits |
| On crash | Unregisters the plugin's proxy tools, then restarts the process |
| Backoff | Exponential: 1 s → 2 s → 4 s → … → 30 s max |
| Max restarts | 3 consecutive failures → watchdog gives up and logs an error |
| Success reset | A successful restart resets the backoff and retry counter |
| Scope | **mcp** (respawns wrapper + reconnects external servers), **memdb**, **wechat** (restores poll loop) |

Subagents do **not** have their own watchdog — they connect to the main agent's plugin processes via HTTP, so a subagent crash only kills the subagent, not the shared infrastructure.

Processes communicate through environment variables:

| Variable | Purpose |
|----------|---------|
| `SLIFE_SESSION_ID` / `SLIFE_AGENT_ID` | Log correlation, agent identity |
| `SLIFE_DATA_DIR` / `SLIFE_CONFIG_DIR` | Directory overrides |
| `SLIFE_{NAME}_PORT` | Published port of each plugin (MCP / MEMDB / WECHAT / MEMFILES) |
| `SLIFE_MEMFILES_REGISTRY` | JSON token-registry file path for the memfiles server |
| `SLIFE_MEMFILES_URL` | Public ngrok URL |

### Built-in Plugins

| Plugin | Transport | Role |
|--------|-----------|------|
| **slife-mcp** | Streamable HTTP | Gateway for external MCP servers (stdio + HTTP). Manages connection lifecycle — spawn/connect, route tool calls, persist config. |
| **slife-memdb** | Streamable HTTP | Diary database. Hybrid search (FTS5 + vec0 vector). Turn persistence, session restore, embedding configuration. |
| **slife-wechat** | Streamable HTTP | Bidirectional WeChat messaging via iLink ClawBot. Long-poll loop for incoming messages, typing indicators, dispatch for replies. |
| **slife-memfiles** | Plain HTTP | Serves local files through an ngrok tunnel (`GET /share/{token}`). File-backed JSON token registry shared with the parent. |

### slife-mcp — External MCP Gateway

Three wire transports, one raw JSON-RPC connection class (`MCPServerConnection` — deliberately no `ClientSession`/anyio TaskGroups to avoid event-loop conflicts with FastMCP):

| Transport | Mechanism | Use |
|-----------|-----------|-----|
| **stdio** | Spawn subprocess, JSON-RPC over pipes | Local MCP servers (npx/uvx/bunx) |
| **http (SSE)** | GET with `Accept: text/event-stream`, POST to message endpoint | Remote SSE endpoints (tried first for URLs) |
| **http (streamable)** | POST JSON-RPC directly, `mcp-session-id` header | Remote Streamable HTTP endpoints (fallback) |

Exposed management tools: `mcp_add_server` (idempotent upsert), `mcp_remove_server`, `mcp_list_servers`, `mcp_list_tools`, `mcp_call_tool`, `mcp_set_server` (enable/disable/disclosure).

`mcp_list_servers` returns raw server state (name, enabled, active, state, tool_count, transport, error) — pure data, no diagnosis. `check_mcp` (via `system_health`) consumes that data and adds health levels (ok/warning/info) with remediation hints. The separation avoids duplicated state-interpretation logic.

Server lifecycle:

```
disabled ──[mcp_set_server enabled=True]──────→ enabled (connected, tools registered)
enabled  ──[mcp_set_server enabled=False]─────→ disabled (disconnected, tools unregistered)
enabled  ──[mcp_set_server disclosure="lazy"]─→ enabled (connected, tools unloaded)
lazy     ──[mcp_set_server disclosure="eager"]─→ enabled (tools loaded)
any      ──[mcp_add_server]───────────────────→ upsert (same config → no-op; changed → restart)
```

All state changes persist to `slife.json5`. Servers needing OAuth use a device-code flow (see below); tokens are stored in the OS keyring via credstore (`mcp_oauth_*`).

### Subagent MCP Tool Discovery

Subagents inherit `SLIFE_MCP_PORT` from the parent environment, connect to the existing MCP wrapper over Streamable HTTP, and eagerly discover external MCP tools at startup via `_discover_existing_mcp_tools()` — listing tools from already-connected servers without spawning new processes or mutating config. This keeps tool naming consistent between main agent and subagents (`server__tool` in both).

## Memory (MemDB)

Every turn permanently recorded as an independent row — no session concept, a continuous time-ordered log in `~/.slife/<agent>.db`.

### Schema

`diary` table:

| Column | Purpose |
|--------|---------|
| `user_message` | What the user said |
| `messages` | Assistant response as OpenAI JSON array (thinking, tool calls, results, text) |
| `summary` | 1–2 sentence gist (LLM-written) |
| `tags` | Comma-separated topic tags |
| `created_at` | ISO 8601 with timezone (B-tree indexed) |
| `channel` | Source: `human`, `wechat`, or remote agent id |
| `who_helped` / `what_model` | Agent identity + model used |
| `token_count` | Tokens consumed by this turn |

Supporting structures: `diary_fts` (FTS5 content-sync table over message/summary/tags/channel with insert/delete triggers), `diary_semantic` (sqlite-vec `vec0` table: embedding + rowid + chunk index + summary/tags/created_at), and `diary_meta` (key-value store tracking the embedding model identity for migration detection).

Turns are saved **unconditionally** after every turn (cancel, error, or max-iterations) via the harness-only `_memory_save_turn` tool.

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
| GGUF (local) | `llama-cpp-python` | bge-m3 | 1024 |
| Transformer (local) | `sentence-transformers` | BAAI/bge-m3 | 1024 |
| API (OpenAI-compatible) | Provider key | text-embedding-3-small | 1536 |

Backend selection priority: GGUF file present → transformer requested → API key present → disabled. Long turns are chunked at paragraph boundaries (~2000 chars ≈ 500 tokens, 1-paragraph overlap); the embedded text is the user message plus all assistant/tool contents. Configuration lives in `slife.json5` under `memdb.embedding`. Model/dimension migration drops and recreates the `vec0` table automatically; reindexing runs in the background in small batches without blocking.

### Session Restore

On startup, recent turns are read **directly from SQLite** — no MCP transport, no plugin dependency. The UI rebuilds the last session immediately (user messages, assistant text, tool-call widgets, images whose files still exist); plugins start in parallel.

### Agent Isolation

`--agent alice` uses `~/.slife/alice.db` — isolation is at the database-file level. Each agent has its own diary, FTS, and vector indexes; nothing is shared between agents.

## A2A — Agent-to-Agent

One tool surface over three transports:

```
  a2a_list_agents / a2a_send_task / …
         │
  ┌──────┼──────┐
  │      │      │
 MQTT   HTTP   Subagent
(paho) (mcp)  (JSON-RPC stdin/stdout)
 complete skeleton complete
```

| Transport | Backend | Status |
|-----------|---------|--------|
| **MQTT** | paho-mqtt (MQTTv5) → asyncio.Queue, LWT | Fully implemented |
| **HTTP Streamable** | `mcp.client.streamable_http` | Skeleton — connect/disconnect only |
| **Subagent** | `asyncio.create_subprocess_exec`, JSON-RPC 2.0 | Fully implemented, always available |

The unification lives at the **tool level**: each `a2a_*` tool routes to the MQTT client or the `SubagentManager` depending on whether the target is a local subagent. Subagents are not a `TransportAdapter`.

### MQTT Mesh

- Topics: `Slife/<agent_id>/presence`, `Slife/<agent_id>/tasks/inbox`, `Slife/<agent_id>/tasks/result`
- Presence heartbeat every 15 s (configurable); peers silent for 45 s are pruned. LWT publishes `{"status":"offline"}` (QoS 1) so crashes are visible
- Client id is `<agent_id>-<pid>` to allow multiple processes per agent id
- Duplicate agent detection: after subscribing, the client listens 1.5 s for an existing presence with the same id and exits with a clear error rather than splitting the identity
- Slife only **probes** the broker (TCP connect) — Mosquitto is started by the user; if the probe fails, A2A is disabled and reported via `system_health`
- Peer presence **transitions** (online/offline/timeout) reach the LLM context: `AgentService._on_agent_change` appends the TUI-identical line (via `format_presence_line`, which also filters heartbeat-driven `status_change`) to a buffer that `AgentLoop` drains read-once into the `_sys_note` footer each turn. The footer carries only *changes* — the current roster stays queryable via `a2a_list_agents`, so a missed event never leaves the LLM with stale state

### Unified Inbox

All messages flow through a single `asyncio.Queue`:

```
Human keyboard ──→ Inbox.post() ──→ Queue ──→ Inbox.run() ──→ AgentLoop
MQTT tasks     ──→ Inbox.post() ──→
WeChat messages──→ Inbox.post() ──→
Subagent results─→ Inbox.post() ──→
```

Messages are processed sequentially — only one AgentLoop runs at a time. Human and WeChat sources keep persistent conversations; remote agents get fresh one-shot conversations. Status flips to `busy`/`idle` around each turn; `on_turn_complete` fires unconditionally (in `finally`), so memory persistence survives cancellation.

### Task Store

Sent/received tasks are tracked in memory (`TaskRecord`: id, agent, preview, status, transport, timings, result capped at 2000 chars; 500-record soft cap). The store is **not persisted across restarts** — `a2a_list_tasks` after restart is empty by design.

### Subagent Transport

Local child-process workers, always available — no config toggle:

- **headless.py**: Slife without TUI, JSON-RPC 2.0 over stdin/stdout — methods `tasks/send`, `shutdown`; notifications `tasks/complete`, `tasks/progress`; a `{ready: true}` result signals startup
- **SubagentManager**: spawn/stop/list lifecycle; auto-names `sub-1`, `sub-2`, …; `max_subagents` default 5, `task_timeout` default 120 s
- **Memory isolation**: `SLIFE_MEMDB_PORT` / `SLIFE_WECHAT_PORT` are removed from the child environment — subagents don't write to the diary (and can't deadlock on the shared server)
- **Recursion guard**: `SLIFE_SUBAGENT_NAME` in the child environment prevents spawning further subagent managers

## Image & Memfiles

### Image Input

User attaches with `@path` / `@url` syntax (quoted paths supported):

```
Check this screenshot @D:\Downloads\error.png and tell me what's wrong
```

The TUI extracts attachments; `include_image_url()` turns each into a vision content block — HTTP(S) URLs pass through, local files are read and base64-encoded as `data:` URIs. The agent can attach images mid-conversation with the `include_image` tool (injects into the last user message). Tool results may embed `[image: <path>]` markers; the loop scans for them after each batch and renders them in the UI. Each backend converts blocks to its wire format (Anthropic `image.source`, Responses `input_image`).

### Image Display

Three-tier rendering in the terminal: **Sixel** (full-colour; whitelisted terminals: Windows Terminal, WezTerm, iTerm2, Kitty — detected via `WT_SESSION` / `TERM_PROGRAM` / `KITTY_WINDOW_ID`) → **HalfcellImage** (coloured Unicode half-blocks, any true-colour terminal) → text placeholder. Chat images are capped at 32×16 cells (thumbnails 20×10).

### Memfiles — File Serving

A lightweight plain-HTTP server (`slife/plugins/memfiles/server.py`) streams local files through an ngrok tunnel:

1. `expose_file(path)` → registers the file under a random 30-char hex token (`secrets.token_hex(15)`) → returns `https://xxx.ngrok-free.dev/share/<token>`.  This tool is **dynamically registered** — it only appears in the tool list when the ngrok tunnel is active.  If the tunnel fails to start (no token, free-tier limit reached), the tool is hidden from the LLM entirely rather than returning errors at runtime.
2. `GET /share/{token}` streams the file in 64 KB chunks (403 unknown token, 404 file gone)

No BLOBs, no database, no HMAC — token→path mappings live in a JSON registry file (atomic writes) whose path is passed to the server subprocess via `SLIFE_MEMFILES_REGISTRY`. `save_content_or_files` persists content/URL/files under `<agent>.files/` with an `index.json` and returns share URLs when the tunnel is active; when offline, files are still saved locally and the result notes "(sharing offline)".

### Ngrok Tunnel

Started at session init via the official ngrok Python SDK (embedded agent — no external binary). Authtoken resolution: credstore `NGROK_AUTHTOKEN` → environment. Uses **endpoint pooling** (`pooling_enabled=True`) so multiple slife instances (WSL + Windows, sub-agents on different machines) share the same dev domain — ngrok load-balances across all online agents. Initial start retries up to 3 times with linear backoff (2/4 s); a background monitor performs one follow-up retry if the first start failed.

ngrok free tier limits: **1 online agent** (one tunnel per token — only the first agent to start gets the memfiles tunnel; subsequent agents fail to bind), 1 GB transfer/month, 20k HTTP requests/month. Endpoint pooling requires no paid plan.

## UI

Textual TUI with minimal chrome:

- **ChatView** — scrollable message container; printable keys redirect to the input
- **UserMessage** — prefix-styled user text with optional image attachments
- **AssistantMessage** — streaming text with collapsible thinking blocks (Enter/Space toggle)
- **ToolCallWidget** — collapsible amber headers: status icon, label, primary-arg preview, iteration counter; Ctrl+Y copies the result
- **StatusBar** — model name, thinking indicator, inbox state, token count
- **ApprovalDialog** — modal approve/deny for `requires_approval` tools
- **Auto-restore** — rebuilds last session's UI from the diary on startup

All user-supplied text is rendered with `markup=False` to prevent `MarkupError` injection.

### Keyboard

| Key | Action |
|-----|--------|
| `Ctrl+C` | Quit |
| `Esc` | Cancel agent loop |
| `Ctrl+L` | Focus input |
| `Home` / `End` | Scroll to top / bottom |
| `Ctrl+Y` | Copy result (on a tool call) |
| `Enter` / `Space` | Toggle thinking block (on an assistant message) |

### Progressive Disclosure

Not all tools are in every request. Several categories use lightweight summaries:

| Category | Browse | Load |
|----------|--------|------|
| MemDB | `memory_search` | `memory_open` |
| Skills | `skill_list` | `skill_use` |
| MCP | `mcp_list_servers` / `mcp_list_tools` | `mcp_set_server(enabled=True)` / disclosure |

## Config & Credentials

### Two-Layer Architecture

```
┌──────────────────────────────────────────────┐
│  OS Keyring (credstore)                      │
│  Encrypted at OS level + cryptfile backup.   │
│  credstore set <KEY>    ← masked stdin       │
└──────────────────┬───────────────────────────┘
                   │ ${VAR} / keyring:service/key reference
                   ▼
┌──────────────────────────────────────────────┐
│  slife.json5 → env: section                  │
│  Plain config. Holds refs, not secrets.      │
└──────────────────────────────────────────────┘
```

`${VAR:-default}` fallbacks supported; resolution is recursive over strings/lists/dicts.

### Credstore Backend Matrix

| Platform | Backend | Priority | Mechanism |
|----------|---------|----------|-----------|
| **WSL** | WslBackend | 9.5 | PowerShell bridge → advapi32.dll CredReadW/CredWriteW (C# P/Invoke) |
| **Windows** | WinCredKeyring | 9.0 | Windows Credential Manager (pywin32/win32ctypes) |
| **macOS** | Keychain | 5.0 | macOS Keychain via keyring |
| **Linux (desktop)** | SecretService | 5.0 | D-Bus Secret Service (GNOME Keyring / KWallet) |
| **Linux (headless)** | KeyutilsBackend | 1.5 | Kernel persistent keyring via ctypes syscalls (zero deps) |

Auto-selected by priority — no configuration needed. `credstore set` dual-writes: cryptfile first, then the system keyring (rolled back if the keyring write fails). The CLI also provides `set-password`, `status`, `get`, `delete`, `copy`, `list`, `reset-keyring`, `reset-backup`, `inject`/`uninject` (shell-aware export: bash / powershell / cmd; Windows persistence via `HKCU\Environment`).

### Secret Sanitization

Three chokepoints, single pattern-masking engine (`logfmt.sanitize_secrets`):
1. **Inbound** — `Conversation.add_user_message()` on every external message
2. **Tool arguments** — `Conversation.add_assistant_message()` on tool_call arguments
3. **Outbound** — `AgentLoop._execute_tools()` on every tool result

Known API key shapes (`sk-*`, `ghp_*`, `ya29.*`, `pypi-*`), `Authorization: Bearer` tokens, and credential-named `key=value` pairs are masked with `<MASKED>`. This is a pattern-based best effort, not a guarantee (see REVIEW.md).

### Config Sections

`slife.json5` structure parsed by `Config.from_json5`:

| Section | Purpose |
|---------|---------|
| `env` | `${VAR}` references, applied to the environment at startup |
| `models.providers` | Provider configs (api_key, base_url, api, models[]) |
| `active_model` | Currently active model ref (`provider/model`) |
| `agent` | `max_iterations`, `tool_timeout`, `context_floor`, `context_ceiling`, `tool_result_ceiling` |
| `tools` | Per-tool overrides (timeout, enabled) |
| `mcp.servers` | External MCP server configs (incl. `require_approval`, `disclosure`) |
| `memdb.embedding` | Embedding backend config (backend, model, dim, gguf_path) |
| `wechat` | `enabled` toggle |
| `mqtt` | A2A config (transport, broker host/port, heartbeat, task_timeout) |
| `subagent` | `max_subagents`, `task_timeout` |
| `cli_tools` | External CLI tool definitions (read by the CLI tools directly) |
| `rest_apis` | REST API registrations (read by the REST API tools directly) |

## Health Checks

Health checks fall into two categories, both surfaced through `system_health`:

**Static startup checks** — `check_external_deps()` probes system tooling once at startup:

| Dependency | Use |
|------------|-----|
| **node** | Readability.js article extraction (fetch MCP fallback) |
| **npm** | npx-based MCP servers |
| **bun** | nvidia-nim MCP server (bunx) |
| **uv** | uvx-based MCP servers |

**Dynamic runtime checks** — each query inspects current application state:

| Check | What it monitors | Layer |
|-------|-----------------|-------|
| `check_memdb` | Database file + embedding backend (model, dimension, availability) | Application state (memdb plugin) |
| `check_wechat` | Login status, session age, QR expiry | Application state (wechat plugin) |
| `check_memfiles` | File-sharing tunnel online? ngrok URL? | Application state (memfiles plugin) |
| `check_mcp` | Wrapper health + per-server diagnosis (connected/disconnected/disabled, disclosure, hints) | Application state (MCP wrapper + external servers) |
| `check_watchdog` | Plugin process PID alive? restart count? | Process layer |

The watchdog only monitors processes — it does not introspect application state. Each plugin owns its own runtime health check. Missing deps are recorded as warnings — Slife still starts; affected MCP servers won't work.

## Project Structure

```
slife/
  agent/               # LLM interaction
    loop.py            #   Function-calling loop (streaming, concurrent tool execution, trim)
    service.py         #   Lifecycle manager (plugins, inbox, model switching)
    conversation.py    #   Message storage + history (OpenAI-format, sanitization, repair)
    llm_client.py      #   Backend router + StreamChunk
    system_prompt.py   #   Prompt rendering (static + dynamic Jinja2)
    templates/         #   system_prompt.j2, context_status.j2
    llm_backends/      #   API backends: openai.py, anthropic.py, openai_responses.py
    inbox.py           #   Unified message queue + ConversationStore
    plugins.py         #   Plugin spawn/stop helpers
    multimodal.py      #   Image encoding for vision models
  tools/               # Native tools (auto-discovered, 54)
    base.py            #   Tool ABC
    registry.py        #   ToolRegistry
    factory.py         #   Auto-discovery (pkgutil.iter_modules)
    system.py          #   System health, embedding/wechat checks
    exec.py            #   Shell, Python, package install
    skill.py           #   Skill management (SKILL.md)
    cli.py             #   External CLI tool management
    rest_api.py        #   REST API tool management (OpenAPI → MCP)
    a2a.py             #   Agent-to-agent tools (13)
    models.py          #   Model management (list/add/remove/switch)
    config.py          #   Config env var + native tool toggles
    credentials.py     #   Credential check/inject/uninject
    memfiles.py        #   File save / expose / include_image
    display.py         #   Inline image display
    meta.py            #   list_tools, check_async, cancel_async, clear_context
    _config_io.py      #   JSON5 read/write helpers
  memfiles/            # File serving infra
    token.py           #   File-backed JSON token registry (atomic writes)
    tunnel.py          #   Ngrok tunnel lifecycle (official SDK, embedded agent)
  plugins/             # Built-in plugins (auto-discovered server.py packages)
    mcp/               #   External MCP gateway (raw JSON-RPC: stdio/SSE/streamable)
    memdb/             #   Diary database (store, search, embeddings, schema.sql)
    wechat/            #   WeChat messaging (iLink ClawBot client)
    memfiles/          #   Plain-HTTP file server
  mcp/                 # MCP client infra
    client.py          #   Streamable HTTP client
    tool_adapter.py    #   MCPProxyTool (bridges MCP → Tool ABC)
    process.py         #   MCPWrapperProcess (spawn, port handshake)
    oauth.py           #   OAuth 2.0 device-code flow
  a2a/                 # Agent-to-Agent
    transport.py       #   Abstract transport + TransportMessage
    mqtt.py            #   MQTT adapter (paho-mqtt, MQTTv5, LWT)
    http.py            #   HTTP Streamable transport (skeleton)
    client.py          #   A2A client (presence, heartbeat, task routing)
    broker.py          #   Broker TCP probe
    task_store.py      #   In-memory task records
    card.py            #   AgentCard + format_presence_line (TUI/context shared)
    config.py          #   A2A config
    identity.py        #   AgentId, HUMAN/WECHAT sentinels, AgentMessage
    tools.py           #   Back-compat re-export of slife.tools.a2a
  subagent/            # Local workers
    headless.py        #   Headless JSON-RPC 2.0 process
    process.py         #   SubagentProcess + SubagentManager
  ui/                  # Textual TUI
    app.py             #   Textual App, bindings, HistoryInput, StatusBar
    chat.py            #   Chat message widgets
    handler.py         #   TUIHandler (bridges events → widgets)
    tool_display.py    #   ToolCallWidget + display helpers
    image_utils.py     #   Image rendering (Sixel/Halfcell/fallback)
    restore.py         #   Session restore (rebuilds UI from diary)
    approval_dialog.py #   Tool approval modal
    slife.tcss         #   Textual CSS
  config.py            # JSON5 config parsing (models, env, plugins, A2A, subagent)
  paths.py             # Filesystem paths (dev vs prod, data dir, DB, memfiles)
  platform.py          # OS detection, shell detection, process lifecycle, notifications
  logfmt.py            # Structured logging + secret sanitization
  server_utils.py      # Plugin lifecycle: port binding, signal, FastMCP helpers
  bootstrap.py         # Logging setup, skill seeding, console restore
  health.py            # External dependency checks (node, npm, bun, uv)
  env.py               # ${VAR} environment resolution
  os_detect.py         # OS path detection for install scripts

credstore/
  credstore/
    __init__.py        # Python API (get/set/delete/exists/list, keyring: URIs)
    __main__.py        # CLI (11 commands)
    _store.py          # CredentialStore
    _backend.py        # Dual-write: system keyring + cryptfile backup
    _platform.py       # WSL detection
    _wsl_backend.py    # PowerShell bridge → Windows Credential Manager
    _keyutils_backend.py # Headless Linux: kernel keyring via ctypes
    _enumerate.py      # Credential enumeration (Win/WSL)
    _resolver.py       # keyring: URI resolution
    _shell.py          # Shell formatting (export/unset) + persistence
    _config.py         # Config file loading
    _tty.py            # Masked terminal input

skills/                # On-demand SKILL.md skills (seeded to ~/.slife/skills/)
```

## License

MIT
