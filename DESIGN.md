# Slife Design

## Philosophy

### Minimum Harness

The harness does only what the LLM physically cannot:

1. **Execute tools** — the LLM requests function calls; the harness runs them and returns results.
2. **Maintain conversation state** — the harness holds the message list and feeds it back each turn.
3. **Stream responses** — the harness delivers tokens to the UI as they arrive.
4. **Persist memory** — every message, thinking block, and tool output is saved immutably. The LLM decides what to recall and when.

Everything else — reasoning, planning, tool selection, error recovery, coordination — is the LLM's job. The harness does not route, validate, retry, or second-guess.

### Negative Space

What Slife deliberately is not:

- **Not a framework** — no agent composition, pipelines, or orchestration abstractions
- **Not a safety system** — no guardrails, approval gates, or sandboxing beyond the OS
- **Not an automation engine** — no scheduled tasks, background workers, or event triggers

It's a chat window with tools. The LLM is in full control.

## Lean System Prompt

The system prompt is a **runtime spec sheet** — a flat list of facts the LLM cannot
discover from training data or tool schemas.  Two-part design:

- **Static** — ``slife/agent/templates/system_prompt.j2``, rendered once at startup
  by ``slife/agent/system_prompt.py`` → ``build(config)``.  Never changes —
  maximal prompt cache hit rate.  Includes model, CWD, and shell as the
  startup baseline.
- **Dynamic** — ``slife/agent/templates/context_status.j2``, re-rendered before
  each API call by ``build_context_status()``.  Injected as a synthetic
  ``_context_status`` harness tool-call + result pair (same pattern as
  ``_trim_context``) via ``Conversation.insert_context_status()``.

The harness-tool injection means:
- **System message never changes** → full prompt cache hit rate.
- **Status pairs are saved to diary** alongside turn messages → preserved in
  permanent memory, recoverable on session restore.
- **Consistent pattern** — ``_context_status`` and ``_trim_context`` are both
  harness notifications, not in the tool schema, ``_``-prefixed.

### Static Template (``system_prompt.j2``)

Seven numbered sections of platform facts, plus conditional A2A:

1. **环境** — agent identity, model, host, platform, CWD, shell, Python, package manager
2. **上下文窗口策略** — floor/ceiling percentages, trim behaviour, result cap
3. **图像与多模态** — ``[image:<path>]`` marker contract, vision support flag
4. **凭证解析链** — ``${VAR}`` syntax, resolution order, active credstore backend
5. **工具与技能** — MCP tool prefix, skills directory, skill loading via ``use_skill``
6. **子代理特性** — process model, tool inheritance, no memory persistence
7. **数据目录** — data dir, config path, logs, DB, skills, image cache
8. **多代理通信 (A2A)** — transport, broker host:port (conditional on config)

### Dynamic Template (``context_status.j2``)

Injected before each API call.  Two categories of content:

**Always shown** — changes every turn:
- **当前时间** — current datetime with UTC offset
- **上轮 Token** — last API call's ``total_tokens`` and percentage of context
  window (hidden on the first turn)

**Conditional** — only shown when changed since last turn, with distinct
wording to avoid confusion with the static baseline:
- **已切换模型** — display name, context window, input modalities
- **已在新的工作目录** — ``os.getcwd()``
- **已切换到 Shell** — detected shell

The conditional fields are tracked by ``AgentLoop`` via ``_last_*`` instance
variables.  On the first turn all three are emitted (since they differ from
the initial empty state).  On subsequent turns only changes appear.

### Design Principles

1. **Project-specific only.** If the LLM can infer it from tool schemas or training data, it doesn't belong in the prompt.
2. **Tool schemas over prompts.** Usage instructions live in function `description` and `parameters` — the prompt never repeats what a schema already says.
3. **Not a job description.** No personality, no tone, no "you are a helpful assistant."
4. **No slash commands.** The user communicates in natural language. The UI is a plain text input — the LLM decides what the user means and which tool to call.
5. **No tool-call instructions.** The prompt describes *mechanisms* (context trimming, credential sanitization, image markers) — never *how* to use a specific tool.
6. **Static baseline + change notifications.** ``build(config)`` renders platform constants at startup (including model, CWD, shell). ``build_context_status()`` emits time and token every turn, plus model/CWD/shell only when they change — with "已切换/已在新的" wording to signal a delta, not a duplicate.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  UI (Textual TUI)                                                    │
│  slife/ui/app.py, chat.py, handler.py, tool_display.py               │
├──────────────────────────────────────────────────────────────────────┤
│  Agent Service                                                       │
│  slife/agent/service.py — wires client + tools + loop + plugins      │
│  Manages MCP, Memory, A2A/MQTT, WeChat, and subagent lifecycles      │
│  Inbox: serializes human + WeChat + MQTT + subagent messages         │
├──────────────────────────────────────────────────────────────────────┤
│  Agent Loop                              │  MCP Client               │
│  Streaming function-calling              │  Streamable HTTP transport │
│  _context_status + _trim_context         │  OAuth support             │
│  harness notifications                   │  Tool proxy + adapter      │
│  Reasoning (thinking) support            │                           │
├──────────────────────────────────────────┴───────────────────────────┤
│  Tool Registry — unified function definitions for all categories     │
│  Native · Memory · Skills · MCP Proxy · CLI · REST API · A2A         │
├──────────────────────────────────────────────────────────────────────┤
│  Plugins (independent child processes, Streamable HTTP)              │
│  slife-mcp (gateway)  ·  slife-memory (diary)  ·  slife-wechat      │
├──────────────────────────────────────────────────────────────────────┤
│  Platform (slife/platform.py)  │  Config (JSON5)  │  Health checks   │
├──────────────────────────────────────────────────────────────────────┤
│  Credstore — OS keyring + cryptfile backup                           │
│  Win · Mac · Linux (SecretService / keyutils) · WSL (PowerShell)     │
└──────────────────────────────────────────────────────────────────────┘
```

## Agent Loop

Single function-calling loop. All tools are registered as OpenAI function definitions in one `ToolRegistry`. The LLM decides what to call and when.

```
User Input → Conversation.add_user_message()
  → loop: trim oldest turns if > 80% window (inserts visible _trim_context notification)
    → LLM stream → thinking/text chunks → handler callbacks
    → tool calls? → ToolRegistry.execute() → sanitize_secrets() → loop
    → no tool calls? → response text → return
    → save turn to diary
```

- **Streaming**: thinking and text tokens emitted in real-time via `AgentEventHandler` callbacks
- **Tool accumulation**: tool call deltas accumulated across streaming chunks, executed as a batch
- **Tool result images**: `_scan_for_images()` detects ``[image: <path>]`` markers in tool output and fires ``handler.on_image()`` for TUI rendering
- **Tool timeout**: `asyncio.wait_for()` wraps every tool call (default 60s). The LLM can override per-call via `_timeout`. Timeouts return `"Error: …"` strings — never silent, never crash the loop
- **Iteration limit**: `max_iterations` (default 30) prevents infinite loops. When exceeded, returns `AgentResult(cancelled=True)` with accumulated token usage preserved.
- **Cancellation**: `Esc` sets a cancel event. The loop stops at the next iteration boundary, returning `AgentResult(cancelled=True, usage=total_usage)` — partial token costs are retained.
- **Context tracking**: ``_last_context_tokens`` is updated at the end of every turn (normal, cancelled, or max-iterations) with ``_last_usage.prompt_tokens`` — the accurate input token count from the last API call of that turn. Used for precise 80% ceiling detection on the next turn.
- **Orphan repair**: interrupted tool calls are repaired before the next user message

### Context Window Management

Active conversation stays within `context_floor`–`context_ceiling` of the model's
context window (default 20%–80%):

```
                context_window
┌──────────────────────────────────────────────────────────────┐
│   trimmed (already saved     │  current context      │  headroom  │
│   in diary — recall via       │  floor ~ ceiling      │  1-ceiling │
│   memory_search)              │  working memory       │            │
└──────────────────────────────────────────────────────────────┘
```

- **Save**: after each turn, the turn is saved as a new diary row. No trimming happens here.
- **Detect**: uses ``_last_context_tokens`` — the accurate ``prompt_tokens`` from the last API call of the previous turn (cached in memory, not stored in DB). Falls back to ``count_tokens()`` for the first turn of a session. No per-call estimation overhead on the hot path.
- **Trim**: when the ceiling is exceeded, oldest complete turns are removed via ``extract_oldest_turns()`` using ``count_tokens()`` to re-measure after each removal. A synthetic ``_trim_context`` tool-call + result pair is inserted after the system prompt so the LLM sees a visible notification. Trimmed turns were already persisted when they completed, so the notification guides the LLM to use ``memory_search`` for retrieval. After trimming, ``_last_context_tokens`` is updated to the new ``count_tokens()`` value.
- **Subagent trim**: subagents also trim via the same code path, but with `memory_enabled=False`. Trimmed turns from subagents are discarded (ephemeral by design). The notification text reflects this — no mention of `memory_search`.
- **Tool result ceiling**: single tool results are capped at 20% of the context window.
- **Restore**: on restart, recent turns are loaded directly from SQLite. The budget is `context_floor × context_window`. Each turn's incremental token cost is estimated from message text (`len(content)//3`, the same heuristic as `count_tokens`), and turns are selected newest-first until the budget is exhausted. Always at least one turn is kept. The stored `diary.token_count` is **not** used for budgeting — it can be zero (cancelled/error turns) or cumulative (double-counts when summed).

### Token Counting Model

Four layers, each with distinct semantics:

```
API call:  stream_usage               →  _last_usage (per-request, overwritten each stream)
Turn:      sum of all API calls       →  diary.token_count (billed cost, cumulative)
Context:   last API's prompt_tokens   →  _last_context_tokens (accurate input size, in memory)
Session:   sum of all turns           →  status bar total (0 at launch)
```

- **Per API call**: ``stream_usage`` from the streaming LLM response — input + output tokens for that single request. Stored as ``_last_usage`` on ``AgentLoop`` (overwritten each iteration). Displayed in the dynamic context footer as "上轮 Token".
- **Per turn**: ``total_usage`` in the agent loop accumulates ``result.usage`` across all iterations (tool-call loops). Stored as ``diary.token_count`` at turn end — this is the **billed cost** of the entire turn. The dialog displays ``total_usage`` on each assistant message — it grows as the turn progresses.
- **Context snapshot**: ``_last_context_tokens`` caches ``_last_usage.prompt_tokens`` — the accurate **input token count** from the last API call of each turn. Used for the 80% ceiling check. Lives in memory (AgentLoop), not persisted. Unlike ``total_usage.total_tokens`` (which double-counts shared conversation history across tool-call iterations), ``prompt_tokens`` reflects the API's own measurement of what was actually in context.
- **Per session**: ``session_usage.total_tokens`` starts at 0 on launch (not restored from history). Incremented at turn end via ``save_to_memory`` with the turn's final ``token_count``.
- **Cancelled / max-iteration turns**: ``run()`` returns ``AgentResult(cancelled=True, usage=total_usage)`` — partial token usage from earlier API calls within the turn is preserved, no longer discarded as zero.

## LLM API Backends

Three backends, equal citizens — no conversion layer.  Each takes the internal
OpenAI-format messages (``Conversation`` storage) and handles its API natively:

```
LLMClient (thin router — ~50 lines)
  ├── OpenAIBackend         api: "openai-completions"
  ├── AnthropicBackend      api: "anthropic-messages"
  └── OpenAIResponsesBackend  api: "openai-responses"
```

### Design Principles

1. **No fourth format.**  The internal message format is OpenAI Chat Completions
   format — the format the codebase already uses.  No neutral/intermediate format is
   invented.

2. **No public conversion.**  The ``LLMClient`` exposes only ``chat()`` and
   ``chat_stream()``.  Backend-internal adaptation (e.g. OpenAI → Anthropic message
   format) is a private implementation detail, not a public API.

3. **Provider dispatch is automatic.**  ``ModelConfig.api`` determines which
   backend is instantiated.  ``LLMClient.__init__`` is a pure router — no
   ``_is_deepseek()``, no ``_build_kwargs()``, zero API format knowledge.

### Streaming

Every backend produces the same ``StreamChunk`` objects regardless of API.
The ``AgentLoop`` sees unified chunks with ``thinking``, ``content``,
``tool_deltas``, and ``usage`` — backend-agnostic.

### Model Management

Four native tools (``slife/tools/models.py``) provide runtime model management
without editing config files:

| Tool | Description |
|------|-------------|
| ``list_models`` | All configured models grouped by provider |
| ``add_model`` | Add/update a model (creates provider if new) |
| ``remove_model`` | Remove by ref; auto-switches if it was active |
| ``switch_model`` | Switch active model by ref |

### Credential Sharing

``credstore copy <source> <dest>`` duplicates a credential under a new key —
useful when multiple providers share the same API key.  Dual-writes to both
system keyring and cryptfile backup with the standard atomic pattern.

## Tool System

### Tool ABC

`Tool` (`slife/tools/base.py`) is the abstract base. Every tool defines `name`, `description`, `parameters` (JSON Schema), and `async execute(**kwargs) -> str`. Validation at class definition time via `__init_subclass__` — empty fields raise `TypeError` at import time.

### Auto-Discovery

`slife/tools/factory.py` uses `pkgutil.iter_modules` to import every module in `slife.tools.*`, then walks `Tool.__subclasses__()` to discover all valid tool classes. A new `.py` file is automatically picked up — no manual registry.

The `slife.json5` `tools` array is optional. Use it only to override defaults or disable tools.

### Tool Categories

All tools are unified under `Tool` and registered in a single `ToolRegistry`. The LLM sees no difference between categories — only function names and schemas. Each `Tool` subclass declares a ``category`` class attribute; ``list_tools`` groups output accordingly.

**Nine native categories** — one ``.py`` file per category in ``slife/tools/``, auto-discovered by ``pkgutil.iter_modules``:

| Category | File | Tools |
|----------|------|-------|
| System | `system.py` | `check_embedding`, `check_wechat`, `system_health`, `check_mcp_servers` |
| Execution | `exec.py` | `execute_shell`, `run_python_script`, `install_python_package` |
| Skills | `skill.py` | `check_skills_dir`, `list_skills`, `use_skill`, `add_skill`, `remove_skill`, `skill_set` |
| CLI | `cli.py` | `cli_check_installed`, `cli_add_tool`, `cli_remove_tool`, `cli_list_tools`, `cli_set_tool` |
| REST API | `rest_api.py` | `rest_api_add`, `rest_api_remove`, `rest_api_list`, `rest_api_set` |
| A2A | `a2a.py` | 13 tools — agent discovery, task routing, subagent lifecycle, broadcast |
| Config | `config.py` | `config_env_set`, `config_env_get`, `config_env_remove`, `native_tool_set` |
| Credentials | `credentials.py` | `credential_check`, `inject_credential`, `uninject_credential` |
| Meta | `meta.py` | `list_tools`, `check_async`, `cancel_async`, `clear_context`, `show_image` |
| Display | `meta.py` | `show_image` — display local image files inline in the chat |

Adding a new tool is a matter of adding a class in the matching category file — no manual registration needed.

Three former System tools were removed because their output is now in the system prompt or covered by other mechanisms:

- **`check_os_info`** — OS name, architecture, Python path and version are in section 1.
- **`check_shells`** — current shell is in section 1; executable paths are rarely actionable.
- **`check_workspace`** — CWD and package manager are in section 1; git status is covered by GitHub MCP; permissions are discoverable via ``execute_shell`` failures.

**Five managed categories** support dynamic registration with a standard **list / add / remove / set** surface — all `add` tools are idempotent upserts:

| Category | list | add (upsert) | remove | set |
|----------|------|-------------|--------|-----|
| **Native** | `list_tools` | — | — | `native_tool_set(name, enabled)` |
| **MCP** | `mcp_list_servers` | `mcp_add_server` | `mcp_remove_server` | `mcp_set_server(name, enabled, disclosure)` |
| **Skill** | `list_skills` | `add_skill` | `remove_skill` | `skill_set(name, enabled)` |
| **CLI** | `cli_list_tools` | `cli_add_tool` | `cli_remove_tool` | `cli_set_tool(name, enabled)` |
| **REST API** | `rest_api_list` | `rest_api_add` | `rest_api_remove` | `rest_api_set(name, enabled)` |

All `set` tools share `(name: str, enabled: bool)`. `mcp_set_server` additionally accepts `disclosure="lazy"|"eager"` for tool lazy-loading. `mcp_add_server` handles both initial registration and config updates — if called on an existing server with identical config, it returns `already_connected` without restarting.

In addition, the built-in **Memory** plugin (``slife/plugins/memory/``) provides `memory_search`, `memory_open`, `memory_list_recent`, `memory_summarize`, and more.

### Timeout Architecture

Single enforcement point at the Agent Loop level. `_timeout` is injected into **every** tool's JSON Schema as a universal per-call override.

For tools **without** a native ``timeout`` parameter the Agent Loop wraps execution in ``asyncio.wait_for(timeout=...)``. For tools **with** a native ``timeout`` parameter (e.g. ``execute_shell``), ``_timeout`` is mapped to the native ``timeout`` argument and the tool's own internal timeout logic takes over — no ``asyncio.wait_for`` wrapper, no double-layer nesting.

The MCP Client does not apply its own timeout.

## Plugin Architecture

Each plugin is an independent child process running a FastMCP server on a dynamically-assigned `127.0.0.1` port. Communication is via Streamable HTTP transport (MCP protocol, standard `mcp` library). Both client and server use the standard library — no monkey-patches.

### The Plugin Contract

A Slife built-in plugin must:

1. **Bind a free port and signal the parent** — `bind_free_port()` → `signal_port(port)` writes `{"port": N}` to stdout
2. **Start FastMCP on Streamable HTTP** — `mcp.run(transport="streamable-http", host="127.0.0.1", port=port)`
3. **Define `@mcp.tool` functions** — these become Slife tools
4. **Be importable** — `python -m <module>.server` must work

No base class, no import hook, no SDK. Just a FastMCP process.

### Infrastructure (shared startup path)

Every plugin follows the same path in `slife/agent/service.py`:

1. `MCPWrapperProcess(command, args).start()` → spawns subprocess, reads `{"port": N}` from stdout
2. `MCPClient.connect(url)` → `streamablehttp_client(f"http://127.0.0.1:{port}/mcp")`
3. `list_tools()` → discover tool schemas
4. `MCPProxyTool(mcp_client, tool_info)` → registered in ToolRegistry

Subagents share the main agent's plugin servers via environment variables (`SLIFE_MCP_PORT`, `SLIFE_MEMORY_PORT`, `SLIFE_WECHAT_PORT`).

### slife-mcp — External MCP Gateway

The unified gateway for all external MCP server connections. Dual transport:

| Transport | Mechanism | When |
|-----------|-----------|------|
| **stdio** | Spawn subprocess, JSON-RPC over pipes | Local MCP servers (npx/uvx) |
| **http** | POST JSON-RPC via `httpx.AsyncClient` | Remote MCP endpoints |

Both share `MCPServerConnection` — `_request()` dispatches to `_request_stdio()` or `_request_http()` based on `ServerConfig.transport`.

### Built-in vs. External

| | Built-in Plugin | External MCP Server |
|---|---|---|
| Connection | Direct via dedicated `MCPWrapperProcess` | Via slife-mcp proxy (`ConnectionPool`) |
| Config | Top-level or hardcoded | `mcp.servers.<name>` in slife.json5 |
| Tool prefix | `memory__tool`, `wechat__tool` | `server_name__tool` |
| Use case | Slife-native services | Third-party tools |

Both use the same MCP protocol and `MCPProxyTool` adapter. The distinction is operational.

**Why separate processes:** if a plugin crashes, Slife continues. If Slife crashes, the plugin can save state. No in-process crash can race with writes to disk.

### MCP Server Lifecycle

MCP servers transition through states managed by `mcp_set_server` and `mcp_add_server`:

```
disabled ──[mcp_set_server enabled=True]──────→ enabled (connected, tools registered)
disabled ──[mcp_set_server disclosure="lazy"]─→ disabled (cannot set disclosure on disabled)
enabled  ──[mcp_set_server enabled=False]─────→ disabled (disconnected, tools unregistered)
enabled  ──[mcp_set_server disclosure="lazy"]─→ enabled (connected, tools unloaded)
lazy     ──[mcp_set_server disclosure="eager"]─→ enabled (tools loaded)
any      ──[mcp_add_server]───────────────────→ upsert (same config → no-op; different config → restart)
```

**Startup behavior:** All configured servers are loaded into the connection pool. Enabled servers connect and register tools immediately. Disabled servers are added to the pool but never connected — they appear in `mcp_list_servers` with `enabled: false` until explicitly enabled via `mcp_set_server`.

**Persistence:** All state changes persist to `slife.json5`. Disabled servers write `enabled: false` so they stay off across restarts. Disclosure mode is tracked via the `active` flag.

## Permanent Memory

Every turn is permanently recorded as an independent row. No session concept, no lifecycle — a continuous, time-ordered log. Runs as a built-in MCP plugin (`slife/plugins/memory/`).

### Schema

One row = one turn, plus associated image BLOBs:

| Table / Column | Purpose |
|--------|---------|
| `diary.user_message` | What the user said |
| `diary.messages` | Assistant response as OpenAI JSON array (thinking, tool calls, results, text) |
| `diary.summary` | 1–2 sentence gist (LLM-written via `memory_summarize`) |
| `diary.tags` | Comma-separated topic tags |
| `diary.created_at` | ISO 8601 with timezone |
| `diary.channel` | Source: `human`, `wechat`, or remote agent id |
| `diary.who_helped` / `what_model` | Agent identity + model used |
| `diary.token_count` | Tokens consumed by this turn (sum of all API calls within the turn) |
| `diary_images.image_id` | UUID (matches cache filename stem) |
| `diary_images.data` | Raw image binary (BLOB) |
| `diary_images.mime_type` | `image/png`, `image/jpeg`, … |
| `diary_images.file_name` | Original filename + size for reference |

Images are saved atomically by ``memory_save_turn`` — each turn
gets its text row plus any ``[image: …]`` markers extracted from
tool results and written as BLOBs.  Turns are saved
**unconditionally** (even on cancel / error / max-iterations) so
no conversation content is ever lost.

### Search

Three indexes: FTS5 (BM25 keyword), sqlite-vec `vec0` (cosine KNN), B-tree on `created_at` (time range).

| Mode | Backend | Best for |
|------|---------|----------|
| `grep` | `LIKE` + `instr()` | Exact strings |
| `fts5` | FTS5 + BM25 | Topic / keyword search |
| `hybrid` | FTS5 + vec0 → RRF | Semantic recall merged with keyword precision |
| `time` | Range scan | Browse by date |

Hybrid search uses Reciprocal Rank Fusion (RRF, k=60). Without an embedding backend, hybrid degrades to FTS5-only.

### Embedding

Three backends, configurable at runtime (no restart):

| Backend | Dep | Default model | Dim |
|---------|-----|---------------|-----|
| GGUF (local) | `slife[gguf]` | bge-m3 (Q4_K_M) | 1024 |
| Transformer (local) | `slife[transformer]` | BAAI/bge-m3 | 1024 |
| API (OpenAI-compatible) | Provider key | text-embedding-3-small | 1536 |

Long turns are chunked at paragraph boundaries (~500 tokens, 1-paragraph overlap). `memory_set_embedding` triggers background re-indexing.

### Session Restore

On startup, recent turns are read **directly from SQLite** — no MCP transport,
no plugin dependency. The UI shows history immediately; plugins start in
parallel. Turns are selected within a token budget (`context_floor ×
context_window`) using incremental cost estimates from message text, not stored
`token_count` values (which can be zero or cumulative). This decouples restore
from plugin health.

Image echo follows one rule: **the ``diary_images`` BLOB table is the single
source of truth — cache files in ``logs/images/`` are ephemeral display
artifacts** (Textual needs a real file to render).  Each stored
``[image: <path>]`` marker is resolved without trusting the disk: BLOB (by
filename stem) → written back to the images dir and rendered; no BLOB but the
marker file still exists (legacy turns) → rendered in place; neither → text
placeholder.  Restored images mount one-at-a-time on staggered timers after
their tool-call widgets (HalfcellImage needs its own compositor cycle).

### Agent Isolation

`--agent alice` creates `~/.slife/alice.db`. `author` is the primary isolation column. `diary_semantic` uses `author` as a vec0 partition key — KNN search is automatically scoped to one agent.

## A2A — Agent-to-Agent

Three transports, unified interface. The LLM sees one agent pool:

```
  a2a_list_agents / a2a_send_task
         │
  ┌──────┼──────┐
  │      │      │
 MQTT   HTTP   Subagent
(paho) (mcp)  (JSON-RPC stdin/out)
```

| Transport | Backend | Use |
|-----------|---------|-----|
| **MQTT** | paho-mqtt → asyncio.Queue, LWT | Remote peers over broker |
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

Messages are processed sequentially — only one AgentLoop runs at a time. Incoming messages never interrupt a running loop.

The queue has no priority or preemption. During long-running tasks (a 20-iteration AgentLoop may take several minutes), external channel messages accumulate and may time out at the transport layer (e.g. WeChat's 5–10s reply window). For time-sensitive channels, configure the channel's own timeout or retry handling accordingly — this is an explicit trade-off of single-threaded, LLM-driven processing.

### Subagent Transport

Local child-process workers. Always available — no config toggle.

- **headless.py**: Slife without TUI, JSON-RPC 2.0 over stdin/stdout
- **SubagentManager**: spawn/stop/list lifecycle, `max_subagents` limit
- **Memory isolation**: subagents don't connect to memory server (avoids deadlock)
- **Ephemeral**: subagents exist only while the parent runs. No persisted registry.
- **Nested prevention**: `SLIFE_SUBAGENT_NAME` env var blocks recursive spawning

## UI

Textual TUI with minimal chrome:

- **ChatView** — scrollable message container
- **UserMessage** — prefix-styled user text with optional image attachments
- **AssistantMessage** — streaming text with thinking blocks (collapsible)
- **ToolCallWidget** — collapsible amber headers with detail
- **TUIHandler** — bridges `AgentEventHandler` callbacks to Textual widgets
- **StatusBar** — model name, thinking indicator, token count
- **Auto-restore** — rebuilds last session's UI on startup

All user-facing text rendered with `markup=False` to prevent `MarkupError`.

### Image Display

Slife renders images inline in the chat via ``textual-image`` with a
two-tier detection strategy:

1. **Sixel** — full-colour bitmap protocol, used only on whitelisted terminals
   where Textual's compositor can render it (Windows Terminal, WezTerm,
   iTerm2, Kitty).  Sixel bypasses the character-cell texture atlas
   entirely — one atomic DCS escape sequence per image.
2. **HalfcellImage** — coloured Unicode half-block characters (``▀``),
   works in any true-colour terminal (VS Code, PyCharm, Warp, Alacritty,
   etc.).  Each cell is a unique (foreground, background) color pair
   that maps to an xterm.js texture atlas entry.  CSS dimensions are
   capped at 32×16 (full) and 20×10 (thumbnail) to keep atlas entries
   under ~500 — well below the threshold that triggers
   `xtermjs/xterm.js#4484 <https://github.com/xtermjs/xterm.js/issues/4484>`_
   (texture atlas cache corruption from large numbers of unique color
   combinations).
3. **Text placeholder** — ``🖼 filename (XX KB)`` when ``textual-image`` is
   not installed or the image file is invalid.

The detection runs at module-import time (before ``App.run()``) by checking
environment variables.  CSS ``overflow: hidden`` constraints prevent
Halfcell rendering from bleeding into docked widgets.

**User attachment**: prefix a file path with ``@`` in the input field::

    Look at this screenshot @D:\\Downloads\\error.png and tell me what's wrong

The ``@path`` is extracted, validated, displayed as a thumbnail in chat,
stripped from the text sent to the LLM, and the file paths are passed
through the images pipeline to ``Conversation.add_user_message()``.

**Agent/tool images**: the ``show_image`` native tool reads an image file,
writes it to the cache directory (``logs/images/{uuid}.ext``), and returns a
``[image: <path>]`` marker.  The agent loop's ``_scan_for_images()`` detects
the marker and calls ``handler.on_image()``, which mounts an inline image
widget.  MCP binary-data output follows the same marker path.

**Persistence**: when the turn ends (unconditionally — even on cancel or
error), ``save_to_memory`` scans tool results for ``[image: …]`` markers,
reads the corresponding cache files, and writes the raw binary as BLOBs into
the ``diary_images`` table alongside the turn text.  On session restore,
images are reconstructed from BLOBs (not cache files) for re-rendering.

**Vision guard**: ``AgentLoop.supports_vision`` (from model config
``input: ["text", "image"]``) gates the ``image_url`` encoding path.
Models without vision support receive a clear error instead of a 400
API response.

**Safety**: all image paths are validated (file exists, recognised
extension) before display.  The rendering chain is Sixel → HalfcellImage
→ text fallback; each step degrades gracefully if the library is absent
or the terminal doesn't support it.

## Credential & Configuration

### Two-Layer Architecture

```
┌──────────────────────────────────────────────┐
│  OS Keyring (credstore)                      │
│  Encrypted at OS level. Survives config.     │
│  credstore set <KEY>    ← masked stdin        │
│  credential_check <KEY> ← masked value        │
└──────────────────┬───────────────────────────┘
                   │ ${VAR} reference
                   ▼
┌──────────────────────────────────────────────┐
│  slife.json5 → env: section                  │
│  Plain config. Holds refs, not secrets.      │
│  config_env_set/get/remove                    │
└──────────────────────────────────────────────┘
```

### Backend Matrix

Credstore selects the best available backend automatically via keyring's priority system:

| Platform | Backend | Priority | Mechanism |
|----------|---------|----------|-----------|
| **Windows** | WinCredKeyring | 9.0 | Windows Credential Manager via pywin32 |
| **WSL** | WslBackend | 9.5 | PowerShell bridge → advapi32.dll CredReadW/CredWriteW (C# P/Invoke) |
| **macOS** | Keychain | 5.0 | macOS Keychain via `security` CLI |
| **Linux (desktop)** | SecretService | 5.0 | D-Bus Secret Service (GNOME Keyring / KWallet) |
| **Linux (headless)** | KeyutilsBackend | 1.5 | Linux kernel keyring via `add_key`/`keyctl` syscalls (ctypes) |

`WslBackend` (priority 9.5) beats `WinCredKeyring` (9.0) when both are installed on WSL — it fixes target-format and encoding issues by calling `advapi32.dll` directly via embedded C#. `KeyutilsBackend` (priority 1.5) provides a zero-dependency fallback on headless Linux where no D-Bus session is available.

### Platform Detection

`credstore/_platform.py` provides a single `is_wsl()` function used by both the WSL backend and the enumeration module. Detection checks for the WSL interop file (`/proc/sys/fs/binfmt_misc/WSLInterop`) and falls back to `/proc/version` kernel string inspection.

`slife/platform.py` provides cross-platform utilities: `IS_WINDOWS` flag, `get_os_info()`, `resolve_command()`, `build_python_command()`, `terminate_process()` (graceful → force-kill escalation), and `desktop_notify()` (native notifications on Windows/macOS/Linux).

### Credential Enumeration

`credstore/_enumerate.py` reads credential keys from the OS store without loading secret values:

| Platform | API |
|----------|-----|
| **Windows** | `win32cred.CredEnumerate` — pointers to CREDENTIAL structs |
| **WSL** | `powershell.exe` with inline C# `CredEnumerateW` via `advapi32.dll` |
| **Other** | Empty list (unsupported — re-run `credstore set` to populate cryptfile) |

`with_values=False` (the default) returns only key names — secret values are never decoded. Set `with_values=True` only for explicit sync operations like `reset-backup`.

### Secret Sanitization

Sanitization is applied at three chokepoints — all use the same pattern-masking engine:

1. **Inbound** — `Conversation.add_user_message()` on every user/external message before it enters the conversation.
2. **Tool arguments** — `Conversation.add_assistant_message()` on every `tool_calls[].function.arguments` before storing in history. These arguments re-enter the LLM context on subsequent turns.
3. **Outbound** — `AgentLoop._execute_tools()` on every tool result before it reaches the LLM, TUI, or conversation.

`sanitize_secrets()` in `logfmt.py` masks API key patterns (`sk-*`, `ghp_*`, Bearer tokens, 32+ char hex/base64 tokens) with `<MASKED>`. Both chokepoints use the same function — no divergence in masking behaviour.

> **Note:** Sanitization is credential-only (pattern masking). Slife does not implement semantic guardrails against instruction hijacking or jailbreak prompts. The built-in shell and python_exec tools are provided for power users; use them only in trusted environments.

### Design Decisions

| | OS Keyring | slife.json5 env: |
|---|---|---|
| **What lives here** | Actual secret values | `${VAR}` references + non-secret config |
| **Encryption** | OS-level (Keychain/Win DPAPI) | Plaintext file |
| **Who writes** | User via `credstore set` CLI | Agent via `config_env_set` |
| **Survives** | OS profile changes | Version control (no secrets) |

## Config Loading

`Config.from_json5()` (`slife/config.py`) parses in nine phases: Models → Env → Agent → MCP → Memory → A2A → Subagent → Tools → System Health. `${VAR}` and `${VAR:-default}` resolution works recursively through dicts and lists. `_resolve_secret()` is the shared lookup chain for `${VAR}` → `os.environ` → credstore.

## Project Structure

```
slife/
  agent/            # LLM interaction: loop.py, conversation.py, service.py, system_prompt.py
  tools/            # Native tools (auto-discovered): base.py, registry.py, factory.py
  plugins/          # Built-in MCP plugins: mcp/, memory/, wechat/
  mcp/              # MCP client infra: client.py, tool_adapter.py, process.py, oauth.py
  a2a/              # Agent-to-Agent: transport.py, mqtt.py, http.py, client.py, broker.py
  subagent/         # Local workers: headless.py, process.py, tools.py
  ui/               # Textual TUI: app.py, chat.py, handler.py, tool_display.py, image_utils.py
  config.py         # JSON5 config: models, env, MCP, memory, A2A
  paths.py          # Canonical filesystem paths (dev vs prod)
  platform.py       # Cross-platform utilities: OS detection, shell detection, platform type, process lifecycle, notifications
  logfmt.py         # Structured logging + secret sanitization
  server_utils.py   # Plugin lifecycle: port binding, signal, FastMCP
  bootstrap.py      # Logging setup, session init
  health.py         # System health checks (external deps, config, model)
  env.py            # Environment variable management
  os_detect.py      # OS detection for install/upgrade scripts

credstore/
  __init__.py       # Python API (get/set/delete/exists/list, keyring URI resolution)
  __main__.py       # CLI (10 commands)
  _store.py         # CredentialStore: get/set/delete/reset/list
  _backend.py       # Dual-write: system keyring + cryptfile backup
  _platform.py      # WSL detection (is_wsl)
  _wsl_backend.py   # WSL backend: PowerShell bridge → Windows Credential Manager
  _keyutils_backend.py  # Headless Linux: kernel keyring via ctypes (add_key/keyctl)
  _enumerate.py     # Platform-specific credential enumeration (Win/WSL)
  _resolver.py      # keyring: URI resolution
  _shell.py         # Shell formatting (export/unset for bash/zsh/pwsh/fish)
  _config.py        # Config file loading (credstore.json5)
  _tty.py           # Masked terminal input

skills/             # On-demand SKILL.md plugins (seeded to ~/.slife/skills/)
```

## License

MIT
