# Slife Design

## Philosophy

### Minimum Harness

The harness does only what the LLM physically cannot do:

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

It's a chat window with tools. The LLM is in full control — including of when to spawn subagents or delegate to remote peers.

## Lean System Prompt

**The system prompt contains only project-specific information not in the LLM's training data.**

The prompt is rendered from `slife/agent/templates/system_prompt.j2` via Jinja2. The LLM already knows function calling, shell syntax, error handling, and tool-use patterns. Teaching any of this is noise.

What the LLM cannot know (and the prompt provides):

- The `list_skills` / `use_skill` flow — a Slife-specific convention
- That `slife.json5` has an `env:` section for API keys and env vars
- That pre-configured MCP servers need no auth
- That MCP servers default to eager, with lazy as an option for large tool sets
- That `anyapi-mcp-server` converts OpenAPI specs to tools
- That `cli_add_tool` persists discovered CLIs across restarts
- That `config_env_set` accepts placeholders when a value isn't available yet
- That A2A agents are discovered via `a2a_list_agents` / `a2a_list_subagents`
- That `a2a_spawn_subagent` creates local workers for parallel computation
- That every conversation is permanently recorded and searchable via `memory_search`

### Design Principles

1. **Project-specific only.** If the LLM can infer it from tool schemas or training data, it doesn't belong in the prompt.

2. **Tool schemas over prompts.** Usage instructions live in function `description` and `parameters` — the prompt never repeats what a schema already says. The schema describes *what* the tool does; the prompt tells *when* to use it.

3. **Don't block on missing values.** When a tool or server needs an API key the user doesn't have yet, set a placeholder and move on. Never force the user to provide a key before work can proceed.

4. **Minimal is correct.** Every line must carry a fact the model has no other way to discover. If a line can be removed without losing project-specific knowledge, remove it.

5. **Not a job description.** No personality, no tone, no "you are a helpful assistant." The prompt is a lookup table for Slife-specific conventions.

6. **The conversation handles everything — no slash commands.** The user communicates with the LLM in natural language. If the user wants to quit, they say "quit." If they want to attach an image, they say "look at this image" and the LLM asks for the path. Every action goes through the conversation — the UI is a plain text input, no special syntax, no command parser, no `/` prefix convention. The LLM decides what the user means and which tool to call.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  UI (Textual TUI)                                                │
│  slife/ui/app.py, chat.py, handler.py, tool_display.py           │
├──────────────────────────────────────────────────────────────────┤
│  Agent Service                                                   │
│  slife/agent/service.py — wires client + tools + loop + MCP     │
│  Manages MCP, Memory, A2A/MQTT, and subagent lifecycles          │
│  Inbox: serializes human + MQTT + subagent messages               │
├──────────────────────────────────────────────────────────────────┤
│  Agent Loop                                                      │
│  slife/agent/loop.py — streaming function-calling                │
│  Emits: thinking chunks, text chunks, tool events                 │
│  Conversation: full context + automatic window trimming            │
├──────────┴──────────────┴──────────────┴─────────────────────────┤
│  Native Tools (auto-discovered from slife/tools/*)                │
│  shell.py  run_python_script.py  os_info.py  config_env.py       │
│  cli.py  skill.py  a2a.py                                        │
│                                                                   │
│  Memory Tools       Skills         MCP Tools        A2A Tools    │
│  slife/plugins/memory/  skills/ dir  slife/mcp/    slife/a2a/   │
│  (MCP service)      SKILL.md       (MCP proxy)      MQTT+subagent│
├──────────────────────────────────────────────────────────────────┤
│  LLM Client (AsyncOpenAI)                                        │
│  slife/agent/llm_client.py — streaming + thinking support         │
├──────────────────────────────────────────────────────────────────┤
│  Config (JSON5)                                                  │
│  slife/config.py — env resolution, model parsing, MCP/Memory cfg │
└──────────────────────────────────────────────────────────────────┘
```

### Plugin Architecture

Slife has a **plugin system** built on the MCP stdio protocol. A plugin is any FastMCP server spawned as a child process. It communicates via stdin/stdout, exposes tools through standard `list_tools` / `call_tool` MCP methods, and its tools are automatically registered in Slife's `ToolRegistry`.

All three (slife-mcp, slife-memory, slife-wechat) are built-in plugins using this exact mechanism:

```
Slife ── MCPClient (stdio) ──▶ MCPWrapperProcess ──▶ slife-mcp    (MCP proxy)
  │                          │                         ├── filesystem (npx)
  │                          │                         ├── fetch (uvx)
  │                          │                         └── ... (any MCP server)
  │                          │
  │                          └── MCPWrapperProcess ──▶ slife-memory (diary DB)
  │                          │                         └── ~/.slife/slife.db
  │                          │
  │                          └── MCPWrapperProcess ──▶ slife-wechat (WeChat)
  │                                                    └── iLink ClawBot API
  │
  └── MQTT ──── mosquitto ─── other Slife instances
  └── JSON-RPC 2.0 ─── subagent (headless)
```

#### The Plugin Contract

A plugin must:

1. **Be a FastMCP server** — `mcp = FastMCP("name")` with `mcp.run(transport="stdio")`
2. **Define one or more `@mcp.tool` functions** — these become Slife tools
3. **Be importable** — `python -m <module>.server` must work

That's the entire contract. No base class, no import hook, no SDK. Just a FastMCP stdio server.

#### Infrastructure (reusable)

Every plugin startup follows the same path in `slife/agent/service.py`:

```
1. MCPWrapperProcess(command, args, server_module).start()
   → asyncio.create_subprocess_exec(exe, *args, stdin=PIPE, stdout=PIPE)

2. MCPClient.connect_streams(process.stdout, process.stdin)
   → JSON-RPC over asyncio.Queue adapters + ClientSession

3. list_tools() → discover tool schemas

4. MCPProxyTool(mcp_client, tool_info, server="plugin_name")
   → registered in ToolRegistry
```

Key classes in `slife/mcp/`:

| Class | Role |
|-------|------|
| `MCPClient` (`client.py`) | stdio MCP connection — `connect_streams()`, `list_tools()`, `call_tool()` |
| `MCPProxyTool` (`tool_adapter.py`) | Adapts an MCP tool to Slife's `Tool` ABC. Sets `name`/`description`/`parameters` at instance level, tool names prefixed as `{server}__{tool}` |
| `MCPWrapperProcess` (`process.py`) | Child process lifecycle — `start()`, `create_client()`, `stop()` |

#### Harness Tools vs. LLM Tools

A plugin can register both programmatic tools and LLM-visible tools. Use naming conventions to distinguish them:

```python
# LLM-visible: auto-registered in ToolRegistry
@mcp.tool(name="my_search", description="Search my knowledge base")
async def my_search(query: str) -> str: ...

# Harness-only: filtered out by AgentService before registration
@mcp.tool(name="my_plugin_save", description="Save state (harness)")
async def my_plugin_save(data: str) -> str: ...
```

`AgentService._register_memory_tools()` shows the pattern: call `list_tools()`, filter out harness names from a set, wrap the rest in `MCPProxyTool`.

#### slife-wechat — WeChat iLink Bridge

Bi-directional WeChat messaging via the iLink ClawBot protocol. Enables
Slfe to receive and reply to WeChat messages from a personal account.

**Enable:** `wechat: { enabled: true }` in `slife.json5`.

**Architecture:**
```
Phone WeChat ──▶ iLink API ◀── slife-wechat (FastMCP stdio)
   ▲                ▲                │
   │                │                ├── poll_updates() — long-poll getupdates (3s)
   │                │                ├── send_message() — reply via sendmessage
   │                │                └── send_typing() — typing indicator on phone
   │                │
   └── reply received ───────────────┘

Service-side (AgentService._wechat_poll_loop):
  1. call_tool("check_messages") every 5s → drains pending queue
  2. send_typing(status=1) → "typing…" on phone
  3. agent_loop.run() → LLM processes
  4. send_message → iLink → phone
  5. send_typing(status=2) → hide typing indicator
```

**Data flow:** incoming messages follow the official iLink bot pattern:
`getupdates → getconfig → sendtyping(1) → AI → sendmessage → sendtyping(2)`

**Session management:** bot token is saved in `wechat_<user>.json5` (gitignored).
Auto-restored on startup via `check_status` → `try_restore_session()`.
Session max age: ~23 hours, after which re-login (QR scan) is required.

**No user_id config needed:** the WeChat user ID (`from_user_id`) and
`context_token` are extracted from incoming messages — no manual configuration.

**Per-call aiohttp sessions** eliminate event-loop-closed errors that occur
when FastMCP's anyio-based event loop management is incompatible with
cached `aiohttp.ClientSession` instances.

**Reference:** [SiverKing/weixin-ClawBot-API](https://github.com/SiverKing/weixin-ClawBot-API) (MIT).

### Third-Party Plugins

Third-party plugin auto-loading from `slife.json5` is not yet implemented.
Currently the three built-in plugins (slife-mcp, slife-memory, slife-wechat)
are hardcoded in `AgentService`.  The infrastructure — `MCPWrapperProcess`,
`MCPClient`, `MCPProxyTool` — is generic and ready for external plugins once
the config-driven startup loop is added.

#### Plugin vs. MCP Server

| | Plugin | MCP Server (via slife-mcp) |
|---|---|---|
| Connection | Slife directly (stdio) | Via slife-mcp proxy |
| Config section | `plugins` | `mcp.servers` |
| Transport for downstream | N/A (no downstream) | stdio + HTTP |
| Tool prefix | `plugin_name__tool` | `server_name__tool` |
| Use case | Extend Slife itself | Third-party tools (filesystem, APIs) |

Plugins are Slife-native extensions. MCP servers are external tools. Choose a plugin when you're building something Slife-specific (like memory); choose an MCP server when you're connecting an existing service.

**Why separate processes:**

If a plugin crashes, Slife continues. If Slife crashes, the plugin observes the disconnection and can save state. No in-process crash can race with writes to disk. Both plugins are part of the slife source tree — they share the same repo, the same test suite, and the same release cycle.

## Agent Loop

Single function-calling loop. All tools — native functions, MCP tools, memory tools, A2A tools, skills — are registered as OpenAI function definitions in one `ToolRegistry`. The LLM decides what to call and when.

```
User Input → Conversation.add_user_message()
  → loop: LLM stream → thinking/text chunks → handler callbacks
    → tool calls? → ToolRegistry.execute() → Conversation.add_tool_result() → loop
    → no tool calls? → response text → return
    → save turn to diary (permanent memory)
    → trim context if > 80% window (oldest turns → diary, keep 20%)
```

- **Streaming**: thinking and text tokens are emitted in real-time via `AgentEventHandler` protocol callbacks. The TUI renders them as they arrive.
- **Tool accumulation**: tool call deltas are accumulated across streaming chunks, then deserialized and executed as a batch.
- **Iteration limit**: `max_iterations` (default 10) prevents infinite loops.
- **Orphan repair**: if the user interrupts mid-tool-execution, orphaned tool calls without results are repaired before the next user message to keep the conversation well-formed for the API.

### Context Window Management

The active conversation stays within 20%–80% of the model's context window:

```
                context_window (e.g. 131072 tokens)
┌──────────────────────────────────────────────────────────────┐
│   trimmed (in diary)    │  current context      │  headroom  │
│   recall via            │  20% ~ 80%            │  20%       │
│   memory_search         │  working memory       │            │
└──────────────────────────────────────────────────────────────┘
                           ↑                      ↑
                       floor=0.2             ceiling=0.8
```

- **Save**: after each turn, the turn is saved as a new row in memory. The active
  context is then trimmed if it exceeds the ceiling.
- **Trim**: if tokens exceed `context_ceiling × window`, oldest complete turns are
  removed until tokens ≤ `context_floor × window`. Turns are never split — a turn
  starts with a user message and includes all following assistant and tool messages.
- **Tool result ceiling**: a single tool result (file read, web fetch, API response)
  is capped at `tool_result_ceiling × context_window`. Default 0.2 (20%). Set to 0
  to disable. Exceeded results are truncated with a notice.
- **Restore**: on restart, recent turns are loaded by rowid and the conversation is
  reconstructed. No `trim_count` needed — each turn is its own immutable row.

Configure in `slife.json5`:
```json5
agent: {
    max_iterations: 10,
    context_floor: 0.2,
    context_ceiling: 0.8,
    tool_result_ceiling: 0.2,   // max single tool result = 20% of context window
}
```

## Tool System

### Tool ABC

`Tool` (`slife/tools/base.py`) is the abstract base. Every tool must define:

- `name` — unique identifier
- `description` — what the tool does (goes to the LLM)
- `parameters` — JSON Schema for function arguments
- `async execute(**kwargs) -> str` — run the tool

Validation happens at class definition time via `__init_subclass__` — a tool with empty `name`, `description`, or `parameters` raises `TypeError` at import time, not at runtime.

`Tool.to_openai_function()` converts the tool to the standard OpenAI function definition format. `Tool.from_config(cfg, config)` creates a tool instance from config overrides — subclasses override this to accept constructor parameters like `timeout` or `skills_dir`.

### Auto-Discovery

Tool loading (`slife/tools/factory.py`) uses `pkgutil.iter_modules` to import every module in `slife.tools.*`, then walks `Tool.__subclasses__()` recursively to discover all valid tool classes. No manual registry — a new `.py` file in `slife/tools/` is automatically picked up.

The `slife.json5` `tools` array is optional. Use it only to:
- Override defaults: `{name: "execute_shell", timeout: 60}`
- Disable a tool: `{name: "list_skills", enabled: false}`

Config overrides match by `Tool.name`. A2A tools are skipped when A2A is not enabled (`requires_a2a = True`).

### Tool Categories

Slife has six categories of tools, all unified under `Tool` and registered in a single `ToolRegistry`. The LLM sees no difference between them.

#### 1. Native Tools

Built-in tools implemented directly in Python, auto-discovered from `slife/tools/*.py`:

| Tool | Implementation |
|------|---------------|
| `execute_shell` | `asyncio.create_subprocess_shell` with configurable timeout |
| `run_python_script` | Platform-correct Python invocation with JSON arguments |
| `get_os_info` | Current OS name for platform-specific shell syntax |
| `config_env_set` / `get` / `remove` | Manage env vars in slife.json5 + os.environ |
| `cli_add_tool` / `check_installed` / `remove` / `list` | CLI discovery and registration management |

#### 2. Memory Tools

Seven tools implementing the full memory lifecycle. The memory service runs as a **built-in MCP plugin** (`slife/plugins/memory/`), discovered through the same `MCPClient` + `MCPProxyTool` pattern as all other MCP tools.

| Tier | Tool | Visibility | Description |
|------|------|-----------|-------------|
| Harness | `memory_open_diary` | Programmatic only | Start new conversation or detect interrupted one |
| Harness | `memory_close_diary` | Programmatic only | Mark conversation complete, generate embedding |
| Harness | `memory_update_diary` | Programmatic only | Save conversation after each turn |
| Summary | `memory_list_recent` | LLM | Browse recent sessions (titles + summaries) |
| Search | `memory_search` | LLM | Four modes: grep, fts5, hybrid, time |
| Load | `memory_open` | LLM | Load full session by rowid |
| Load | `memory_summarize` | LLM | Add title, summary, tags, key moments |

Harness-level tools are called programmatically by `AgentService` — they're never exposed to the LLM.

#### 3. A2A Tools

13 auto-discovered tools in `slife/tools/a2a.py` implementing the full A2A protocol — discovery, task routing, lifecycle, and notifications. Transport resolution is lazy: tools look up the live `A2AClient` and `SubagentManager` at call time via module-level references set by `AgentService`.

| Tool | Description |
|------|-------------|
| `a2a_list_agents` | Discover remote MQTT peers |
| `a2a_list_subagents` | List local subagent workers |
| `a2a_send_task` | Send task and wait for result (sync) |
| `a2a_send_task_async` | Fire-and-forget, returns task ID |
| `a2a_get_task_result` | Poll task status from TaskStore |
| `a2a_list_tasks` | Filterable task listing across all agents |
| `a2a_cancel_task` | Best-effort cancellation |
| `a2a_subscribe_task` | Block until task completion (event-driven or poll) |
| `a2a_agent_card` | Agent introspection (local or remote) |
| `a2a_spawn_subagent` | Create local worker with same LLM + tools |
| `a2a_stop_subagent` | Stop a locally-managed subagent |
| `a2a_notify_user` | Fire desktop notification |
| `a2a_broadcast` | Scatter/gather — send to all known agents |

Transport routing is subagent-first (fast, local), MQTT fallback (network). The LLM never needs to know which transport a given agent uses.

#### 4. Skills

On-demand documentation plugins. Four tools in `slife/tools/skill.py`:

| Tool | Description |
|------|-------------|
| `list_skills` | Discover available SKILL.md files |
| `use_skill` | Load a skill's full markdown body into context |
| `add_skill` | Install a skill from files or archive |
| `remove_skill` | Remove an installed skill |

Skills use progressive disclosure — a lightweight list first, full content only when requested.

#### 5. MCP Tools

External MCP servers connected through slife-mcp, adapted via `MCPProxyTool` and registered with a `{server}__` prefix (e.g. `filesystem__read_file`, `serper__search`). Supports progressive disclosure via `disclosure: "lazy"`.

#### 6. CLI Tools

External CLI commands the LLM discovers and registers. The tools (`cli_add_tool`, etc.) manage the discovery registry — actual execution goes through `execute_shell`. Registered CLIs are persisted in `slife.json5` and survive restarts.

## MCP Integration

### slife-mcp — MCP Proxy Plugin

slife-mcp is a built-in plugin that manages persistent connections to external MCP servers. It runs as a child process (stdio), spawned by Slife via `MCPWrapperProcess`.

```
                   stdio
slife agent  ←────────────→  slife-mcp (FastMCP)
                                  ├── filesystem MCP (npx stdio)
                                  ├── fetch MCP (uvx stdio)
                                  ├── remote MCP (HTTP POST)
                                  └── ... (any MCP server)
```

**Architecture rationale:** MCP servers are subprocesses. If managed in-process, a Slife crash would orphan them. A separate proxy process means MCP servers stay alive and can be shared across Slife instances.

### Slife side (`slife/mcp/`)

- **MCPClient** (`client.py`): connects via stdio (child process). Uses `asyncio.Queue` adapters to bridge subprocess pipes to MCP's `ClientSession`.
- **MCPProxyTool** (`tool_adapter.py`): adapts external MCP tools to Slife's `Tool` ABC. Sets `name`/`description`/`parameters` at instance level. Tool names are prefixed with the server name.
- **MCPWrapperProcess** (`process.py`): generic child process lifecycle management — start, create client from streams, graceful stop (stdin close → SIGTERM → SIGKILL escalation). Used identically for both slife-mcp and slife-memory.

**Startup flow:**
1. Spawn `slife.plugins.mcp.server` as child process via `MCPWrapperProcess.start()`
2. Connect via `MCPClient.connect_streams()` over stdio pipes
3. Discover wrapper management tools, create proxies
4. Auto-connect pre-configured servers in parallel; eager servers get their tools discovered immediately, lazy servers connect but skip registration

### slife-mcp side (`slife/plugins/mcp/`)

A FastMCP server running on stdio transport. Always spawned as a child process.

**Management tools:** `mcp_add_server` / `mcp_remove_server` / `mcp_list_servers` / `mcp_list_tools` / `mcp_check_server` / `mcp_set_disclosure` / `mcp_call_tool` / `mcp_reload`

**Connection pool** (`connection.py`): supports two transports for connecting to external MCP servers:
- **stdio**: spawn server as subprocess, raw JSON-RPC over pipes
- **http**: POST JSON-RPC to a Streamable HTTP MCP endpoint (with `mcp-session-id` header management)

No anyio, no `ClientSession` — avoids TaskGroup conflicts with FastMCP.

### Progressive Disclosure

Not all tools need to be in every LLM request. Slife uses a two-level pattern:

| Category | Summary Tool | Load Tool |
|----------|-------------|-----------|
| Memory | `memory_list_recent` / `memory_search` | `memory_open` |
| Skills | `list_skills` | `use_skill` |
| MCP/REST | `mcp_list_tools` / `mcp_list_servers` | `mcp_set_disclosure("eager")` |
| Native | Always loaded | — |
| CLI | Metadata-only (no schema cost) | — |

Memory search returns lightweight results (titles, snippets). `memory_open` loads the full session. Skills return a list first; `use_skill` loads the full markdown. MCP lazy servers connect but don't register tools until the LLM calls `mcp_set_disclosure("eager")`.

### REST APIs via anyapi-mcp-server

[anyapi-mcp-server](https://github.com/quiloos39/anyapi-mcp-server) converts any OpenAPI spec to MCP tools at runtime. It's configured as a regular MCP server:

```json5
github: {
  command: "npx",
  args: [
    "-y", "anyapi-mcp-server",
    "--name", "github",
    "--spec", "https://raw.githubusercontent.com/.../api.github.com.yaml",
    "--base-url", "https://api.github.com",
    "--header", "Authorization: Bearer ${GITHUB_TOKEN}",
  ],
}
```

Each endpoint becomes a tool named `{name}__{operationId}`. The pattern works for any REST API with an OpenAPI spec.

## Permanent Memory (slife-memory)

Every turn (user message + assistant response including thinking, tool calls,
and tool results) is permanently recorded as an independent row.  There is no
session concept, no lifecycle — memory is a continuous, time-ordered log of
every exchange.  The memory service runs as a **built-in MCP plugin**,
same architecture as slife-mcp.

### Architecture

```
                         MCP protocol (stdio)
slife agent ───────────────┼────────────────
                           │
                    ┌──────────────┐
                    │ slife-memory │  (built-in plugin)
                    └──────┬───────┘
                           │
                    ~/.slife/slife.db
                      ├── diary            (one row = one turn)
                      ├── diary_fts (FTS5) (keyword search, BM25 ranking)
                      └── diary_semantic   (vec0, cosine KNN on turn text)
```

### Why a Separate Process

```
slife crash ──→ slife-memory still alive ──→ turns already persisted
                                              │
Slife restart ──→ get_recent_turns() ──→ rebuild conversation
```

If memory were in-process, a crash would race with the final database write. A separate process observes the disconnection and marks the crash — no race window, no data loss.

**Important:** memory is saved *at the end of each turn*, not mid-turn. If Slife crashes or the user presses Ctrl+C while the LLM is still generating a response (tools running, thinking in progress), that turn is **not saved** — there is no partial write. Only completed turns are persisted. This is by design: an incomplete turn would be misleading when recalled later.

### Diary Schema

One row = one turn. No sessions, no status, no lifecycle — just time-ordered records.

```sql
CREATE TABLE diary (
    author         TEXT,     -- who (--user flag)
    user_message   TEXT,     -- what the user said
    messages       TEXT,     -- assistant response JSON (thinking, tool calls, results, text)

    summary        TEXT,     -- 1-2 sentence gist (LLM-written via memory_summarize)
    tags           TEXT,     -- comma-separated topic tags

    created_at     TEXT,     -- when this turn happened
    who_helped     TEXT,     -- agent name (--agent flag)
    what_model     TEXT,     -- model used
    token_count    INTEGER   -- tokens consumed by this turn
);
```

### Search Modes

| Mode | Backend | Best for | Example |
|------|---------|----------|---------|
| `grep` | SQLite LIKE | Exact strings, error messages, code | `"ConnectionError: timeout"` |
| `fts5` | FTS5 + BM25 | Topic/keyword search | `"MCP connection issue"` |
| `hybrid` | FTS5 + vec0 KNN → RRF | Semantic similarity, fuzzy recall | `"that memory leak fix"` |
| `time` | SQLite range scan | Browse by date, no query needed | `since="2026-07-14"` |

All modes search the full diary including the active session. The LLM can distinguish between results already in context and genuinely new findings — no need for the harness to pre-filter.

**Reciprocal Rank Fusion (RRF):** hybrid mode merges keyword results and semantic results with RRF, producing a single ranked list. If no embedding backend is configured, hybrid degrades gracefully to FTS5-only.

### Embedding

When a turn is saved, the full text content (user message + all assistant text +
all tool results) is concatenated and embedded via the configured backend.
If the concatenated text exceeds the model's token limit (8192 for most models),
the turn is **skipped** — no embedding is stored and semantic search won't find
it.  Keyword search (FTS5 / grep) is unaffected and continues to work normally.

No truncation.  Partial embeddings are misleading: an incomplete turn could
match semantically but miss the critical detail the user is actually searching
for.  Skipping is safer than truncating.

### What Gets Saved

Each turn writes one row — user_message + the assistant's response messages.
System prompt is NOT stored per-turn (it's reconstructed on restore from the
current config).  The `messages` JSON array contains:

| Content | In diary? | In API calls? |
|---------|-----------|---------------|
| User input (separate column) | ✅ | ✅ |
| Assistant thinking | ✅ | ❌ (stripped by `to_openai_messages()`) |
| Tool call name + arguments | ✅ | ✅ |
| Tool execution output | ✅ | ✅ |
| Assistant final response | ✅ | ✅ |
| Image attachments | ✅ | ✅ |

Thinking is stored in a `thinking` field on assistant messages — preserved
for memory recall, stripped before sending to the API.

### Embeddings

Semantic search (`hybrid` mode) uses vector embeddings via two configurable backends:

1. **Local GGUF model** (llama-cpp-python) — offline, no API cost, BGE-M3 by default (1024-dim)
2. **OpenAI-compatible API** — uses api_key from models.providers, text-embedding-3-small by default (1536-dim)

Embedding config is managed at runtime via `memory_check_embedding`, `memory_set_embedding`, and `memory_remove_embedding` — no restart needed.

### Session Recovery

Every restart automatically restores recent turns.  Since each turn is independently
saved, recovery is simply: load the most recent N turns by rowid, extract their
messages, rebuild the conversation.

1. `save_to_memory()` is called **once per turn**, after `agent_loop.run()` completes
   (i.e., after the LLM finishes its final response, not after each tool-call
   iteration).  It extracts the just-completed turn's messages and INSERTs a row.
2. If the user exits or crashes mid-turn — while the LLM is still calling tools,
   reasoning, or streaming — the turn is **not saved**.  Only completed turns are
   persisted.  On restart, the last partial turn is gone; work restarts from the
   end of the previous completed turn.
3. On restart, `get_recent_turns(author, limit=50)` returns the last 50 turns.
4. The UI rebuilds by concatenating all turn messages and recreating widgets.

No trim_count needed — each turn is its own row, immutable once written.
If no prior turns exist, starts fresh.

### User Isolation

Multiple users on the same machine are isolated by `--user`:

```bash
Slife --user alice              # alice's diary, alice's knowledge
Slife --user bob --agent bob    # bob's diary + A2A identity "bob"
```

`--user` and `--agent` are orthogonal:
- `--user` → memory isolation key (who owns the diary)
- `--agent` → A2A network identity (who I am on the MQTT mesh)

Every memory tool takes an `author` parameter. The `diary` table uses `author` as the primary isolation column. `diary_semantic` (vec0) uses `author` as a partition key — KNN search is automatically scoped to one user with zero cross-user overhead.

## A2A — Agent-to-Agent

Two transports, unified interface. The LLM sees one agent pool.

### Architecture

```
                    a2a_list_agents / a2a_send_task
                           │
            ┌──────────────┴──────────────┐
            │                             │
     MQTT Transport              Subagent Transport
     (--agent enables)            (always available)

  ┌─────────────────┐       ┌──────────────────────┐
  │ MQTT Broker      │       │ Parent Process        │
  │ (mosquitto)      │       │  SubagentManager      │
  │                  │       │  ├─ sub-1 (headless)  │
  │ slife/+/presence │       │  │  JSON-RPC stdin/stdout
  │ slife/+/inbox    │       │  ├─ sub-2 (headless)  │
  │ slife/+/result   │       │  │  JSON-RPC stdin/stdout
  └─────────────────┘       │  └─ ...               │
                            └──────────────────────┘
```

### MQTT Transport (`slife/a2a/`)

Remote Slife instances discover each other and delegate tasks over MQTT. Enabled via `--agent <id>` CLI flag.

- **MQTTAdapter** (`mqtt.py`): paho-mqtt → `asyncio.Queue` bridge with Last Will and Testament (instant offline detection)
- **A2AClient** (`client.py`): presence heartbeat, peer discovery (subscribe to `slife/+/presence`), task routing (publish to target inbox, listen on own result topic)
- **BrokerManager** (`broker.py`): optional mosquitto auto-spawn if not already running
- **TaskStore** (`task_store.py`): shared task-lifecycle tracking — every send, result, and cancellation across both transports, with status, timestamps, and result text

### Subagent Transport (`slife/subagent/`)

Local child-process workers spawned via `asyncio.create_subprocess_exec`. Always available — no config toggle needed.

- **headless.py**: Slife without TUI, JSON-RPC 2.0 over stdin/stdout
- **SubagentProcess**: pipe bridge + task dispatch, pending futures for async results
- **SubagentManager**: spawn/stop/list lifecycle, enforces `max_subagents` limit
- **Nested prevention**: subagents set `SLIFE_SUBAGENT_NAME` in their environment; `start_subagent()` checks for this and skips creation to prevent recursive spawning
- **Ephemeral by design**: subagents exist only while the parent process runs. When Slife exits, `SubagentManager.stop_all()` terminates every subagent. On restart, the LLM spawns fresh ones — there is no persisted subagent registry. This keeps subagents lightweight and stateless, with no cleanup burden across crashes.

### Unified Inbox

All messages — human keyboard input, MQTT tasks, subagent results — flow through a single `asyncio.Queue`:

```
Human keyboard ──→ Inbox.post() ──→ asyncio.Queue ──→ Inbox.run() ──→ AgentLoop
MQTT inbox msgs ──→ Inbox.post() ──→              ──→ ConversationStore
Subagent results ──→ Inbox.post() ──→              ──→ per-source convs
```

**ConversationStore**: the human's conversation persists across messages (continuous back-and-forth). Remote agent conversations are fresh each time (one-shot task model).

**Serialization**: the inbox processes messages sequentially — even if human and remote agents send simultaneously, only one `AgentLoop` runs at a time. While a loop is running, the agent card shows "busy."

### Remote Task UI Integration

Remote tasks stream to the chat view exactly like locally-typed messages. The source agent's name becomes the prompt prefix (`Jack> task…`), the LLM's thinking and response stream to the chat, and tool calls render as collapsible widgets. This is achieved through a handler factory pattern that creates fresh `TUIHandler` instances per task.

### Protocol

Subagent IPC uses JSON-RPC 2.0 per the A2A specification (§9):

```
→ {"jsonrpc":"2.0","method":"tasks/send","params":{"task":"…"},"id":"x"}
← {"jsonrpc":"2.0","result":"…","id":"x"}
```

MQTT transport uses topic-based publish/subscribe with the same task semantics.

## UI

Textual TUI in Claude Code CLI style: minimal chrome, dark theme, clean message display.

- **ChatView** — scrollable message container
- **UserMessage** — configurable prompt prefix; defaults to `> ` but shows the agent name when `--agent` is set. Remote tasks use the source agent's name.
- **AssistantMessage** — streaming text with optional thinking block (dim italic, truncated at 500 chars). Click to expand, Enter/Space to toggle.
- **ToolCallWidget** — collapsible tool call display with amber header and detail block. Single `Static` widget — all rendering via `Content` trees for safety.
- **TUIHandler** — bridges `AgentEventHandler` callbacks to Textual widgets in real-time
- **StatusBar** — model name, thinking indicator, token count, key bindings
- **Auto-restore** — on startup, rebuilds the last session's UI with full fidelity

All user-facing text (tool output, search results, file contents) is rendered with `markup=False` to prevent `MarkupError` from special characters.

## Config Loading

`Config.from_json5()` (`slife/config.py`) parses the JSON5 file in structured phases:

1. **Models**: dispatches between provider-dict and flat-list formats. Provider defaults (api_key, base_url, api) are inherited by each model. Duplicate model IDs within a provider raise an error.
2. **Env**: extracted and injected into `os.environ` so tools and subprocesses can reference values via `${VAR}`.
3. **Agent**: `max_iterations`, `context_floor`, `context_ceiling`, `tool_result_ceiling`.
4. **MCP**: built-in plugin — always enabled. External servers configured under `mcp.servers`; each can set `enabled: false` to skip auto-connect.
5. **Memory**: built-in plugin — always enabled. Embedding backend auto-detected — local GGUF takes priority over API; if neither is configured, semantic search degrades gracefully.
6. **A2A**: enabled only via `--agent` CLI flag. The `mqtt` config section provides broker connection details — it never auto-enables A2A.
7. **Subagent**: always available, configured with `max_subagents` and `task_timeout`.
8. **Tools**: optional override list — auto-discovery handles defaults.

`${ENV_VAR}` and `${ENV_VAR:-default}` resolution works recursively through dicts and lists.

## Project Structure

```
slife/
  __init__.py           # Entry point: main(), config loading, log setup
  config.py             # JSON5 config loading (ModelConfig, MCPConfig, MemoryConfig)
  env.py                # ${ENV_VAR} resolution
  platform.py           # OS detection, shell syntax (Windows/Unix)
  logfmt.py             # Structured logging with session/request IDs
  bootstrap.py          # Logging setup, session ID generation

  agent/                # LLM interaction layer
    loop.py             #   Function-calling while-loop with streaming
    llm_client.py       #   OpenAI-compatible streaming client (+ thinking)
    conversation.py     #   Message history + context window trimming
    service.py          #   Wiring: client + tools + loop + MCP + Memory + A2A
    system_prompt.py    #   Jinja2 template rendering
    multimodal.py       #   Image encoding for vision APIs
    inbox.py            #   Unified message queue (human + MQTT + subagent)
    templates/
      system_prompt.j2  #   Lean system prompt template

  a2a/                  # Agent-to-Agent (MQTT + subagent)
    identity.py         #   AgentId, AgentMessage
    card.py             #   AgentCard
    client.py           #   A2AClient — P2P mesh, presence, task routing
    mqtt.py             #   MQTTAdapter — paho-mqtt → asyncio bridge
    broker.py           #   BrokerManager — mosquitto lifecycle
    task_store.py       #   TaskRecord + TaskStore — lifecycle tracking
    config.py           #   A2AConfig (enabled via --agent)

  subagent/             # Local child-process workers
    headless.py         #   JSON-RPC 2.0 runner (no TUI)
    process.py          #   SubagentProcess + SubagentManager

  tools/                # Tool implementations (auto-discovered)
    base.py             #   Tool ABC with __init_subclass__ validation
    registry.py         #   Name → Tool lookup & execution
    factory.py          #   Auto-discovery via pkgutil + __subclasses__()
    a2a.py              #   13 A2A protocol tools
    shell.py            #   execute_shell
    run_python_script.py#   run_python_script
    os_info.py          #   get_os_info
    skill.py            #   list_skills / use_skill / add_skill / remove_skill
    config_env.py       #   config_env_set / get / remove
    cli.py              #   cli_add_tool / check_installed / remove / list
    _config_io.py       #   Shared JSON5 read/write helpers

  mcp/                  # MCP client + plugin infrastructure
    client.py           #   stdio client with asyncio.Queue adapters
    tool_adapter.py     #   MCP → Slife Tool adapter (MCPProxyTool)
    process.py          #   Child process lifecycle manager

  plugins/              # Built-in MCP plugins
    mcp/                #   slife-mcp — MCP proxy
      server.py         #     FastMCP server — 10 management tools
      connection.py     #     asyncio JSON-RPC connection pool (stdio + HTTP)
    memory/             #   slife-memory — diary database
      server.py         #     FastMCP server — 7 memory + 3 embedding config tools
      store.py          #     SQLite + FTS5 + vec0 hybrid search
      embeddings.py     #     GGUF local or OpenAI API embedding backend
      embedding_config.py #   Runtime embedding config management
      search.py         #     RRF (Reciprocal Rank Fusion) merge
      schema.sql        #     DDL — diary + FTS5 + vec0
    wechat/             #   slife-wechat — WeChat iLink bridge
      server.py         #     FastMCP server — 5 tools (login, send, check, status, logout)
      client.py         #     iLink ClawBot protocol client (QR, poll, send, typing)
      config.py         #     Per-user session persistence (wechat_<user>.json5)

  ui/                   # Textual TUI
    app.py              #   Main app (SlifeApp) — memory + recovery UI
    chat.py             #   Message widgets (ChatView, AssistantMessage)
    handler.py          #   Streaming event → UI bridge (TUIHandler)
    tool_display.py     #   Tool call rendering (ToolCallWidget)

skills/                 # On-demand skill plugins (SKILL.md per directory)
tests/                  # pytest suite (asyncio_mode=strict)
```

## The Knowledge Base Effect

The memory system IS a knowledge base. **Everything the agent encounters** — file contents, web search results, API responses, command output, errors, thinking, decisions — is permanently stored in `diary.messages` and indexed by FTS5 + vec0. Over time, this becomes a searchable archive of everything you and the agent have done.

No separate knowledge base, no external indexing pipeline. The conversation IS the knowledge base — every observation, reasoning trace, and decision is recorded in its original context and searchable through a single interface. Vector search is provided by the sqlite-vec extension inside the same SQLite database, not a separate service.

The LLM can recall its own past experience:

- `memory_search(mode="grep", query="ConnectionError")` — find every past occurrence of a specific error
- `memory_search(mode="fts5", query="MCP connection issue")` — find past discussions about a topic
- `memory_search(mode="hybrid", query="that memory leak we fixed")` — find the conversation where a bug was fixed
- `memory_search(mode="time", since="2026-07-14")` — browse everything from a specific date
