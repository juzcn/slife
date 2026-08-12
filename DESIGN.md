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
- **Not a safety system** — no sandboxing beyond the OS. Approval is model-driven: the LLM sets `_approve: true` on any tool call to push a confirmation dialog, but nothing hardcodes approval on any tool
- **Not an automation engine** — no scheduled tasks, background workers, or event triggers

It's a chat window with tools. The LLM is in full control.

### Language policy

The model input should read uniformly, so text that Slife authors is English:

- **System prompt** (`system_prompt.j2`, `context_status.j2`): English.
- **Native tool schemas** — tool `name`, `description`, parameter docs, and result strings: English.
- **Plugin tool schemas and result strings**: English (same policy as native tools — they are model-visible).
- **External tools** (MCP servers, skills, third-party commands): keep the language of the external source — do not translate. They are opaque and pass through as-is.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  UI (Textual TUI)                                                    │
│  slife/ui/app.py, chat.py, handler.py, tool_display.py,              │
│  image_utils.py, restore.py, approval_prompt.py                      │
├──────────────────────────────────────────────────────────────────────┤
│  Agent Service                                                       │
│  slife/agent/service.py — wires client + tools + loop + plugins      │
│  Manages MCP, MemDB, A2A/MQTT, WeChat, MemFiles, subagent lifecycles │
│  Unified inbox serializes human + WeChat + MQTT + subagent messages  │
├──────────────────────────────────────────────────────────────────────┤
│  Agent Loop                              │  MCP Client                │
│  Streaming function-calling              │  Streamable HTTP transport │
│  Context trim (_sys_trim) + status       │  OAuth device-code flow    │
│  (_sys_note); concurrent tool execution  │  Tool proxy + adapter      │
│  Reasoning (thinking) support            │                            │
├──────────────────────────────────────────┴───────────────────────────┤
│  Tool Registry — unified OpenAI function definitions for all tools   │
│  Native · MemDB · MCP Proxy · Skills · CLI · REST API · A2A          │
├──────────────────────────────────────────────────────────────────────┤
│  Plugins (independent child processes, Streamable HTTP)              │
│  slife-mcp (gateway) · slife-memdb (diary) · slife-wechat            │
│  slife-a2a (A2A over MQTT) · slife-memfiles (file sharing: /mcp + /share)                         │
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
    → auto-invoke _sys_note (context status)        (usage computed once)
    → usage ≥ ceiling? → auto-invoke _sys_trim      (trim to floor)
    → LLM stream → thinking/text/tool deltas → handler callbacks
    → tool calls? → ToolRegistry.execute() concurrently (asyncio.gather)
                    → sanitize_secrets() on each result → truncate → loop
    → `_approve: true` on a call? → serialized ApprovalPrompt before execution
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
- **Turn consistency**: one function — `Conversation._ensure_turn_consistent()` — enforces two idempotent invariants before a conversation is persisted (and again on load), so it is always well-formed when it next reaches the wire:
  1. **No orphaned tool_calls** — an assistant `tool_call` whose result never arrived (an interrupted turn, e.g. a hung tool) gets a synthetic `Error: request cancelled by user` result inserted right after it; otherwise the orphan is persisted and re-repaired on every restore.
  2. **Alternating roles** — a conversation ending on a `user`/`tool` message (a tool result is a `user` role on the Anthropic wire, which rejects two consecutive users with a 400) gets a closing assistant message.

  It has exactly **two call sites**: `save_to_memory` (before persisting — the save-side guarantee, which runs unconditionally after every turn via the inbox `finally`), and `restore_session` (after loading from memory — the load-side guarantee). Because every turn is saved unconditionally, the conversation is always left consistent before the next user message is appended. Each turn also opens with an auto-invoked `_sys_note` assistant+tool pair, so a user message is always sandwiched between assistant messages.
- **Context tracking**: `AgentLoop.context_tokens_for()` is the single source for the current context size (actual `prompt_tokens` from the last API call, else the restore-time estimate before the first call, else the chars÷3 live estimate). It drives `_sys_note`, the trim gate, and the TUI status bar — one value, no recompute

### Context Window Management

Active conversation stays within `context_floor`–`context_ceiling` (default 20%–80% of `context_window`):

```
                context_window
┌──────────────────────────────────────────────────────────────┐
│   trimmed (in diary —        │  current context  │  headroom  │
│   recall via memory_search)  │  floor ~ ceiling  │  1-ceiling │
└──────────────────────────────────────────────────────────────┘
```

- **Detect**: context usage is computed **once per turn** via `context_tokens_for()` — the last API call's actual prompt tokens after the first round, else the restore-time estimate primed on `_last_usage` (computed when the UI rebuilds the session to decide how many turns to restore), else the chars÷3 live estimate; `_sys_note` reports it as the usage %
- **Trim**: when the reported usage % hits the configured `context_ceiling` (default 80%), the loop auto-invokes **`_sys_trim`** — which is the trim itself. It removes the oldest complete turns down to `context_window × context_floor` (default 20%) and returns the notification. The gate lives outside the tool so `_sys_trim` is only invoked — and only records a pair — when a trim actually happens; if the LLM calls it directly it genuinely compacts the context (a legitimate action, not a no-op)
- **Status**: once per turn the loop auto-invokes **`_sys_note`** (a normal tool-call pair) with the same `current` value the trim gate uses — it renders `context_status.j2`: current time, context usage %, token usage, context time range, change notifications (model/CWD/shell/modalities), and any A2A peer presence events since the last turn (online/offline/timeout, drained read-once)
- **Restore**: on startup, recent turns are loaded directly from SQLite within the `context_floor` token budget
- **Tool result cap**: a single tool result is truncated at `tool_result_ceiling × context_window × 3` characters (default 20% of the window; ~3 chars/token heuristic)

### Harness Tools — Visibility Tiers

Harness tools are internal machinery the loop invokes on the agent's behalf. Two prefixes encode the visibility tier:

1. **`_` (single underscore) = harness, LLM-visible but reserved.** The native `_sys_note` / `_sys_trim` (in `slife/tools/harness.py`) **do** appear in the schema — required so the Anthropic / OpenAI-Responses backends accept their tool-call pairs in history. `AgentLoop._auto_invoke()` calls them as normal tool-call pairs; the system prompt forbids the LLM from calling them. `_sys_note` is pure (only reads state); `_sys_trim` genuinely trims to the floor — a legitimate action if the LLM calls it anyway.
2. **`__` (double underscore) = harness, LLM-invisible.** Plugin harness tools (`__memory_save_turn`, `__a2a_drain_incoming`, `__wechat_drain_incoming`, `__mcp_call_tool`, …) are filtered out of the schema before registration — they never reach `to_openai_functions()`. They are called programmatically via `client.call_tool("__…")`.

| Tool | Shape | Visibility |
|------|-------|------------|
| `_sys_note` | Native tool, auto-invoked each turn | `_` — visible-but-forbidden |
| `_sys_trim` | Native tool, auto-invoked on trim | `_` — visible-but-forbidden |
| `__memory_save_turn` / `__memory_get_recent_turns` | memdb plugin | `__` — invisible |
| `__wechat_drain_incoming` / `__wechat_dispatch_reply` | wechat plugin | `__` — invisible |
| `__a2a_drain_incoming` / `__a2a_dispatch_result` | a2a plugin | `__` — invisible |
| `__mcp_connection_status` / `__mcp_call_tool` | mcp plugin | `__` — invisible |
| `__tunnel_status` / `__register_file` | memfiles plugin | `__` — invisible |

The three registration paths share one `__` predicate (`agent/plugins.py`) — no harness tool leaks into the schema.

### System Prompt

The prompt is a **runtime spec sheet** — facts the LLM cannot discover from training data or tool schemas. Two-part design:

- **Static** — `slife/agent/templates/system_prompt.j2`, rendered once at startup: model identity, context policy (floor/ceiling/tool-result %), host platform (OS, arch, shell, python), workspace paths (data/config/logs/db/images/skills), credstore backend name, MCP tool naming prefix, and A2A broker info when configured. Never changes → maximal prompt cache hit rate.
- **Dynamic** — `slife/agent/templates/context_status.j2`, rendered by the `_sys_note` tool (auto-invoked once per turn): current time + UTC offset and context usage % always; context time range when set; model/CWD/shell/modalities only when changed; pending A2A peer presence events since the last turn (the same lines the TUI shows, drained once).

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

**Prompt caching (Anthropic system blocks):** `AnthropicBackend._oa_msgs_to_anthropic` emits each OpenAI `system` message as an Anthropic system content block and tags the **last** one with `cache_control: {type: "ephemeral"}` — the static base prompt becomes the cache breakpoint, so only the dynamic `_sys_note` status (a message-stream tool pair, never a second `system` message) changes per turn. Guarded by `_use_system_cache_control()`: on by default for `api.anthropic.com`, off for Anthropic-compatible providers (Bailian/Qwen) that may reject the field, overridable per model via `compat.cacheControl`.

**History validation (H3, resolved):** Anthropic (and OpenAI-Responses) reject tool calls in history whose names aren't in the declared `tools` list. `_sys_note` / `_sys_trim` are therefore **declared native tools** (schema-present, auto-invoked by `AgentLoop._auto_invoke()`), not conversation-layer fabrications — so their pairs validate. The system prompt forbids the LLM from calling them (see §6 of `system_prompt.j2`), and both are side-effect free if it does. DeepSeek (Chat Completions) doesn't validate and is unaffected.

**History wire shape (W2, resolved):** `OpenAIResponsesBackend._oa_msgs_to_responses` emits the Responses API's native `function_call` / `function_call_output` items for tool history — not the Chat-Completions `role:"tool"` / `tool_calls` shape. Multi-turn tool conversations are accepted by the Responses API (unit-tested; not yet exercised against a live endpoint).

### Model Management

Runtime model management via native tools — no config editing needed:

| Tool | Description |
|------|-------------|
| `model_list` | All configured models grouped by provider (active marked) |
| `model_set` | Add/update a model (creates provider if new) |
| `model_remove` | Remove by ref; auto-switches if it was active |
| `model_switch` | Switch active model by ref — persists to config and rebuilds the client live |

Model switches fire callbacks that rebuild the LLM client, update loop parameters (vision, context window, modalities), and re-render the system prompt.

## Tool System

### Tool ABC

`Tool` (`slife/tools/base.py`) defines `name`, `description`, `parameters` (JSON Schema), `category`, and `async execute(**kwargs) -> str`. Required fields are validated at class-definition time via `__init_subclass__`. `from_config(cfg, config, ctx)` allows per-tool construction from the `tools:` overrides in `slife.json5` (e.g. `execute_shell` reads its default timeout there); `ctx` carries runtime references (registry, config, MCP client, conversation) as `self._ctx`.

Categories in use (14): `System`, `Execution`, `Skills`, `CLI`, `REST API`, `A2A`, `Subagent`, `Config`, `Models`, `Credentials`, `Vision`, `Display`, `Harness`, `Meta`. The docstring in `base.py` lists only a subset — treat it as illustrative, not enforced.

### Auto-Discovery

`slife/tools/factory.py` uses `pkgutil.iter_modules` to import every module in `slife.tools.*` (skipping `base`/`factory`), then walks `Tool.__subclasses__()` recursively. A new `.py` file is automatically picked up. Filtering applies `enabled: false` overrides, skips vision tools when the active model can't see images, and skips `_skip_auto_register` classes (e.g. `MCPProxyTool`, created per-instance at runtime).

### Tool Categories — 52 native tools (up to 50 LLM-visible + 2 harness `_` tools)

`include_image` is dropped when the active model has no vision; `install_python_package` is disabled by default in the shipped config.

All tools unified under `Tool`, registered in a single `ToolRegistry`. The LLM sees only function names and schemas.

| Category | File | Tools |
|----------|------|-------|
| System | `system.py` | `system_health`, `check_memdb`, `check_wechat`, `check_memfiles`, `check_mcp`, `check_a2a`, `check_watchdog` |
| Display | `display.py` | `show_image`, `notify_user` |
| Execution | `exec.py` | `execute_shell`, `run_python_script`, `install_python_package` |
| Skills | `skill.py` | `skill_list`, `skill_use`, `skill_set`, `skill_remove`, `skill_set_enabled` |
| CLI | `cli.py` | `cli_list`, `cli_set`, `cli_remove`, `cli_set_enabled` |
| REST API | `rest_api.py` | `rest_api_list`, `rest_api_set`, `rest_api_remove`, `rest_api_set_enabled` |
| A2A | `a2a` plugin | one uniform `a2a_` prefix (hosted in the plugin, mesh-only) — `a2a_send_task`, `a2a_send_task_async`, `a2a_get_task_result`, `a2a_cancel_task`, `a2a_subscribe_task`, `a2a_list_agents`, `a2a_list_tasks`, `a2a_agent_card`, `a2a_broadcast` |
| Subagent | `subagent.py` | `spawn_subagent`, `list_subagents`, `stop_subagent`, `subagent_send_task`, `subagent_send_task_async`, `subagent_get_task_result`, `subagent_list_tasks`, `subagent_cancel_task` (no prefix; local workers, not A2A) |
| Config | `config.py` | `config_env_set`, `config_env_get`, `config_env_remove`, `native_tool_set` |
| Models | `models.py` | `model_list`, `model_set`, `model_remove`, `model_switch` |
| Credentials | `credentials.py` | `credential_check`, `credential_inject`, `credential_uninject` |
| Vision | `vision.py` | `include_image` (native — injects image blocks into the conversation; gated on a vision-capable model) |
| Harness | `harness.py` | `_sys_note`, `_sys_trim` (visible-but-reserved, see above) |
| Meta | `meta.py` | `list_tools`, `check_async`, `cancel_async`, `clear_context` |

Plus **plugin tools** — registered at runtime as `{server}__{tool}` proxies via `create_proxy_tools`:

| Server | LLM-visible tools |
|--------|-------------------|
| `mcp` | `mcp_set`, `mcp_set_enabled`, `mcp_remove`, `mcp_list`, `mcp_list_tools` |
| `memdb` | `memdb__memory_list_recent`, `memdb__memory_search`, `memdb__memory_open`, `memdb__memory_summarize`, `memdb__memory_count`, `memdb__memory_check_embedding`, `memdb__memory_set_embedding`, `memdb__memory_set_enabled` |
| `wechat` | `wechat_login`, `wechat_send_message`, `wechat_send_typing`, `wechat_check_messages`, `wechat_check_status`, `wechat_logout` |
| `memfiles` | `memfiles__expose_file`, `memfiles__save_content_or_files` |

Naming rule: a plugin tool already carrying its server as a name prefix (`mcp_set`, `wechat_login`) is registered as-is (avoids the redundant `mcp__mcp_set` / `wechat__wechat_login`); otherwise the proxy adds `{server}__`. External MCP servers always appear as `{server}__{tool}` (e.g. `filesystem__read_file`).

**Managed categories** (Skills / CLI / REST API / Models / MCP) support a standard **`X_list` / `X_set` / `X_remove`** surface (plus `X_set_enabled` where an enable/disable toggle applies). `X_set` is an idempotent upsert — add + update in one call. Config uses the `config_env_*` prefix (no `config_list`); Models substitutes `model_switch` for `X_set_enabled`.

### Registry

`ToolRegistry` is a name-keyed dict with `register` / `unregister` / `unregister_by_prefix` / `get` / `list_tools` / `to_openai_functions` / `execute`. A module-level singleton (`get_registry()`) lets meta-tools introspect without circular imports. Dynamic tools — plugin tools, MCP wrapper tools, and external MCP server tools — are registered at runtime as `MCPProxyTool` instances named `"{server}__{tool}"`. Harness-only plugin tools (prefixed `__`) are filtered out before registration.

### Timeout Architecture

Single enforcement point at the Agent Loop level. `_inject_meta_params()` adds `_timeout` (number), `_async` (boolean), and `_approve` (boolean) to **every** function definition sent to the LLM:

- Tools **without** a native `timeout` parameter → `asyncio.wait_for(timeout=…)`
- Tools **with** a native `timeout` (`execute_shell`) → mapped to the native argument, no double-wrap

The MCP client applies no timeout of its own; enforcement stays in one place.

### Approval Gate

Approval is **model-driven** (pure model judgment). The loop injects an `_approve` boolean meta-parameter on every tool schema (alongside `_timeout`/`_async`, visible on all three backends). When the LLM sets `_approve: true` on a call, the tool call is only surfaced to the UI once approved — execution pauses and an inline `ApprovalPrompt` row is mounted in the chat stream (Claude Code style, no modal: Y = approve, N / Esc = deny). Prompts serialize behind a lock. A denied call never mounts a `ToolCallWidget`; the prompt row itself carries the rejection state.

There is no hardcoded `requires_approval` flag on any tool or MCP server — the model decides per-call whether to ask the user. Headless (subagent) contexts have no handler and auto-approve.

The modal declares its own `enter → approve` / `escape → deny` bindings at `priority=True`; the App's `escape → cancel` is deliberately *not* priority so Textual's priority pass (which resolves the App before the screen) cannot steal Esc — Esc on an approval always denies (REVIEW C7).

## Plugin Architecture

Five built-in plugins run as independent child processes. Communication is via **Streamable HTTP** (MCP protocol) for all of them — the memfiles plugin additionally serves plain-HTTP file bytes on the same port via a custom route (`GET /share/{token}`), but its control surface is pure MCP.

**WSL note:** Custom env vars set via `create_subprocess_exec(env=…)` are NOT forwarded to Windows `.exe` processes through WSL interop. `WSLENV` is only read by the WSL `/init` at session start, not by child processes. Therefore, **all MCP server runtimes on WSL must be Linux-native binaries** — the install script enforces this by detecting `/mnt/*` paths and installing native versions.

### The Plugin Contract

1. Bind a free port: `bind_free_port()` pre-binds `127.0.0.1:0` and keeps the socket — no race between port discovery and server start
2. Signal the parent **once ready**: `run_plugin_server` wraps the server's lifespan and emits `signal_port(port)` (`{"port": N}` on stdout, then closes stdout) only **after** the app is ready to serve MCP — i.e. after the plugin's lifespan (if any) completes. The signal means *"ready to serve MCP on this port"*, aligning with the MCP startup handshake: the parent's first `initialize` always lands on a ready server. Plugins must **not** signal early themselves
3. Start FastMCP on Streamable HTTP with the pre-bound socket
4. Define `@mcp.tool` functions; optionally serve plain-HTTP endpoints on the same port via `@mcp.custom_route(path, methods=[...])` (e.g. memfiles `GET /share/{token}`)
5. Be importable: `python -m <module>.server`

No base class, no import hook, no SDK. Plugins are auto-discovered by scanning `slife.plugins.*` for packages with a `server.py`. Each `server.py` uses `create_plugin_server(...)` for logging + FastMCP setup and `run_plugin_server(mcp)` (or `run_plugin_server(mcp, sockets=[sock])`) for the single entry call. The parent reads the port line with a 30 s readiness budget, then connects once. Because the signal is deferred until the app is ready, slow lifespan startup (e.g. memfiles' ngrok tunnel, a2a's MQTT connect) cannot race the handshake — the parent simply waits for the signal.

The MCP client keeps bounded retry (6 attempts, 0.5 s apart, each attempt time-boxed at 10 s including transport setup) as **defense-in-depth**: a plugin that signals early (violating the contract) still loads instead of hanging.

### Watchdog (Auto-Restart)

Each plugin runs with a **watchdog** background task that monitors the child process and auto-restarts it on unexpected exit:

| Feature | Detail |
|---------|--------|
| Detection | `await subprocess.wait()` — blocks until the child exits |
| On crash | Unregisters the plugin's proxy tools (`unregister_by_prefix("{name}__")`), then restarts the process |
| Backoff | Exponential: 1 s → 2 s → 4 s → … → 30 s max |
| Max restarts | 5 consecutive failures → watchdog gives up and logs an error |
| Success reset | A successful restart resets the backoff and retry counter |
| Scope | **mcp** (respawns wrapper + reconnects external servers), **memdb**, **wechat** (restores poll loop), **memfiles**, **a2a** |

Auto-discovered third-party plugins get the same watchdog: `_spawn_plugin_generic` creates a `PluginLifecycle` for any plugin not in the built-in set, so a crash restarts it with the same backoff as the built-ins.

Subagents do **not** have their own watchdog — they connect to the main agent's plugin processes via HTTP, so a subagent crash only kills the subagent, not the shared infrastructure.

Processes communicate through environment variables:

| Variable | Purpose |
|----------|---------|
| `SLIFE_SESSION_ID` / `SLIFE_AGENT_ID` | Log correlation, agent identity |
| `SLIFE_DATA_DIR` / `SLIFE_CONFIG_DIR` | Directory overrides |
| `SLIFE_{NAME}_PORT` | Published port of each plugin (MCP / MEMDB / WECHAT / MEMFILES / MQTT) |
| `SLIFE_MEMFILES_URL` | Public ngrok URL (set inside the memfiles plugin process) |

### Built-in Plugins

| Plugin | Transport | Role |
|--------|-----------|------|
| **slife-mcp** | Streamable HTTP | Gateway for external MCP servers (stdio / SSE / Streamable HTTP). Manages connection lifecycle — spawn/connect, route tool calls, persist config. |
| **slife-memdb** | Streamable HTTP | Diary database. Hybrid search (FTS5 + vec0 vector). Turn persistence, session restore, embedding configuration. |
| **slife-wechat** | Streamable HTTP | Bidirectional WeChat messaging via iLink ClawBot. Long-poll loop for incoming messages, typing indicators, dispatch for replies. |
| **slife-memfiles** | Streamable HTTP + `/share` route | File cabinet + public sharing. MCP tools (`expose_file`, `save_content_or_files`), harness tools (`__tunnel_status`, `__register_file`), and `GET /share/{token}` for file bytes — same port, two protocols. Plugin owns the ngrok tunnel and in-process token registry. |
| **slife-a2a** | Streamable HTTP | A2A mesh over the MQTT binding (paho-mqtt v5, LWT). Only starts when the broker is reachable (TCP probe). Hosts the LLM-visible `a2a_*` tools; only the drain/dispatch harness tools (`__a2a_*`) stay `__`-prefixed. |

### slife-mcp — External MCP Gateway

Three wire transports, one raw JSON-RPC connection class (`MCPServerConnection` — deliberately no `ClientSession`/anyio TaskGroups to avoid event-loop conflicts with FastMCP):

| Transport | Mechanism | Use |
|-----------|-----------|-----|
| **stdio** | Spawn subprocess, JSON-RPC over pipes | Local MCP servers (npx/uvx/bunx) |
| **http (SSE)** | GET with `Accept: text/event-stream`, POST to message endpoint | Remote SSE endpoints (tried first for URLs) |
| **http (streamable)** | POST JSON-RPC + `mcp-session-id` header; single-JSON **or SSE-streamed** responses | Remote Streamable HTTP endpoints (fallback) |

For `url`-configured servers the gateway probes with `GET + Accept: text/event-stream`: a `text/event-stream` reply switches to **SSE** mode (the `endpoint` event yields the POST message URL); otherwise the same client falls through to **Streamable HTTP**. A Streamable response may be a single JSON body or an SSE stream — both are parsed (the first matching JSON-RPC message; later events are server-initiated notifications and are dropped). (REVIEW C9.)

Exposed management tools (LLM-visible as `mcp_set`, `mcp_set_enabled`, `mcp_remove`, `mcp_list`, `mcp_list_tools`). Live status is reported by `check_mcp` via the harness `__mcp_connection_status`. The tool-call bridge `__mcp_call_tool` is a harness tool — LLM-invisible, invoked only by the `server__tool` proxies.

`mcp_list` is a static config view — the configured servers (name, transport, command/args or url, enabled/disabled, description), with no live state and no secrets (env/headers/auth omitted). `check_mcp` (a standalone tool, also run by `system_health`) calls the harness `__mcp_connection_status` for the raw live server state and adds health levels (ok/warning/info) with remediation hints. The separation keeps "what is configured" distinct from "what is connected", so the LLM picks the right tool.

Server lifecycle:

```
disabled ──[mcp_set_enabled(name, enabled=true)]──→ enabled (connected, tools registered)
enabled  ──[mcp_set_enabled(name, enabled=false)]─→ disabled (disconnected, tools unregistered)
enabled  ──[mcp_set(changed config)]───────────────→ restarted with new settings
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

Turns are saved **unconditionally** after every turn (cancel, error, or max-iterations) via the harness-only `__memory_save_turn` tool. The save-side invariant is enforced by the harness: a turn with an orphaned `tool_call` is repaired (`_ensure_turn_consistent`) before it reaches the plugin, so the diary never persists an incomplete pair.

### Search

Three indexes: FTS5 (BM25 keyword), sqlite-vec `vec0` (cosine KNN), B-tree on `created_at` (time range).

| Mode | Best for |
|------|----------|
| `grep` | Exact strings — error messages, file paths, code |
| `fts5` | Topic / keyword search with ranked snippets |
| `hybrid` | Semantic recall (FTS5 + vec0 → RRF merge) |
| `time` | Browse by date |

Hybrid mode uses Reciprocal Rank Fusion (RRF, k=60). Without an embedding backend, hybrid degrades to FTS5-only gracefully.

Search hardening (REVIEW M5–M7): `search_grep` escapes `%`/`_` with `ESCAPE '\'`; `memory_count` honors `since`/`until` in fts5 mode; LLM-facing search clamps `limit` to `[1, 200]`; the background reindex counts only stored embeddings so it gives up on a persistently failing embedder instead of spinning. Known remainder: the `memory_count` grep branch still emits `LIKE ?` without the `ESCAPE` clause (see REVIEW.md).

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

## A2A — Agent-to-Agent (mesh)

The A2A protocol (JSON-RPC operations `SendMessage` / `GetTask` / `CancelTask` / `SubscribeToTask`, and Message/Task/AgentCard data shapes mirroring the official a2a-python reference interface) runs over a pluggable transport **binding** — currently MQTT.  The **`a2a` plugin** owns the mesh: it hosts the LLM-facing `a2a_*` tools and the `A2AClient`.

```
  a2a_send_task / a2a_list_agents / …   (LLM tools, hosted in the a2a plugin)
         │
   a2a plugin (slife.plugins.a2a)
         │  A2AClient (official operations + data model)
   MQTT binding (paho, LWT) — the transport
```

| Binding | Backend | Status |
|---------|---------|--------|
| **MQTT** | `slife.plugins.a2a` (paho-mqtt MQTTv5 → asyncio.Queue, LWT) | Fully implemented |

Only MQTT is implemented.  A `transport` other than `"mqtt"` in the `a2a` config section disables A2A with a warning at config load instead of crashing startup (REVIEW C1).

The LLM-facing `a2a_*` tools live in the a2a plugin (one uniform prefix; the MCP proxy keeps the exact names).  Subagents are **not** part of A2A — they are local workers (see "Subagent Workers" below).

### MQTT Mesh

- Topics: `Slife/<agent_id>/presence`, `Slife/<agent_id>/tasks/inbox`, `Slife/<agent_id>/tasks/result`
- Presence heartbeat every 15 s (configurable); peers silent for 45 s are pruned. LWT publishes `{"status":"offline"}` (QoS 1) so crashes are visible
- Client id is `<agent_id>-<pid>` to allow multiple processes per agent id
- Duplicate agent detection: after subscribing, the client listens 1.5 s for an existing presence with the same id and exits with a clear error rather than splitting the identity
- Slife only **probes** the broker (TCP connect) — Mosquitto is started by the user; if the probe fails, the a2a plugin is not started (A2A disabled) and this is reported via `system_health`
- The mesh connects **eagerly** when the plugin starts (lifespan hook) so presence is announced at launch; a failed eager connect is tolerated and mesh tools attempt a lazy connect on demand
- Peer presence **transitions** (online/offline/timeout) reach the LLM context: the a2a plugin queues them; `AgentService._a2a_poll_loop` drains them and appends the TUI-identical line (via `format_presence_line`, which also filters heartbeat-driven `status_change`) to a buffer that `AgentLoop` drains read-once into the `_sys_note` footer each turn. The footer carries only *changes* — the current roster stays queryable via `a2a_list_agents`, so a missed event never leaves the LLM with stale state

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

Mesh tasks are tracked in memory (`TaskRecord`: id, agent, preview, status, transport, timings, result capped at 2000 chars; 500-record soft cap). The store is **not persisted across restarts** — `a2a_list_tasks` after restart is empty by design. Worker (subagent) tasks are **not** in this store — they live in per-worker local records (`SubagentProcess._task_records`).

### Subagent Workers

Local child-process workers, always available — no config toggle.  A subagent (agent worker) is **not** an A2A peer: no network identity, no presence, and no mesh tooling of its own — when it reaches the mesh it sends as the parent via the shared a2a plugin.

- **headless.py**: Slife without TUI, worker-scoped JSON-RPC 2.0 over stdin/stdout — methods `worker/send`, `shutdown`; notifications `worker/complete`, `worker/progress`; a `{ready: true}` result signals startup
- **SubagentManager**: spawn/stop/list lifecycle; auto-names `sub-1`, `sub-2`, …; `max_subagents` default 5, `task_timeout` default 120 s
- **Serial processing + visibility**: a worker runs one task at a time. `SubagentProcess` tracks the in-flight count (`is_busy` / `queued`); a sync `subagent_send_task` to a busy worker is automatically queued as async and reported (never a silent timeout, never a resend). `subagent_list_tasks` lists worker tasks across workers.
- **Shared plugins**: subagents connect to the main agent's plugin servers (MCP / memdb / wechat / a2a / memfiles) via inherited ports — no isolation; they can send but never drain the inbound queue (all replies and management belong to the main agent)
- **Recursion**: subagents can spawn their own descendants (each level has its own SubagentManager + watchdogs)

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

A standard Streamable HTTP plugin (`slife/plugins/memfiles/server.py`) — self-contained and replaceable exactly like memdb / mqtt.  The harness is a thin MCP client: it spawns the plugin, registers the `memfiles__*` tools, and never touches file-serving state directly.

The plugin owns everything — the in-process token registry, the ngrok tunnel, and serving the file bytes on the **same port** via a custom HTTP route (one port, two protocols: `/mcp` for Streamable HTTP, `/share/{token}` for plain HTTP):

1. `expose_file(path)` (MCP) → registers the file under a random 30-char hex token (`secrets.token_hex(15)`) → returns `https://xxx.ngrok-free.dev/share/<token>`.  Always registered — when the tunnel is offline the tool returns a graceful error rather than being hidden.
2. `GET /share/{token}` streams the file in 64 KB chunks (403 unknown token, 404 file gone).

No BLOBs, no database, no HMAC — token→path mappings are an in-process dict (server and tunnel share one process, so no shared registry file). `save_content_or_files` persists content/URL/files under `<agent>.files/` with an `index.json` and returns share URLs when the tunnel is active; when offline, files are still saved locally and the result notes "(sharing offline)". `include_image` is **not** part of this plugin — it is a native vision helper (`slife/tools/vision.py`) that injects image blocks into the main-process conversation.

`GET /share/{token}` streams the file with an RFC 5987 `Content-Disposition` — a non-ASCII filename (e.g. CJK) is emitted as an ASCII fallback in `filename=` plus the real name percent-encoded in `filename*=UTF-8''`, because HTTP header values must be Latin-1 (a raw CJK filename otherwise raises `UnicodeEncodeError` → HTTP 500).

### Ngrok Tunnel

Started **by the memfiles plugin** (eagerly in its lifespan, non-blocking; graceful failure) via the official ngrok Python SDK (embedded agent — no external binary). Authtoken resolution: credstore `NGROK_AUTHTOKEN` → environment. Uses **endpoint pooling** (`pooling_enabled=True`) so multiple slife instances (WSL + Windows, sub-agents on different machines) share the same dev domain — ngrok load-balances across all online agents. Initial start retries up to 3 times with linear backoff (2/4 s); a background monitor performs one follow-up retry if the first start failed; share tools fall back to an on-demand start.

ngrok free tier limits: **1 online agent** (one tunnel per token — only the first agent to start gets the memfiles tunnel; subsequent agents fail to bind), 1 GB transfer/month, 20k HTTP requests/month. Endpoint pooling requires no paid plan. Subagents reuse the main agent's memfiles plugin via Streamable HTTP (`SLIFE_MEMFILES_PORT`) instead of spawning a second tunnel.

## UI

Textual TUI with minimal chrome:

- **ChatView** — scrollable message container; printable keys redirect to the input
- **UserMessage** — prefix-styled user text with optional image attachments
- **AssistantMessage** — streaming text with collapsible thinking blocks (Enter/Space toggle)
- **ToolCallWidget** — collapsible amber headers: status icon, label, primary-arg preview, iteration counter; Ctrl+Y copies the result
- **StatusBar** — model name, thinking indicator, inbox state, token count
- **ApprovalPrompt** — inline approve/deny row for `_approve: true` tool calls (Y / N / Esc), re-renders to ✓ Approved / ✗ Denied
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
| MemDB | `memdb__memory_search` | `memdb__memory_open` |
| Skills | `skill_list` | `skill_use` |
| MCP | `mcp_list` / `mcp_list_tools` | `mcp_set_enabled(name, enabled=True)` |

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

`${VAR:-default}` fallbacks supported; resolution is recursive over strings/lists/dicts. Known gap: `${VAR:-default}` resolves from the shell env only and never consults credstore, so the fallback beats a credstore-held key for that form (see REVIEW.md).

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

**The input and output gates are the authoritative trust boundary.** A secret may appear as plaintext anywhere *inside* the process (tool internals, `conn.error`, log lines) — that is **not** a vulnerability by itself. Judge any finding by two questions:

1. **Does the secret reach the LLM context / conversation as plaintext?** → blocked by the gates below.
2. **Does the secret cross the machine trust boundary as plaintext?** — network egress, a file published publicly (`/share`), anything readable outside the running user. → this is a real security finding.

Everything else — including plaintext in `~/.slife/logs/` (readable only by the running user) — is hygiene, not a security issue. Do not report a plaintext string that the gates will mask, or a local log line, as a security finding.

Two gates, single pattern-masking engine (`logfmt.sanitize_secrets`):
1. **Inbound** — `Conversation.add_user_message()` on every external message
2. **Outbound** — `AgentLoop._execute_tools()` runs `sanitize_secrets` on **every** tool result before it enters the conversation (tool-call arguments are also masked at `Conversation.add_assistant_message`). So even a tool that returns a secret verbatim (e.g. a config/env lookup) never puts a known-shaped value into the LLM context.

Known API key shapes (`sk-*`, `ghp_*`, `ya29.*`, `pypi-*`), `Authorization: Bearer` tokens, and credential-named `key=value` pairs are masked with `<MASKED>`. The engine is pattern-based, so a value that matches no known shape is **not** masked at the gates either — the honest boundary is "known-shaped secrets never reach the LLM"; an exact-match denylist from credstore remains a possible hardening (see REVIEW.md).

### Config Sections

`slife.json5` structure parsed by `Config.from_json5`:

| Section | Purpose |
|---------|---------|
| `env` | `${VAR}` references, applied to the environment at startup |
| `models.providers` | Provider configs (api_key, base_url, api, models[]) |
| `active_model` | Currently active model ref (`provider/model`) |
| `agent` | `max_iterations`, `tool_timeout`, `context_floor`, `context_ceiling`, `tool_result_ceiling` |
| `tools` | Per-tool overrides (timeout, enabled) |
| `mcp.servers` | External MCP server configs |
| `memdb.embedding` | Embedding backend config (backend, model, dim, gguf_path) |
| `wechat` | `enabled` toggle |
| `a2a` | A2A config (transport binding, broker host/port, heartbeat, task_timeout) |
| `subagent` | `max_subagents`, `task_timeout` |
| `cli_tools` | External CLI tool definitions (read by the CLI tools directly) |
| `rest_apis` | REST API registrations (read by the REST API tools directly) |

## Health Checks

Health checks fall into two categories. `system_health` runs all of them together, and every dynamic check is also exposed as a standalone native tool (`check_memdb`, `check_wechat`, `check_memfiles`, `check_mcp`, `check_a2a`, `check_watchdog`) so the LLM can probe a single subsystem directly without the full report. `check_mcp` additionally takes an optional `server` argument (default: all) to diagnose just one external server.

**Static startup checks** — `check_external_deps()` probes system tooling once at startup; results are recorded via `slife.health.record()` and appear in `system_health`'s report:

| Dependency | Use |
|------------|-----|
| **node** | Readability.js article extraction (fetch MCP fallback) |
| **npm** | npx-based MCP servers |
| **bun** | JS/TS MCP servers (optional — the shipped config runs them via `npx`) |
| **uv** | uvx-based MCP servers |

**Dynamic runtime checks** — each query inspects current application state:

| Check | What it monitors | Layer |
|-------|-----------------|-------|
| `check_memdb` | Database file + embedding backend (model, dimension, availability) | Application state (memdb plugin) |
| `check_wechat` | Login status, session age, QR expiry | Application state (wechat plugin) |
| `check_memfiles` | File-sharing tunnel online? ngrok URL? | Application state (memfiles plugin) |
| `check_mcp` | Wrapper health + per-server diagnosis (connected/disconnected/disabled, hints) | Application state (MCP wrapper + external servers) |
| `check_a2a` | A2A mesh connection + peer status (via the a2a plugin's `__a2a_status` harness tool) | Application state (a2a plugin) |
| `check_watchdog` | Auto-restart status per plugin, deduplicated from health records (latest record per plugin) | Process layer |

The watchdog only monitors processes — it does not introspect application state. Each plugin owns its own runtime health check. Missing deps are recorded as warnings — Slife still starts; affected MCP servers won't work.

## Logging Convention

Structured log lines: `event_name key1=value1 key2=value2 …` (see `slife/logfmt.py`).

- Event name: snake_case, past-tense for completions (`tool_done`), present-tense for state (`mcp_connected`)
- Levels: `debug` = per-request detail, `info` = lifecycle milestones, `warning` = recoverable, `error`/`exception` = hard failure (use `exception()` to keep the traceback)
- Every line that could contain user input, tool args, tool output, or subprocess stderr passes `sanitize_secrets()` before logging
- Plugins inherit the session id and write to per-session files via `setup_server_logging`; their stderr is relayed by the parent at DEBUG
- No diagnostics on stdout (reserved for the TUI and the plugin port signal)

Known gaps (see REVIEW.md): several call sites log raw user/tool/task content without sanitization, a few use prose instead of `key=value`, and the MCP wrapper stderr relay re-implements `drain_stderr` (it masks via `sanitize_secrets`, but the duplication remains).

## Project Structure

```
slife/
  agent/               # LLM interaction
    loop.py            #   Function-calling loop (streaming, concurrent tool execution, harness auto-invoke)
    service.py         #   Lifecycle manager (plugins, inbox, model switching)
    conversation.py    #   Message storage + history (OpenAI-format, sanitization, _ensure_turn_consistent)
    llm_client.py      #   Backend router + StreamChunk
    system_prompt.py   #   Prompt rendering (static + dynamic Jinja2)
    templates/         #   system_prompt.j2, context_status.j2
    llm_backends/      #   API backends: openai.py, anthropic.py, openai_responses.py
    inbox.py           #   Unified message queue + ConversationStore
    plugins.py         #   Plugin spawn/stop + watchdog (PluginLifecycle)
    multimodal.py      #   Image encoding for vision models
  tools/               # Native tools (auto-discovered, 52)
    base.py            #   Tool ABC + make_params/NO_PARAMS/require_params
    registry.py        #   ToolRegistry
    factory.py         #   Auto-discovery (pkgutil.iter_modules)
    _config_io.py      #   JSON5 read/write helpers
    harness.py         #   _sys_note / _sys_trim (visible-but-reserved harness tools)
    system.py          #   system_health + per-plugin checks
    exec.py            #   Shell, Python, package install (+ _kill_process_tree)
    skill.py           #   Skill management (SKILL.md)
    cli.py             #   External CLI tool management
    rest_api.py        #   REST API tool management (OpenAPI → MCP)
    subagent.py        #   Local worker tools (spawn/list/stop + delegation + task mgmt)
    models.py          #   Model management (model_list/set/remove/switch)
    config.py          #   Config env var + native tool toggles
    credentials.py     #   Credential check/inject/uninject
    vision.py          #   include_image — vision helper (native, conversation-scoped)
    display.py         #   show_image + notify_user (pure UI)
    meta.py            #   list_tools, check_async, cancel_async, clear_context
  plugins/             # Built-in plugins (auto-discovered server.py packages)
    mcp/               #   External MCP gateway (raw JSON-RPC: stdio/SSE/streamable)
    memdb/             #   Diary database (store, search, embeddings, schema.sql)
    wechat/            #   WeChat messaging (iLink ClawBot client)
    memfiles/          #   File cabinet + sharing (server.py owns tunnel + registry, tunnel.py = ngrok)
    a2a/               #   A2A mesh (a2a_* tools + A2AClient, MQTT binding)
  mcp/                 # MCP client infra
    client.py          #   Streamable HTTP client
    tool_adapter.py    #   MCPProxyTool (bridges MCP → Tool ABC, ProxyRoute dispatch)
    process.py         #   MCPWrapperProcess (spawn, port handshake)
    oauth.py           #   OAuth 2.0 device-code flow
  a2a/                 # Agent-to-Agent protocol
    transport.py       #   Abstract transport + TransportMessage
    wire.py            #   Official-shape wire contract (Message/Task/TaskState/envelopes)
    mqtt.py            #   MQTT adapter (paho-mqtt, MQTTv5, LWT)
    client.py          #   A2A client (presence, heartbeat, task routing)
    broker.py          #   Broker TCP probe
    task_store.py      #   In-memory mesh task records
    card.py            #   AgentCard + format_presence_line (TUI/context shared)
    config.py          #   A2A config (transport validation)
    identity.py        #   AgentId, HUMAN/WECHAT sentinels, AgentMessage
  subagent/            # Local workers (agent workers, not A2A)
    headless.py        #   Headless worker-scoped JSON-RPC process
    identity.py        #   SUBAGENT unified-inbox source sentinel
    process.py         #   SubagentProcess + SubagentManager
  ui/                  # Textual TUI
    app.py             #   Textual App, bindings, HistoryInput, StatusBar
    chat.py            #   Chat message widgets
    handler.py         #   TUIHandler (bridges events → widgets)
    tool_display.py    #   ToolCallWidget + display helpers
    image_utils.py     #   Image rendering (Sixel/Halfcell/fallback)
    restore.py         #   Session restore (rebuilds UI from diary)
    approval_prompt.py #   Inline tool approval (Y/N/Esc, no modal)
    slife.tcss         #   Textual CSS
  config.py            # JSON5 config parsing (models, env, plugins, A2A, subagent)
  paths.py             # Filesystem paths (dev vs prod, data dir, DB, memfiles)
  platform.py          # OS detection, shell detection, process lifecycle, notifications
  logfmt.py            # Structured logging + secret sanitization
  server_utils.py      # Plugin contract: create_plugin_server, run_plugin_server, port/signal
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

## Known Gaps & Open Items

The full audit, this round's fixes, and every open item are tracked in **[REVIEW.md](REVIEW.md)**. Highlights of what is **still open**:

- **C9 remainder** — a Streamable HTTP server that answers the SSE-detection GET with a live notification channel but no `endpoint` event is misrouted: after a 5 s wait for the endpoint event it falls through to Streamable HTTP, which a legacy-SSE-only server usually rejects. Servers that reject the GET fall through correctly.
- **M5 remainder** — `memory_count(query, mode="grep")` still lacks the `ESCAPE '\'` clause that `memory_search(grep)` now has.
- **Tests/CI** — subagent process and backend wire formats untested end-to-end; the built wheel is never exercised; install scripts are never smoke-run; no coverage gate.

**Resolved in the 2026-08-11 refactor/fix round** (all regression-tested; see REVIEW.md): C1 non-`"mqtt"` transport, C2 external-server health-check/reconnect + stale session-id, C4 subagent reader guards, C5 true cancel (mesh + worker via the unified inbox), C6 WeChat dedup (key now includes text), C7 approval-dialog Esc, C8 reserved plugin names, C9 SSE-streamed Streamable responses, W1/W2 wire formats, M2–M7, memfiles `_save_url` restricted to publicly reachable http(s) URLs, **watchdog extended to third-party plugins**, **config writes now atomic** (`os.replace` + in-process lock), **`switch_to_nvidia_free` tool names verified** (`list_models`/`get_model_info` on nvidia-nim-mcp v2.1.2, npx/bunx reconciled), **`skill_set_enabled` actually works** (persists to `skills:` config; `skill_list` hides and `skill_use` refuses disabled skills), plus the F-* conformance fixes (memdb English, `_sys_trim` active-conversation targeting, unified `__` filter, `rest_api_set_enabled`/`cli_set_enabled`, memfiles health-pointer refresh, worst unredacted log sites).

## License

MIT
