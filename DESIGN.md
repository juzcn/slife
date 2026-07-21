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
- That secrets live in the OS keyring (credstore), config lives in `~/.slife/slife.json5 → env:`
- That pre-configured MCP servers need no auth
- That MCP servers default to eager, with lazy as an option for large tool sets
- That `anyapi-mcp-server` converts OpenAPI specs to tools
- That `cli_add_tool` persists discovered CLIs across restarts
- That `config_secret_register` registers secrets (writes `${VAR}`, user runs `credstore set` in terminal)
- That `config_env_set` handles non-secret env vars (writes value or `<YOUR_VAR>` placeholder)
- That `config_env_set` **rejects values that look like secrets** (API key prefixes, token patterns, key names with KEY/SECRET/TOKEN) — use `config_secret_register` instead
- That **plaintext API keys in `slife.json5` are rejected at startup** — `api_key` fields must use `${VAR}` references
- That MCP server stderr is **sanitized** before logging — API key patterns are redacted
- That **all tool output is sanitized** before reaching the LLM — `sanitize_secrets()` in
  `logfmt.py` is the harness-level guard applied at `AgentLoop._execute_tools()`,
  the single chokepoint between tool execution and the LLM context
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
│  Manages MCP, Memory, A2A/MQTT, WeChat, and subagent lifecycles  │
│  Inbox: serializes human + WeChat + MQTT + subagent messages     │
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

Slife has a **plugin system** built on the MCP stdio transport (JSON-RPC over
stdin/stdout).  A plugin is an independent child process that uses FastMCP as a
server framework, but it is **not** a standard MCP service — it is a
Slife‑specific plugin that borrows MCP stdio as its IPC mechanism.  Tools are
exposed through the standard ``list_tools`` / ``call_tool`` methods and
automatically registered in Slife's ``ToolRegistry``.

All three (slife-mcp, slife-memory, slife-wechat) are built-in plugins using
this exact mechanism.  **slife-mcp is the gateway for external MCP services
only** — memory and wechat connect directly to Slife, not through the proxy:

```
                         ┌─ MCPWrapperProcess ── slife-mcp (gateway)
                         │    │
                         │    └── ConnectionPool ── external MCP servers
                         │         ├── filesystem (npx, stdio)
                         │         ├── fetch (uvx, stdio)
                         │         ├── remote-api (HTTP POST)
                         │         └── ... (any MCP server)
                         │
Slife ── MCPClient ──────┼─ MCPWrapperProcess ── slife-memory (direct)
  (stdio per plugin)     │    └── ~/.slife/<agent_id>.db
                         │
                         └─ MCPWrapperProcess ── slife-wechat (direct)
                              └── iLink ClawBot API

  MQTT ──── mosquitto ─── other Slife instances
  JSON-RPC 2.0 ─── subagent (headless)
```

#### The Plugin Contract

A Slife plugin uses **MCP stdio as its IPC transport**, but it is **not a
standard MCP service** — it cannot be consumed by arbitrary MCP clients.  It
is a Slife‑specific child process that happens to speak JSON-RPC over
stdin/stdout via FastMCP.

A plugin must:

1. **Be a FastMCP server on stdio** — `mcp = FastMCP("name")` with `mcp.run(transport="stdio")`
2. **Define one or more `@mcp.tool` functions** — these become Slife tools
3. **Be importable** — `python -m <module>.server` must work

That's the entire contract. No base class, no import hook, no SDK. Just a
FastMCP stdio process — the same technology, but a different contract from
standard MCP services.

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

Bidirectional WeChat messaging via the iLink ClawBot protocol.  Messages flow
through the unified inbox, get processed by the same agent loop, and replies
are routed back via the message's `on_reply` callback.

**Enable:** `wechat: { enabled: true }` in `slife.json5`.

| Feature | Detail |
|---------|--------|
| Transport | HTTP long-poll (3s interval) — `getupdates → getconfig → sendtyping → AI → sendmessage` |
| Session | Token saved in `wechat_<user>.json5`, auto-restored on startup, ~23h validity |
| TUI integration | Incoming messages appear as `Wechat> hi`, replies stream via shared `TUIHandler` factory |
| Typing indicator | Server-managed keep-alive (8s refresh) — plugin process handles it, harness never sees it |
| Config | No `user_id` needed — extracted from incoming messages |

**Tool separation** — follows the same harness/LLM pattern as slife-memory:

| Tier | Tools | Visibility |
|------|-------|------------|
| Harness | `wechat_drain_incoming`, `wechat_dispatch_reply` | AgentService poll loop — never exposed to LLM |
| LLM | `login`, `check_messages`, `send_message`, `send_typing`, `check_status`, `logout` | Full agent access for proactive messaging |

**Typing architecture:** when `wechat_drain_incoming` returns new messages, the
plugin automatically starts a per-conversation typing keep-alive task.  When
`wechat_dispatch_reply` sends the agent's response, it cancels the keep-alive
and hides the typing indicator.  The harness (AgentService) never touches typing
API calls — all wechat-specific logic is contained in the plugin process.

**Reference:** [SiverKing/weixin-ClawBot-API](https://github.com/SiverKing/weixin-ClawBot-API) (MIT).

### Third-Party Plugins

Third-party plugins are standard MCP servers — any program that speaks the MCP
stdio or HTTP protocol.  They are configured in `slife.json5` under `mcp.servers`
and auto-connected on startup via `AgentService._auto_connect_mcp_servers()`
(`slife/agent/service.py:215`).

Two transports are supported per server:

| Transport | Required fields | Protocol |
|-----------|----------------|----------|
| **stdio** | `command`, `args` | Spawns a local subprocess, communicates via stdin/stdout JSON-RPC |
| **HTTP** | `url` | POSTs JSON-RPC to a Streamable HTTP MCP endpoint |

#### Configuration

```json5
mcp: {
  servers: {
    "my-plugin": {                       // stdio example
      command: "uv", args: ["run", "python", "-m", "my_plugin.server"],
      env: { API_KEY: "${API_KEY}" },
      description: "My custom MCP server.",
    },
    "remote-api": {                      // HTTP example
      url: "https://api.example.com/mcp",
      headers: { Authorization: "Bearer ${TOKEN}" },
      description: "Remote MCP server over HTTP.",
    },
  },
}
```

Servers are connected **in parallel** at startup via `asyncio.gather`.  Eager
servers (default) have their tools discovered and registered immediately;
lazy servers (`disclosure: "lazy"`) connect but skip tool registration until
the LLM calls `mcp_set_disclosure("eager")`.

Dynamic management at runtime is also supported — the LLM can call
`mcp_add_server` to connect new servers (auto-persisted to `slife.json5`),
`mcp_remove_server` to disconnect and clean up, and `mcp_set_disclosure` to
toggle between eager and lazy modes.

#### The Plugin Contract

A third-party plugin must:

1. **Speak MCP** — implement the standard `initialize`, `tools/list`, `tools/call`
   JSON-RPC methods over stdio or HTTP.
2. **Define tools** — each with a `name`, `description`, and `inputSchema` (JSON Schema).
3. **Be launchable** — for stdio: `command` + `args` that start the process.

That's the entire contract.  No Slife SDK, no base class, no import hook.
Any MCP-compatible server — in Python, Node.js, Go, Rust, or any other language —
is a Slife plugin.

FastMCP (Python) is the recommended path for Python developers:

```python
from fastmcp import FastMCP
mcp = FastMCP("my-plugin")

@mcp.tool(name="hello", description="Say hello.")
async def hello(name: str) -> str:
    return f"Hello, {name}!"

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

#### Built-in Plugin vs. External MCP Server

| | Built-in Plugin | External MCP Server |
|---|---|---|
| Connection | Slife directly (stdio), dedicated `MCPWrapperProcess` | Via slife-mcp proxy (`ConnectionPool`) |
| Config section | Top-level (`memory:`, `wechat:`) or hardcoded | `mcp.servers.<name>` |
| Tool routing | Direct `MCPClient.call_tool()` | Routed via `mcp_call_tool` on slife-mcp wrapper |
| Tool prefix | `memory__tool`, `wechat__tool` | `server_name__tool` |
| Lifecycle | Dedicated `start_*()` / `stop_*()` in `AgentService` | Managed by `ConnectionPool`, auto-reconnect on restart |
| Use case | Slife-native services (memory, WeChat) | Third-party tools (filesystem, search, APIs) |

Both use the same MCP protocol and the same `MCPProxyTool` adapter.
The distinction is operational — built-in plugins get dedicated lifecycle
management with direct tool routing; external servers are dynamically managed
through the slife-mcp proxy with auto-persistence to the config file.

#### Tool Routing (MCPProxyTool.execute)

The `MCPProxyTool.execute()` method (`slife/mcp/tool_adapter.py:103-148`) dispatches
tool calls through one of three paths based on the tool's server origin:

| Server | Routing path | MCP client used |
|---|---|---|
| `mcp` (built-in gateway) | Direct call on the wrapper client; side-effect callbacks for config persistence | `self._mcp_client` → slife-mcp |
| `memory` / `wechat` (built-in, direct) | Direct call on the dedicated plugin client — no proxy routing | `self._mcp_client` → slife-memory or slife-wechat |
| External servers | Routed: `mcp_call_tool` → slife-mcp → `ConnectionPool.call_tool()` → external server's JSON-RPC transport | slife-mcp → `ConnectionPool` → `MCPServerConnection` |

This is the code-level realization of the architecture: built-in plugins are
peers with a direct line to Slife; external servers only speak through the
slife-mcp gateway.

**Why separate processes:**

If a plugin crashes, Slife continues. If Slife crashes, the plugin observes the disconnection and can save state. No in-process crash can race with writes to disk. Both plugins are part of the slife source tree — they share the same repo, the same test suite, and the same release cycle.

## Agent Loop

Single function-calling loop. All tools — native functions, MCP tools, memory tools, A2A tools, skills — are registered as OpenAI function definitions in one `ToolRegistry`. The LLM decides what to call and when.

```
User Input → Conversation.add_user_message()
  → loop: LLM stream → thinking/text chunks → handler callbacks
    → tool calls? → ToolRegistry.execute() → sanitize_secrets() → Conversation.add_tool_result() → loop
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
| `config_env_set` / `config_secret_register` / `get` / `remove` | Manage env vars in slife.json5 — secrets → `${VAR}` refs (no value param), non-secrets → values. `config_env_set` rejects secret-looking values (API key prefixes, token patterns). `config_env_get` handles shell + config only (no keyring). |
| `credential_check` | Verify OS keyring credentials — shows masked value (`sk-a…B3f2`). Never exposes the full secret. |
| `inject_credential` | Load a secret from keyring into `os.environ` — temporary, this process only. Secret never appears in return value. LLM-safe. |
| `uninject_credential` | Remove an env var from `os.environ`. No keyring access. LLM-safe. |
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

slife-mcp is a built-in plugin that manages persistent connections to **external** MCP servers. It runs as a child process (stdio), spawned by Slife via `MCPWrapperProcess`. Other built-in plugins (slife-memory, slife-wechat) connect directly to Slife via their own `MCPWrapperProcess` — they do **not** go through the slife-mcp proxy.

```
                         stdio
Slife ── MCPClient ────────────▶ slife-mcp (gateway)
                                      │
                                      ├── filesystem MCP (npx, stdio)
                                      ├── fetch MCP (uvx, stdio)
                                      ├── remote MCP (HTTP POST)
                                      └── ... (any MCP server)

                         stdio
Slife ── MCPClient ────────────▶ slife-memory (direct)
Slife ── MCPClient ────────────▶ slife-wechat (direct)
```

**Architecture rationale:** MCP servers are subprocesses. If managed in-process, a Slife crash would orphan them. A separate gateway process (slife-mcp) means external MCP servers stay alive and can be shared across Slife instances. Built-in services (memory, wechat) each get their own process with a direct MCP connection — no proxy routing overhead.

### Slife side (`slife/mcp/`)

- **MCPClient** (`client.py`): connects via stdio (child process). Uses `asyncio.Queue` adapters to bridge subprocess pipes to MCP's `ClientSession`.
- **MCPProxyTool** (`tool_adapter.py`): adapts external MCP tools to Slife's `Tool` ABC. Sets `name`/`description`/`parameters` at instance level. Tool names are prefixed with the server name.
- **MCPWrapperProcess** (`process.py`): generic child process lifecycle management — start, create client from streams, graceful stop (stdin close → SIGTERM → SIGKILL escalation). Used identically for all three built-in plugins (slife-mcp, slife-memory, slife-wechat).

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

### Post-Connect Setup

After a successful connection, ``MCPServerConnection._post_connect_setup()`` runs
server-specific initialization (best-effort — failures are logged but never block
the connection).

**fetch MCP server — Node.js detection on Windows.**  The ``mcp-server-fetch``
package uses ``readabilipy`` for article extraction.  ``readabilipy`` detects
Node.js by running ``subprocess.run(['node', '-v'])`` — which works because
``node.exe`` is found via Windows ``CreateProcess``'s ``.exe`` extension search.
But ``readabilipy`` also needs to install its JavaScript dependencies via
``subprocess.run(['npm', 'version'])``, which **fails on Windows** because
``CreateProcess`` only appends ``.exe`` when searching for executables, not
``.cmd`` (and ``npm`` is only available as ``npm.cmd``).  See
`python/cpython#94541 <https://github.com/python/cpython/issues/94541>`_.

**Workaround.**  The post-connect hook pre-installs ``node_modules`` directly
into the ``readabilipy/javascript`` directory via ``cmd /c npm install``.
With ``node_modules`` already present, ``have_node()`` succeeds without ever
calling ``have_npm()``, completely sidestepping the detection bug.  This is
done once per slife session at MCP server connection time — the uvx cache
keeps the installed packages across restarts.

### External Dependencies

Slife depends on several external tools at runtime.  The approach is:

| Where | What | How |
|-------|------|-----|
| **Install script** (`install.ps1` / `install.sh`) | Python, uv, Node.js | Auto-detects and installs missing dependencies |
| **Runtime startup** (`_check_external_deps` in `__init__.py`) | node, npm, uv | Checks availability, reports via ``system_health`` — no auto-install |
| **Post-connect hook** (`connection.py`) | readabilipy node_modules | Pre-installs to work around Windows PATHEXT limitation |

The division of responsibility:

- **Install script** handles the "not installed" case — it's the one-click
  path for production users.  Developers manage their own toolchain.
- **Runtime check** surfaces missing tools to the LLM via ``system_health``
  so the agent can guide the user.  It never attempts to install — that
  belongs in the install script or the user's package manager.
- **Post-connect hook** handles the "installed but not detected" edge case
  (Windows ``CreateProcess`` not finding ``.cmd`` files).  This is a
  platform-specific workaround that the install script cannot fix.

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
                    ~/.slife/<agent_id>.db
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
    author         TEXT,     -- who (--agent flag)
    user_message   TEXT,     -- what the user said
    messages       TEXT,     -- assistant response JSON (thinking, tool calls, results, text)

    summary        TEXT,     -- 1-2 sentence gist (LLM-written via memory_summarize)
    tags           TEXT,     -- comma-separated topic tags

    channel        TEXT,     -- source: 'human', 'wechat', or remote agent id
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
| Source channel (human/wechat/agent) | ✅ | ❌ (UI prefix on restore) |
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

**Windows: llama-cpp-python** cannot be built from source (no C++ compiler).
Install a pre-built wheel instead:

```bash
uv add "llama-cpp-python @ https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.34-vulkan/llama_cpp_python-0.3.34-py3-none-win_amd64.whl"
```

Available backends: `v0.3.34-vulkan` (any GPU, falls back to CPU), `v0.3.34-cu132` / `cu125` (NVIDIA CUDA), `v0.3.34-hip-radeon` (AMD).  Vulkan is the safest default.
Download GGUF models from [Hugging Face](https://huggingface.co/ChristianAzinn/bge-m3-gguf) — Q4_K_M quantized (~300 MiB) gives near-full accuracy.

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

**Restore fidelity:** The UI rebuild recreates user messages, assistant responses
(thinking + text), and tool call widgets from stored OpenAI-format messages.
However, transient UI state — notably the per-tool-call iteration counter
(e.g. ``(3/10)`` shown during live execution) — is not stored in the diary and
is therefore absent from restored widgets.  The iteration counter is derived
from the agent loop's internal state during live runs; restored tool calls are
rendered as completed with their results but without iteration numbering.

### Agent Isolation

Multiple agents on the same machine are isolated by `--agent`:

```bash
Slife --agent alice              # alice's diary, alice's knowledge
Slife --agent bob                # bob's diary + A2A identity "bob"
```

`--agent` serves both purposes:
- Memory isolation key (who owns the diary)
- A2A network identity (who I am on the MQTT mesh)

Every memory tool takes an `author` parameter. The `diary` table uses `author` as the primary isolation column. `diary_semantic` (vec0) uses `author` as a partition key — KNN search is automatically scoped to one agent with zero cross-agent overhead.

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

All messages — human keyboard input, MQTT tasks, subagent results, WeChat messages — flow through a single `asyncio.Queue`:

```
Human keyboard ──→ Inbox.post() ──→ asyncio.Queue ──→ Inbox.run() ──→ AgentLoop
MQTT inbox msgs ──→ Inbox.post() ──→              ──→ ConversationStore
WeChat messages  ──→ Inbox.post() ──→              ──→ per-source convs
Subagent results ──→ Inbox.post() ──→
```

**ConversationStore**: human (TUI) and WeChat conversations are persistent across messages (continuous back-and-forth). Remote agent conversations are fresh each time (one-shot task model).

**Serialization**: the inbox processes messages sequentially — even if human, WeChat, and remote agents send simultaneously, only one `AgentLoop` runs at a time. While a loop is running, the status bar shows "⏳ processing."

**Queue guarantees**:
- **No interruption**: `Inbox.post()` is non-blocking. Messages are always enqueued and waited — an incoming WeChat message never interrupts a running agent loop. The current loop finishes, then the next queued message is processed.
- **No message loss**: the inbox runs as a persistent background task (`asyncio.create_task(inbox.run())`) that lives for the entire session. It starts before any input channel (Step 0 in `on_mount`) and is the last thing shut down. Every channel — keyboard, WeChat, MQTT — drops messages into the same queue with the same guarantee.
- **No cancellations**: the TUI input handler uses `run_worker(exclusive=False)` so human messages don't cancel the current loop. They simply wait their turn.

**Message handler resolution**:
1. If the message carries its own `handler` (TUI keyboard path), use it directly.
2. Otherwise, look up `handler_for(source)` → registered handlers → default factory.
3. The default factory creates a fresh `TUIHandler` per message, so WeChat and remote A2A messages stream to the chat view just like locally-typed messages.

**Reply routing**: each message can carry an `on_reply` callback. After the agent loop completes, the response text is passed to this callback — WeChat uses it to forward replies back to the phone, A2A uses it to publish task results to MQTT.

### Remote Task & WeChat UI Integration

Remote tasks and WeChat messages stream to the chat view exactly like locally-typed messages. The source agent's name or channel prefix (`Wechat>`) identifies the origin. The LLM's thinking and response stream to the chat, and tool calls render as collapsible widgets. This is achieved through a handler factory pattern that creates fresh `TUIHandler` instances per message.

Activity callbacks and the handler factory are registered at startup (Step 3 in `on_mount`, before any channel starts polling) and are always active — not gated behind A2A. This ensures WeChat display works regardless of whether A2A is enabled, and no messages are dropped before the UI is listening.

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

## Credential & Configuration Architecture

Slife separates secrets from config into two layers with different security properties:

```
┌─────────────────────────────────────────────────────┐
│  OS Keyring (credstore)                             │
│  Encrypted at OS level.  Survives config changes.   │
│  ─────────────────────────────────────────────────  │
│  credstore set <KEY>          ← masked stdin input  │
│  credential_check <KEY>       ← masked value        │
│  (interactive-only CLI — LLMs cannot invoke)        │
└────────────────────┬────────────────────────────────┘
                     │  ${VAR} reference
                     ▼
┌─────────────────────────────────────────────────────┐
│  slife.json5 → env: section                         │
│  Plain config file.  Holds refs, not secrets.       │
│  ─────────────────────────────────────────────────  │
│  config_secret_register <KEY>  ← secrets only       │
│  config_env_set <KEY> [value]  ← non-secrets only   │
│  config_env_get [key]          ← non-secrets only   │
│  config_env_remove <KEY>       ← config only        │
└─────────────────────────────────────────────────────┘
```

### Design Principles

**Secrets never reach the LLM context.** The `credstore set <KEY>` CLI reads secrets via masked stdin input (no echo, no shell history) and writes them directly to the OS keyring. **credstore is an interactive-only CLI tool — LLMs cannot invoke it** (it requires direct TTY input). At runtime, secrets from the keyring may be loaded into ``os.environ`` for subprocess compatibility (MCP servers, embeddings), but ``sanitize_secrets()`` redacts any secret patterns in tool output before they reach the LLM context.

**Plaintext API keys are rejected at config load time.**  ``api_key`` fields in
``slife.json5`` must use ``${VAR}`` references or ``keyring:`` URIs — literal key
values cause a ``ValueError`` at startup.  This prevents accidental key leakage
through version control or config sharing.

**``config_env_set`` guards against secret leakage.**  The tool rejects values
that match known API key patterns (``sk-*``, ``ghp_*``, ``ya29.*``, high-entropy
blobs) or key names containing KEY/SECRET/TOKEN/AUTH.  This is belt-and-suspenders
on top of the structural split — even if the LLM ignores the tool description,
the value is blocked before it touches disk.

**Harness-level tool output sanitization.**  ``sanitize_secrets()`` in
``logfmt.py`` is applied to **every tool result** at ``AgentLoop._execute_tools()``
— the single chokepoint between tool execution and the LLM context window.
It redacts API key patterns (``sk-*``, ``ghp_*``, ``ya29.*``, Bearer tokens,
``key=value`` patterns, 32+ char hex/base64 tokens) with ``<REDACTED>`` before
the result reaches the conversation, the TUI display, or the LLM.  This is the
last-resort guard — even if a tool echoes a secret from the environment or reads
a ``.env`` file, the key never enters the LLM context.  The function is
idempotent and passes normal text through unchanged.

**Clean separation of config vs. credentials.** `config_env_get` handles non-secret env vars (shell → slife.json5). Secret values detected in the shell environment are automatically masked (``sk-a…B3f2``). It does NOT query the keyring. `credential_check` handles all secrets (shell → keyring) and shows masked values (e.g. `sk-a…B3f2`). The LLM chooses the right tool — no key-name heuristic needed.

**Two tools for registration — structurally safe.** Secret and non-secret env var registration are separate tools, eliminating the need for runtime key-name detection:

| Tool | Purpose | `value` parameter | Behavior |
|------|---------|:---:|---|
| `config_secret_register` | Secrets (API keys, tokens) | ❌ | Writes `${VAR}` placeholder, directs user to run `credstore set <KEY>` in terminal. Always checks if already stored via `credential_check`. |
| `config_env_set` | Non-secret vars (EDITOR, LANG, etc.) | ✅ optional | Writes value directly, or `<YOUR_VAR>` placeholder if omitted. **Rejects values that look like secrets** (API key prefixes, token patterns, high-entropy strings) — use `config_secret_register` for those. |

The split is structural, not heuristic — `config_secret_register` has **no `value` parameter**, so the secret can never enter the LLM context regardless of model behavior.

**Resolution at runtime.** `config_env_get` resolves non-secret env vars: shell → slife.json5. `credential_check` resolves secrets: shell → OS keyring, with values masked (`sk-a…B3f2`). The two tools are separate — the LLM picks the right one.

**Config removal is scoped.** `config_env_remove` removes only from `slife.json5` — it never touches the OS keyring or shell environment. Credentials stored in the keyring by other applications or by the user directly are never affected by Slife's config management.

**No agent-side deletion.** There is no `credential_delete` tool exposed to the agent. Deleting secrets from the OS keyring is a privileged operation that belongs in the terminal, not in an agent conversation.

### Why Two Layers

| | OS Keyring | slife.json5 env: |
|---|---|---|
| **What lives here** | Actual secret values | References (`${VAR}`) and non-secret config |
| **Encryption** | OS-level (Keychain/Linux keyring/Win DPAPI) | Plaintext file |
| **Who writes** | User via `credstore set` CLI | Agent via `config_secret_register` / `config_env_set` |
| **Who reads** | `credential_check` (masked value from keyring) | `config_env_get` (shell + config only, no keyring) |
| **Survives** | OS user profile changes | Git version control |

Separating them means you can commit `slife.json5` to version control (with `${VAR}` references) without leaking secrets, while secrets stay in OS-level encrypted storage where they belong.

## Config Loading

`Config.from_json5()` (`slife/config.py`) parses the JSON5 file in structured phases:

1. **Models**: dispatches between provider-dict and flat-list formats. Provider defaults (api_key, base_url, api) are inherited by each model. Duplicate model IDs within a provider raise an error.
2. **Env**: extracted and injected into `os.environ` so tools and subprocesses can reference values via `${VAR}`.
3. **Agent**: `max_iterations`, `context_floor`, `context_ceiling`, `tool_result_ceiling`.
4. **MCP**: built-in plugin — always enabled. External servers configured under `mcp.servers`; each can set `enabled: false` to skip auto-connect.
5. **Memory**: built-in plugin — always enabled. Embedding backend auto-detected — local GGUF takes priority over API; if neither is configured, semantic search degrades gracefully.
6. **A2A**: auto-detects Mosquitto at startup. The `mqtt` config section provides broker connection details. `paho-mqtt` is a core dependency — A2A tools are always registered and return helpful errors when the broker is unavailable.
7. **Subagent**: always available, configured with `max_subagents` and `task_timeout`.
8. **Tools**: optional override list — auto-discovery handles defaults. A2A tools (`requires_a2a = True`) are registered when `a2a_config` exists in config, regardless of broker connectivity — each tool handles "not connected" gracefully.
9. **System Health**: `system_health` tool reports live OS info, available shells (bash, cmd, PowerShell), workspace status (dev/prod, uv/pip, readable/writable, git), embedding backend status, MCP server connections, and startup errors — all from a single call.

`${ENV_VAR}` and `${ENV_VAR:-default}` resolution works recursively through dicts and lists. The common `${VAR}` → `os.environ` → credstore lookup chain is consolidated in `_resolve_env_or_credstore()`, shared by `_resolve_api_key()` and `_resolve_mcp_env_var()`.

## Project Structure

```
slife/
  __init__.py           # Entry point: main(), config loading, _check_external_deps()
  config.py             # JSON5 config loading — ModelConfig, MCPConfig, MemoryConfig, etc.
                        #   _resolve_env_or_credstore(): shared ${VAR} → os.environ → credstore
  env.py                # ${ENV_VAR} and ${ENV_VAR:-default} resolution
  platform.py           # OS detection, shell syntax (Windows/Unix), desktop notifications
  logfmt.py             # Structured logging (SessionFormatter, request/session IDs)
                        #   + resolve_log_dir(): shared log-directory resolution
                        #   + ok_json() / error_json(): JSON response envelope helpers
  bootstrap.py          # Main-process logging setup (uses resolve_log_dir from logfmt)
  server_utils.py       # Server-process logging setup + shutdown (uses resolve_log_dir from logfmt)

  agent/                # LLM interaction layer
    loop.py             #   Function-calling while-loop with streaming
    llm_client.py       #   OpenAI-compatible streaming client (+ thinking)
    conversation.py     #   Message history + context window trimming
    service.py          #   Wiring: client + tools + loop + MCP + Memory + A2A + WeChat
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
    config_env.py       #   config_env_set / config_secret_register / get / remove
    credentials.py      #   credential_check — masked keyring lookup
    cli.py              #   cli_add_tool / check_installed / remove / list
    _config_io.py       #   Shared JSON5 read/write helpers + _ConfigPathMixin

  mcp/                  # MCP client + plugin infrastructure
    client.py           #   stdio client with asyncio.Queue adapters (_ReadAdapter, _WriteAdapter)
    tool_adapter.py     #   MCP → Slife Tool adapter (MCPProxyTool)
    process.py          #   Child process lifecycle manager (MCPWrapperProcess)

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
    app.py              #   Main app (SlifeApp) — startup orchestration, session restore
    chat.py             #   Message widgets (ChatView, AssistantMessage, UserMessage)
    handler.py          #   Streaming event → UI bridge (TUIHandler)
    tool_display.py     #   Tool call rendering (ToolCallWidget)

skills/                 # On-demand skill plugins (SKILL.md per directory)
tests/                  # pytest suite (asyncio_mode=strict, 1250+ tests)
```

### credstore — Credential Storage Companion

```
credstore/
  __init__.py           # Public API: get/set/delete credential, format_export/unset, etc.
  __main__.py           # CLI — 10 commands (set-password, status, set, get, delete,
                        #   list, inject, uninject, reset-keyring, reset-backup)
  _backend.py           # Dual-write backend: system keyring + keyrings.cryptfile
                        #   unlocked_cryptfile(password): context manager
  _config.py            # Config file resolution (credstore.json5, CREDSTORE_FILE env var)
  _enumerate.py         # Platform-specific credential enumeration (Windows CredMan)
  _resolver.py          # keyring: URI parsing and resolution
  _shell.py             # Shell formatting + profile persistence helpers
  _store.py             # CredentialStore: get/set/delete/reset/list_keys
  _tty.py               # Cross-platform masked terminal input
  tests/                # pytest suite (206 tests)
```

## The Knowledge Base Effect

The memory system IS a knowledge base. **Everything the agent encounters** — file contents, web search results, API responses, command output, errors, thinking, decisions — is permanently stored in `diary.messages` and indexed by FTS5 + vec0. Over time, this becomes a searchable archive of everything you and the agent have done.

No separate knowledge base, no external indexing pipeline. The conversation IS the knowledge base — every observation, reasoning trace, and decision is recorded in its original context and searchable through a single interface. Vector search is provided by the sqlite-vec extension inside the same SQLite database, not a separate service.

The LLM can recall its own past experience:

- `memory_search(mode="grep", query="ConnectionError")` — find every past occurrence of a specific error
- `memory_search(mode="fts5", query="MCP connection issue")` — find past discussions about a topic
- `memory_search(mode="hybrid", query="that memory leak we fixed")` — find the conversation where a bug was fixed
- `memory_search(mode="time", since="2026-07-14")` — browse everything from a specific date
