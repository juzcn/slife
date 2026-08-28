# Slife Design

> Developer documentation for the slife codebase. For installation, configuration, and everyday usage, see [README.md](README.md). For the authoritative definitions of the project's terminology — model-facing and developer-facing alike — see **[Glossary.md](Glossary.md)**. This document covers the design principles, the agent loop, the tool system, the plugin contract, the MCP gateway, the memory database, the A2A mesh, the credential security model, and the full project structure. It is written for people who work on the code, and it assumes you have read the README.

## Contents

1. [Language policy](#language-policy) — also in [Glossary.md](Glossary.md) §Part III
2. [Architecture](#architecture)
3. [Agent Loop](#agent-loop) — context management, harness vs internal tools, system prompt, autonomous heartbeat
4. [LLM Backends](#llm-backends) — wire formats, thinking, prompt caching
5. [Model Management](#model-management)
6. [Tool System](#tool-system) — Tool ABC, schema authoring, auto-discovery, registry, timeout, approval, model switching
7. [Plugin Architecture](#plugin-architecture) — contract, localhost/proxy, watchdog, built-ins, MCP gateway, subagent discovery
8. [Memory (MemDB)](#memory-memdb) — schema, search, embedding, session restore, agent isolation
9. [A2A — Agent-to-Agent (mesh)](#a2a--agent-to-agent-mesh) — MQTT mesh, unified inbox, task store, subagent workers
10. [Image & Memfiles](#image--memfiles) — @-syntax, image display, memfiles & sharefile plugins
11. [Scheduled Tasks](#scheduled-tasks) — schedule loop, trigger→subagent execution, run/report record, startup sweep
12. [UI](#ui) — widgets, timestamps, progressive disclosure, i18n
13. [Config & Credentials](#config--credentials) — two-layer architecture, credstore matrix, secret sanitization, config sections
14. [Health Checks](#health-checks)
15. [Logging Convention](#logging-convention) — sinks, log vs TUI
16. [Dev vs. Production Data Directory](#dev-vs-production-data-directory)
17. [Project Structure](#project-structure)

---

## Language policy

Two audiences, two languages. The model input reads uniformly in English;
the human-facing TUI follows the OS locale.

**Model input — English (uniform):**

- **System prompt** (`agent.j2` / `subagent.j2` + `slife.j2`, `context_status.j2`): English.
- **Native tool schemas** — tool `name`, `description`, parameter docs, and result strings: English.
- **Plugin tool schemas and result strings**: English (same policy as native tools — they are model-visible).
- **External tools** (MCP servers, skills, third-party commands): keep the language of the external source — do not translate. They are opaque and pass through as-is.
- **Logs** (session file + console): English — for developers, per the
  [Logging Convention](#logging-convention) sink split.

**TUI — bilingual (English / Chinese), by OS locale:**

- Detected once at import via [`sys-lang`](https://pypi.org/project/sys-lang/)
  (`get_sys_lang(region=False)`): `zh*` → Chinese, everything else → English.
  `sys-lang` queries `Get-Culture` on Windows (the only path that works on a
  default install — env vars are unset and `locale.getlocale()` returns
  `None`) and `LC_ALL`/`LC_MESSAGES`/`LANG`/`LANGUAGE` on *nix.
- `--lang en|zh` CLI flag overrides detection — `python -m slife --lang zh`
  forces Chinese regardless of the OS locale (`parse_cli_lang` →
  `set_language` in `slife.ui.i18n`).
- The translation layer is `slife/ui/i18n.py` — a single `t(key, **fmt)`
  accessor over an `en`/`zh` string table.  No catalogs, no YAML, no Pydantic.
- Everything the human reads is localized: system messages (plugin load
  results, memory health, restore outcomes), the approval prompt, the model
  picker, the tool-call widget labels, the status bar, thinking blocks.
- **What stays English regardless of locale:** key caps in the status bar
  (`Ctrl+C`, `Esc`, `Ctrl+S`, `Home/End`) — translating a key label breaks the
  key→action scan and mismatches what the user actually presses; and the
  `notify_user` / OAuth notification *body* — that is LLM- or system-supplied
  text, not Slife-authored chrome (only the framing strings — app title,
  "Notification sent", the OAuth title — are localized).
- Tests pin the language to `en` via an autouse fixture in `conftest.py`, so
  the suite's English assertions hold regardless of the dev machine's locale.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  UI (Textual TUI)                                                    │
│  slife/ui/app.py, chat.py, handler.py, tool_display.py,              │
│  restore.py, approval_prompt.py                                      │
├──────────────────────────────────────────────────────────────────────┤
│  Agent Service                                                       │
│  slife/agent/service.py — wires client + tools + loop + plugins      │
│  Manages MCP, MemDB, A2A/MQTT, WeChat, MemFiles, subagent lifecycles │
│  Unified inbox serializes human + WeChat + MQTT + subagent messages  │
├──────────────────────────────────────────────────────────────────────┤
│  Agent Loop                              │  MCP Client                │
│  Streaming function-calling              │  Streamable HTTP transport │
│  Context trim (internal, after save)     │  OAuth device-code flow    │
│  + status (_sys_note); concurrent tools  │  Tool proxy + adapter      │
│  Reasoning (thinking) support            │                            │
├──────────────────────────────────────────┴───────────────────────────┤
│  Tool Registry — unified OpenAI function definitions for all tools   │
│  Native · MemDB · MCP Proxy · Skills · CLI · REST API · A2A          │
├──────────────────────────────────────────────────────────────────────┤
│  Plugins (independent child processes, Streamable HTTP)              │
│  slife-mcp (gateway) · slife-memdb (turns DB) · slife-wechat ·       │
│  slife-a2a (A2A over MQTT) · slife-memfiles (notes/diary/files) ·    │
│  slife-sharefile (public sharing) · slife-media (generation)         │
├──────────────────────────────────────────────────────────────────────┤
│  Platform (slife/platform.py)  │  Config (JSON5)  │  Health checks   │
├──────────────────────────────────────────────────────────────────────┤
│  Credstore — credential store + AES cryptfile backup                 │
│  Win · Mac · Linux (keyutils) · WSL (PowerShell)                     │
└──────────────────────────────────────────────────────────────────────┘
```

## Agent Loop

Single function-calling loop. Every tool is registered as an OpenAI function definition in one `ToolRegistry`. The LLM decides what to call and when.

```
User Input → MessageHistory.add_user_message()        (secrets sanitized)
  → loop (max_iterations):
    → cancel check
    → auto-invoke _sys_note (context status)        (usage computed once)
    → LLM stream → thinking/text/tool deltas → handler callbacks
    → tool calls? → ToolRegistry.execute() concurrently (asyncio.gather)
                    → sanitize_secrets() on each result → truncate → loop
    → `_approve: true` on a call? → serialized ApprovalPrompt before execution
    → no tool calls? → response text → return
    → save turn to diary (unconditional — even on cancel/error/max-iterations)
    → trim after save (internal, real usage)        (see Context Window Management)
```

- **Streaming**: thinking and text tokens delivered in real time via `AgentEventHandler` callbacks
- **Tool accumulation**: tool-call deltas accumulated across chunks, executed as a batch
- **Concurrent execution**: all calls in a batch run via `asyncio.gather`; approval dialogs serialize behind a lock
- **Tool timeout**: single enforcement point — `asyncio.wait_for()` wraps every call (default 60 s, `agent.tool_timeout`). Per-call override via `_timeout`; tools with a native `timeout` parameter (`execute_shell`) receive it directly instead of a double wrap
- **Background execution**: per-call `_async: true` schedules the tool as a background task and returns a task id immediately; poll with `check_async`, cancel with `cancel_async`
- **Iteration limit**: `max_iterations` (default 30) prevents infinite loops; **0 = unlimited** (the loop only ends via a final response or cancellation). The cap is checked **live each iteration** (not fixed at `run()` start), so a mid-turn `set_max_iterations` (the `set_max_iterations` meta tool) applies to the running turn immediately and to the next. Hitting the cap returns a cancelled result and notifies the handler via `on_max_iterations` — the TUI shows `✗ Agent exceeded maximum of N iterations` instead of stopping silently.
- **Cancellation**: `Esc` sets a cancel event; checked before each iteration, after each stream, and before each tool batch
- **Turn consistency**: one function — `MessageHistory._ensure_turn_consistent()` — enforces two idempotent invariants before a history is persisted (and again on load), so it is always well-formed when it next reaches the wire:
  1. **No orphaned tool_calls** — an assistant `tool_call` whose result never arrived (an interrupted turn, e.g. a hung tool) gets a synthetic `(Tool execution interrupted)` result inserted right after it; otherwise the orphan is persisted and re-repaired on every restore.
  2. **Alternating roles** — a history ending on a `user`/`tool` message (a tool result is a `user` role on the Anthropic wire, which rejects two consecutive users with a 400) gets a closing assistant message.

  It has exactly **two call sites**: `save_to_memory` (before persisting — the save-side guarantee, which runs unconditionally after every turn via the inbox `finally`), and `restore_session` (after loading from memory — the load-side guarantee). Because every turn is saved unconditionally, the history is always left consistent before the next user message is appended. Each turn also opens with an auto-invoked `_sys_note` assistant+tool pair, so a user message is always sandwiched between assistant messages.
- **Context tracking**: `AgentLoop.context_tokens_for()` is the single source for the current context size (actual `prompt_tokens` from the last API call, else the restore-time value primed on `_last_usage` — now the **latest restored turn's persisted `prompt_tokens`** rather than an estimate — before the first call, else the chars÷3 live estimate). It drives `_sys_note`, the trim decision, and the TUI status bar — one value, no recompute. Usage is tracked **per history** (`_usage_by_history`, keyed by `id()`): the heartbeat, A2A, and WeChat turns run in their own (often tiny) histories, so a global last-usage would let a 9.6% heartbeat drag the human history's status bar / `_sys_note` down from its real 26.5%. Each history keeps its own reading; `_last_usage` is retained only as the restore-time slot.

### Context Window Management

Active history stays within `context_floor`–`context_ceiling` (default 20%–80% of `context_window`):

```
                context_window
┌──────────────────────────────────────────────────────────────┐
│   trimmed (in diary —        │  current context  │  headroom  │
│   recall via turn_search)  │  floor ~ ceiling  │  1-ceiling │
└──────────────────────────────────────────────────────────────┘
```

- **Detect**: context usage is computed via `context_tokens_for()` — the history's last API call's actual prompt tokens after the first round (per-history, so heartbeat turns don't contaminate the human reading), else the restore-time value primed on `_last_usage` (the latest restored turn's **persisted `prompt_tokens`** — the exact context size at exit — not an estimate), else the chars÷3 live estimate; `_sys_note` reports it as the usage %
- **Trim**: happens **after a turn is saved** (`save_to_memory` → `AgentLoop._trim_after_save`) — by then the last API call's real `prompt_tokens` are known, so the ceiling check uses the true context occupancy, not the estimate the loop had at the turn's start. When occupancy hits the configured `context_ceiling` (default 80%), `extract_oldest_turns` removes the oldest **complete** turns down to `context_window × context_floor` (default 20%), always keeping the current (just-saved) turn. It is an **internal mechanism — no tool call, no LLM-visible pair**: the cut is marked with a runtime-only **`[INFO: N oldest turns have been removed from context]`** note appended to the last assistant message (N = turns removed), mirrored in the live TUI as a dim/italic footnote. `advance_context_start` persists the boundary, and the tracked "Context covers" time range advances by the same count (reset to the current turn if the date list is exhausted). A freshly-restored history is exempt from the first-turn trim (`_just_restored_history`) — a restored context is a pre-exit state, not growth.
- **Status**: once per turn the loop auto-invokes **`_sys_note`** (a normal tool-call pair) — it renders `context_status.j2`: current time, context usage %, token usage, context time range, change notifications (model/CWD/shell/modalities), and any A2A peer presence events since the last turn (online/offline/timeout, drained read-once). On the first round after a restore, `context_tokens_for` falls back to `_last_usage`, which restore primes with the latest restored turn's **persisted `prompt_tokens`** — so `_sys_note` reports the real exit-time occupancy instead of an estimate.
- **Restore**: on startup, the diary rows recorded **after the persisted live-context boundary** are loaded directly from SQLite **verbatim** — no ceiling re-slicing. The boundary already encodes the trimmed state, so restore simply replays the exact slice that was live at exit (the agent picks up where it left off); only a stale boundary of `0` from a pre-boundary DB is defensively capped at 2× the ceiling. The boundary lives in `diary_meta.context_start` (exclusive rowid): the internal trim advances it by the turns it evicted, `clear_context` flushes it to the latest row (the fresh start). `get_recent_turns` returns `(turns, skipped=0, budget=0)` — skipped/budget are kept for call-site compatibility only. The just-restored history is exempt from the first-turn trim (`_just_restored_history`). The boundary reuses the existing `diary_meta` table (ships idempotently in `schema.sql`) — there is no migration layer (backward compatibility is not supported): schema changes land directly in `schema.sql` and apply to fresh databases only.
- **Tool result cap (HARD constraint)**: a single tool result is truncated at `tool_result_ceiling × context_window × 3` characters (default 20% of the window; ~3 chars/token heuristic). This is the **hard** window-safety limit — it is deliberately generous so a large-but-real file read (≤ ~600K chars) is never truncated, and only pathological outputs that could not fit the window at all are capped. It protects the model's live reasoning; it is not where memory is saved.
- **Permanent-memory compaction**: the diary does **not** hoard reproducible tool output. At `save_to_memory`, any tool result exceeding `memory_tool_result_chars` (default 8000) is stored as a head+tail digest with an explicit marker (original size + which tool to re-run). Small results are stored as-is. Rationale: tool output is reproducible (re-run the tool), a single result must never starve session restore within the floor budget, and turn_search recall stays cheap. The live history keeps the full result — compaction only affects the persisted copy.
- **Truncation is announced in the tool output itself** (not the system prompt): both the live cap and the save-side compaction append a marker inside the result telling the model it was truncated, how large it originally was, and that re-running the tool retrieves the full version.

### Harness vs Internal Tools — a naming distinction

Two distinct concepts live under different prefixes. They are **not** two
tiers of the same thing:

1. **`_` (single underscore) = harness, LLM-visible but reserved.** Harness
   tools are invoked by the agent loop *on the agent's behalf* — the LLM does
   not decide to call them. The only one is the native `_sys_note`
   (`slife/tools/models.py`): `AgentLoop._auto_invoke()` injects it each turn
   as a normal `assistant(tool_calls)` + `tool` pair. It **does** appear in the
   schema — required so the Anthropic / OpenAI-Responses backends accept its
   tool-call pair in history — and the system prompt forbids the LLM from
   calling it. `_sys_note` is pure (only reads state). Context trimming is
   **not** a tool: it runs internally after each save (`_trim_after_save`),
   marking the cut with a runtime `[INFO: N oldest turns have been removed from
   context]` note — no `_sys_trim` in the schema, no pair to validate. Note: `attach_image` is also auto-invoked
   via `_auto_invoke`, but it has no `_` prefix and is not schema-reserved, so
   it is not a harness tool.
2. **`__` (double underscore) = plugin internal tool, LLM-invisible.** This is
   a **plugin-spec marker**, not a harness concept. Plugin internal tools
   (`__memory_save_turn`, `__a2a_drain_incoming`, `__wechat_drain_incoming`,
   `__mcp_call_tool`, …) are ordinary MCP tools that happen to serve the main
   process (agent service / TUI) rather than the LLM. They are filtered out of
   the schema before registration — they never reach `to_openai_functions()`
   — and are called programmatically via `client.call_tool("__…")`.

| Tool | Shape | Category |
|------|-------|----------|
| `_sys_note` | Native tool, auto-invoked each turn | Harness — visible-but-forbidden |
| `__memory_save_turn` / `__memory_get_recent_turns` | memdb plugin | Internal — invisible |
| `__wechat_drain_incoming` / `__wechat_dispatch_reply` | wechat plugin | Internal — invisible |
| `__a2a_drain_incoming` / `__a2a_dispatch_result` | a2a plugin | Internal — invisible |
| `__mcp_connection_status` / `__mcp_call_tool` | mcp plugin | Internal — invisible |
| `__tunnel_status` / `__register_file` | sharefile plugin | Internal — invisible |

Both registration paths share one `__` predicate — `is_internal_tool()`
(`slife/server_utils.py`) — so no plugin internal tool leaks into the schema,
whichever path (generic spawn or subagent connect) registered the plugin.

### System Prompt

The system prompt splits **identity** from **world** so each role reads one coherent document:

- **Identity** — `slife/agent/templates/agent.j2` (main agent) / `subagent.j2` (worker): who the agent is. Role framing only — heartbeat/persistence ownership for the main agent, ephemeral/send-only constraints for a worker. The only part that carries persona.
- **World** — `slife/agent/templates/slife.j2`, `{% include 'slife.j2' %}` by both identity templates: the runtime spec — context policy (floor/ceiling/tool-result %), host platform (OS, arch, shell, python), workspace paths (data/config/logs/db/images/skills), credstore backend name, MCP tool naming prefix, and A2A broker info when configured. Byte-identical in both roles.
- **Dynamic** — `slife/agent/templates/context_status.j2`, rendered by the `_sys_note` tool (auto-invoked once per turn): current time + UTC offset and context usage % always; context time range when set; model/CWD/shell/modalities only when changed; pending A2A peer presence events since the last turn (the same lines the TUI shows, drained once); open failed/missed scheduled runs (one deduplicated "backfill or skip?" list, refreshed by the schedule loop and the startup sweep).

Identity + world are rendered once at startup and never change → maximal prompt cache hit rate.

Design principles:
1. **World spec is project-specific facts only** — if the LLM can infer it from tool schemas or training data, it doesn't belong
2. **Tool schemas over prompts** — usage instructions live in function `description`/`parameters`
3. **No personality in the world spec** — role identity lives in the identity templates, not in `slife.j2`
4. **No slash commands** — natural language only; the LLM interprets intent
5. **Static baseline + change notifications** — constants at startup, deltas per-turn

### Autonomous Heartbeat

The agent is otherwise purely user-driven — no input, no activity. A heartbeat gives it a periodic **autonomous window** (a precondition for emergent self-initiated behavior): while idle, every `agent.heartbeat_interval` seconds (default 60) the service posts a `[Heartbeat]` message to the inbox, which runs as a **normal agent-loop turn** (own history via the heartbeat source, saved to the diary like any turn). The interval is read from `service.config.heartbeat_interval` (parsed from the `agent` section of `slife.json5`), falling back to 60.

- **Reply contract** (also in the system prompt, under **Autonomy** → Heartbeat in `agent.j2`): real content if the agent has something worth proactively saying, otherwise exactly `.` — never empty (the `.` is the minimal non-empty assistant reply, satisfying the user→assistant role alternation).
- **TUI filtering** (live + restore): heartbeat turns are recognised by the `[Heartbeat]` mark on the trigger message and filtered — the trigger is never shown, and a real reply renders as `⚡ 自主`. More generally, a bare `.` reply is **silence** and is never rendered from any event (heartbeat, A2A async-completion notification, …) — the TUI handler skips a lone `.` text chunk and restore skips any assistant message whose content is exactly `.`. The status bar shows the last beat (`●` act / `·` quiet).
- **Main agent only**: subagents (`is_subagent=True`) never start the heartbeat loop — they are task-driven workers, not autonomous agents.
- The heartbeat history is separate (source `heartbeat`), so the autonomous reflections persist in the diary without polluting the human history.

### Context Injection

The system introduces information into the context on its own initiative in three ways, distinguished by *what* is injected and *whether it persists*. Two orthogonal notions run through the forms below. The **channel** names the source of the message — its sender identity relative to the main agent's inbox, recoverable from the message alone, persisted with the turn, and by default not part of the LLM context. The **marker** is the machine-generated notation an injection carries (`[Heartbeat]`, `[Schedule <name>]`, the `[INFO: …]` footnote/trim note). A marker never determines a channel and a channel never forces a marker: a scheduled task, for instance, is a marker whose trigger rides the system channel and whose completion rides the subagent channel.

1. **A new user message, marker-carrying — persistent.** The heartbeat (`[Heartbeat]`) and scheduled-task triggers (`[Schedule <name>]`) are posted to the inbox as synthetic user messages. Each runs as a normal agent-loop turn and is saved to the diary like any turn. The marker lets the TUI filter the trigger (never shown) and surface only a real reply (`⚡ 自主`). See *Autonomous Heartbeat* and *Scheduled Tasks*.
2. **A harness tool-pair — persistent.** A harness tool is auto-invoked once per turn and contributes an assistant `tool_call` plus its tool-result to the history — `_sys_note` (context status). The pair is part of the turn, so it persists; it exists to keep the context state visible to the model without spending an LLM iteration deciding to call it. Context trimming is not such a tool: it runs internally after each save and is announced by the trim note (form 3), not by a harness pair.
3. **Info appended to an existing message, marker-carrying — not persistent content.** The turn footnote (`[INFO: {"turn_id": N, "begin": …, "end": …}]`) is appended to a user message after the turn saves (so the next call can reference the turn), and the trim note (`[INFO: N oldest turns have been removed from context]`) announces evicted turns. These annotate a message already present rather than injecting a standalone one; they are reconstructed as metadata, not stored as injected content.

Forms 1 and 2 add real turns/pairs to the record; form 3 decorates what is already there. All three keep the model informed without waiting for user input.

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
| OpenAI Completions | `extra_body.thinking.type = "enabled"` (+ optional `reasoning_effort`) | DeepSeek requires explicit `"disabled"` when off; thinking streamed from `delta.reasoning_content`. **`compat.thinking`** overrides per model: `"omit"` sends no thinking field (MiniMax-M3-style gateways that 400 on the enabled shape but reason natively), `"disabled"` forces explicit off, `"enabled"` matches the default |
| Anthropic Messages | `thinking.budget_tokens = max(max_tokens // 2, 1024)` | `compat.thinkingFormat: "openai"` (Bailian/Qwen) sends no thinking param — the model always thinks |
| OpenAI Responses | `reasoning.effort` (default `"medium"`) | Streams both `reasoning_text` and `reasoning_summary_text` deltas |

**Prompt caching (Anthropic system blocks):** `AnthropicBackend._oa_msgs_to_anthropic` emits each OpenAI `system` message as an Anthropic system content block and tags the **last** one with `cache_control: {type: "ephemeral"}` — the static base prompt becomes the cache breakpoint, so only the dynamic `_sys_note` status (a message-stream tool pair, never a second `system` message) changes per turn. Guarded by `_use_system_cache_control()`: on by default for `api.anthropic.com`, off for Anthropic-compatible providers (Bailian/Qwen) that may reject the field, overridable per model via `compat.cacheControl`.

**History validation (H3, resolved):** Anthropic (and OpenAI-Responses) reject tool calls in history whose names aren't in the declared `tools` list. `_sys_note` is therefore a **declared native tool** (schema-present, auto-invoked by `AgentLoop._auto_invoke()`), not a history-layer fabrication — so its pair validates. The system prompt forbids the LLM from calling it (see Tools & skills, §3 under **Capabilities** in `slife.j2`), and it is side-effect free if it does. DeepSeek (Chat Completions) doesn't validate and is unaffected. Context trimming no longer needs schema validation at all — it is internal (`_trim_after_save`), not a tool call.

**History wire shape (W2, resolved):** `OpenAIResponsesBackend._oa_msgs_to_responses` emits the Responses API's native `function_call` / `function_call_output` items for tool history — not the Chat-Completions `role:"tool"` / `tool_calls` shape. Multi-turn tool histories are accepted by the Responses API (unit-tested; not yet exercised against a live endpoint).

**External placeholder injection (upstream, not a slife bug):** An Anthropic-Messages gateway that runs LiteLLM's prompt sanitizer rewrites an empty `text` block sitting next to a `tool_use` into the literal `[System: Empty message content sanitised to satisfy protocol]` — `_EMPTY_TEXT_PLACEHOLDER` / `_sanitize_empty_text_content` in LiteLLM's `litellm_core_utils/prompt_templates/factory.py` (issue BerriAI/litellm#24498; fix PRs #28987, #34822). An assistant turn that is `content: ""` + `tool_calls` is the ordinary shape between a tool call and its result, so Anthropic accepts it with the empty text block dropped — the substitution is a LiteLLM defect, and it runs **outside** the `modify_params` gate, so there is no config knob to disable it. Observed in slife via the `bailian_personal` provider (2026-08-21, 4 copies in the diary). It **poisons history**: the placeholder persists verbatim into the diary and replays into the next request, and the model then echoes it back (one observed echo even garbled it: `protection` for `protocol`). slife stores it as ordinary assistant text — contrast with slife's own wire hardening, which lives on the **outbound** request and cannot see a placeholder the gateway already substituted into the **response**: `OpenAIBackend._normalize_messages` replaces empty assistant content with `"…"` (a copy — storage untouched, so openai-completions providers never 400), and `AnthropicBackend` emits a single empty text block for an empty assistant turn. If the gateway substitutes anyway, the only recourse is cleaning the persisted rows (a one-off data cleanup stripping that placeholder) before restore replays it.

## Model Management

Runtime model management via native tools — no config editing needed:

| Tool | Description |
|------|-------------|
| `model_list` | All configured models grouped by provider (active marked) |
| `model_set` | Add/update a model (creates provider if new) |
| `model_remove` | Remove by ref; auto-switches if it was active |
| `model_switch` | Switch active model by ref — persists to config and rebuilds the client live |

`model_set` is an **upsert that merges, not replaces**: a partial update (e.g. only `max_tokens`) keeps the model's existing `reasoning`, `input`, `compat`, and other fields, so a field-focused change can't silently strip a model's thinking/vision capability. It also accepts a `compat` dict (e.g. `{thinking: "omit"}` or `{thinkingFormat: "openai"}`), so per-model compatibility overrides can be configured without hand-editing `slife.json5`. `model_list` surfaces the `compat` dict on each model.

Model switches fire callbacks that rebuild the LLM client, update loop parameters (vision, context window, modalities), and re-render the system prompt.

## Tool System

### Tool ABC

`Tool` (`slife/tools/base.py`) defines `name`, `description`, `parameters` (JSON Schema), `category`, and `async execute(**kwargs) -> str`. Required fields are validated at class-definition time via `__init_subclass__`. `from_config(cfg, config, ctx)` allows per-tool construction from the `tools:` overrides in `slife.json5` (e.g. `execute_shell` reads its default timeout there); `ctx` carries runtime references (registry, config, MCP client, history) as `self._ctx`.

`execute_shell` runs commands in the **detected shell** — `detect_current_shell()`: PowerShell / cmd on native Windows, `$SHELL` on POSIX incl. WSL — the **same value the system prompt reports**, so the LLM's shell syntax actually executes (previously it ran `COMSPEC`=cmd.exe regardless). Output is decoded with the system code page (GBK/cp936 on zh-CN Windows); `run_python_script` forces the child Python to UTF-8 via `-X utf8`.

The tool categories and the naming rules (native vs. built-in plugin vs. external-server tools) are defined in [Glossary.md](Glossary.md) — see **Tool naming** and **Appendix B**.

### Schema Authoring

The schema is the model's only view of a tool — write it for the model, not the maintainer:

- **`description` = what the tool does.** One or two sentences: what it does and what it returns. Do **not** write when-to-use ("Use when…", "when the user says…"), and do **not** restate knowledge the LLM already has (pip, timeouts, env-var concepts). Keep project-specific facts the model cannot infer — idempotency ("upsert — add + update in one call"), blocking ("BLOCKS until the model is loaded"), effect timing ("takes effect after restart"), or that a value comes from a sibling tool.
- **Parameter docs = how to use.** Per parameter: the accepted format, where the value comes from ("`turn_id` from `turn_list`"), what the values mean, and the default. Cross-references to sibling tools are how-to and belong here; only when-to-use is dropped.
- **Mechanism.** Native tools carry parameter docs directly in the `parameters` dict. Plugin tools (`@mcp.tool`) get them from a Google-style `Args:` docstring — fastmcp parses it into the input schema, so a plugin tool whose parameters have no `Args:` yields an undocumented schema.
- **Language.** Model-visible strings are English (see Language policy). Content authored by an external source (CLI / API / skill descriptions) keeps the source language — do not translate.

### Auto-Discovery

`slife/tools/factory.py` uses `pkgutil.iter_modules` to import every module in `slife.tools.*` (skipping `base`/`factory`), then walks `Tool.__subclasses__()` recursively. A new `.py` file is automatically picked up. Filtering applies `enabled: false` overrides, `_skip_auto_register` classes (e.g. `MCPProxyTool`, created per-instance at runtime), and per-model requirements enforced at **execute time** rather than load time: tools are always registered, and a tool like `attach_image` refuses at runtime when the active model has no vision (so the LLM sees the tool and gets a clear "vision=false" error instead of a silently-missing tool).

### Tool Categories

All tools are unified under `Tool` and registered in a single `ToolRegistry`; the LLM sees only function names and schemas. The full inventory — native tools by category, built-in plugin tools by plugin, and the naming rule for external-server tools — is maintained in the authoritative [Glossary.md](Glossary.md) (see **Appendix B — Tool Families by Name** and **Tool naming**). The developer-facing mechanism lives here: auto-discovery, the registry, timeouts, and the approval gate.

**Managed categories** (Skills / CLI / REST API / Models / MCP) support a standard **`X_list` / `X_set` / `X_remove`** surface (plus `X_set_enabled` where an enable/disable toggle applies). `X_set` is an idempotent upsert — add + update in one call. Config uses the `config_env_*` prefix (no `config_list`); Models substitutes `model_switch` for `X_set_enabled`. `model_set`'s upsert **merges** into the existing entry (a partial update preserves `reasoning` / `input` / `compat`), so a field-focused change can't strip a model's capabilities; it also accepts a `compat` dict for per-model overrides (see Model Management).

### Registry

`ToolRegistry` is a name-keyed dict with `register` / `unregister` / `unregister_by_prefix` / `get` / `list_tools` / `to_openai_functions` / `execute`. Dynamic tools — built-in plugin tools and the MCP wrapper's own tools — are registered at runtime as `MCPProxyTool` instances under their **bare names**; external MCP server tools are named `"{server}__{tool}"`. Internal plugin tools (prefixed `__`) are filtered out before registration.

### Timeout Architecture

Single enforcement point at the Agent Loop level. `_inject_meta_params()` adds `_timeout` (number), `_async` (boolean), and `_approve` (boolean) to **every** function definition sent to the LLM:

- Tools **without** a native `timeout` parameter → `asyncio.wait_for(timeout=…)`
- Tools **with** a native `timeout` (`execute_shell`) → mapped to the native argument, no double-wrap

The MCP client applies no timeout of its own; enforcement stays in one place.

### Approval Gate

Approval is **model-driven** (pure model judgment). The loop injects an `_approve` boolean meta-parameter on every tool schema (alongside `_timeout`/`_async`, visible on all three backends). When the LLM sets `_approve: true` on a call, the tool call is only surfaced to the UI once approved — execution pauses and an inline `ApprovalPrompt` row is mounted in the chat stream (Claude Code style, no modal: Y = approve, N / Esc = deny). Prompts serialize behind a lock. A denied call never mounts a `ToolCallWidget`; the prompt row itself carries the rejection state.

There is no hardcoded `requires_approval` flag on any tool or MCP server — the model decides per-call whether to ask the user. Headless (subagent) contexts have no handler and auto-approve.

The inline prompt declares its own `y → approve` / `n`/`escape → deny` bindings at `priority=True`; the App's `escape → cancel` is deliberately *not* priority so Textual's priority pass (which resolves the App before the focused widget) cannot steal Esc — Esc on an approval always denies.

### Model Switching

The active model is switched via the `model_switch` tool (natural language) in normal operation. A `Ctrl+S` inline picker (same interaction style as `ApprovalPrompt`) is an **emergency escape** for when the current model is unavailable and the LLM can't call `model_switch` itself — switching is config + runtime only, no API call. `AgentService.switch_model(ref)` validates, persists `active_model` to the config file, and rebuilds the LLM client / loop / system prompt.

Picker rules (hard-won):

- Pure priority bindings — `↑`/`↓` move a cursor, `Enter` picks, `Esc` cancels. No `_on_key` / `on_click` overrides (they swallowed keys). The cursor opens on the active model, so a bare `Enter` re-selects it.
- The binding action must be **sync**: binding actions run inside the key-event handler (`App._on_key` → `_check_bindings`), so awaiting the picker's future there deadlocks the TUI (the picker needs key events to resolve). The await lives in a background task (`_finish_model_switch`).
- Scroll to the picker **after layout** (`call_after_refresh(scroll_end)`) — an immediate scroll runs against the pre-mount content and the picker's insertion leaves the view pinned at the top (picker below the fold).
- Key is `Ctrl+S` ("s" = switch). Not `ctrl+m` (Textual aliases it to enter), not `ctrl+g` (VSCode's goto-line steals it).
- Every configured model is listed (no cap); the chat scrolls if the list is taller than the viewport.

## Plugin Architecture

Six built-in plugins run as independent child processes, plus the standalone
`mcp-plugin` MCP gateway. Communication is via **Streamable HTTP** (MCP
protocol) for all of them — the sharefile plugin additionally serves plain-HTTP file bytes on the same port via a custom route (`GET /share/{token}`), but its control surface is pure MCP.

**WSL note:** Custom env vars set via `create_subprocess_exec(env=…)` are NOT forwarded to Windows `.exe` processes through WSL interop. `WSLENV` is only read by the WSL `/init` at session start, not by child processes. Therefore, **all MCP server runtimes on WSL must be Linux-native binaries** — the install script enforces this by detecting `/mnt/*` paths and installing native versions.

### The Plugin Contract

1. Bind a free port: `bind_free_port()` pre-binds `127.0.0.1:0` and keeps the socket — no race between port discovery and server start
2. Signal the parent **once ready**: `run_plugin_server` wraps the server's lifespan and emits `signal_port(port)` (`{"port": N}` on stdout, then closes stdout) only **after** the app is ready to serve MCP — i.e. after the plugin's lifespan (if any) completes. The signal means *"ready to serve MCP on this port"*, aligning with the MCP startup handshake: the parent's first `initialize` always lands on a ready server. The lifespan stays handshake-fast (see Readiness), so the signal is never held up by heavyweight startup — which is deferred to `warm_after_handshake`. Plugins must **not** signal early themselves
3. Start FastMCP on Streamable HTTP with the pre-bound socket
4. Define `@mcp.tool` functions; optionally serve plain-HTTP endpoints on the same port via `@mcp.custom_route(path, methods=[...])` (e.g. memfiles `GET /share/{token}`)
5. Be importable: `python -m <module>.server`

**Public vs internal tools.** Every `@mcp.tool` is a normal MCP tool. Most are
**public** — registered as proxy tools under their **bare names** and exposed
to the LLM (first-class, like native tools). A tool is **internal** when its
name is prefixed `__` (double underscore): it is *not* exposed to the LLM, and
is called programmatically by the main process (agent service / TUI) via
`client.call_tool("__…")`. The `__` marker is the plugin-spec convention — a
single shared predicate, `is_internal_tool()` (`slife/server_utils.py`),
filters these out on both registration paths, so an internal tool never leaks
into the schema. This is
distinct from the harness concept (single `_`, e.g. the native `_sys_note`),
which is LLM-visible-but-reserved and auto-invoked by the loop — see [Harness
vs Internal Tools](#harness-vs-internal-tools--a-naming-distinction).

**Readiness (MCP initialize handshake).** Readiness is defined by MCP
itself: a plugin is ready when the harness's `initialize` handshake
completes — the client sends `initialize`, the server answers, the client
sends `initialized`, and the session enters READY. A plugin server only
answers `initialize` after its own FastMCP lifespan has completed, and each
plugin encodes its *own* serving requirement in that lifespan — memdb and
memfiles require their store to be usable (connection open, schema in
place, a query succeeds); the other plugins have no local requirement, so
serving is their readiness. A lifespan that cannot meet the requirement
fails, the port signal never fires, and the spawn fails — the plugin is
reported FAILED (its watchdog backs off and retries). Subordinate or
external dependencies (mcp's external servers, sharefile's tunnel, wechat's
login, media's providers, a2a's broker, a store's embedding backend) are
NOT readiness conditions: they are uncontrollable and self-heal at runtime,
and are surfaced separately via their own status tools (`__mcp_connection_status`,
`__tunnel_status`, `__a2a_status`, `system_health`, …) — they never gate
readiness. The lifespan is therefore **handshake-fast by contract**: it
establishes only the minimum needed to serve and never holds the event loop
while the wrapper is connecting. Work that can stall or temporarily freeze
the loop — a GIL-holding embedding-model load, slow I/O, long connects — is
deferred until after the first `tools/list` by
`warm_after_handshake(factory)` (`slife/server_utils.py`), which runs it in
the background once the handshake completes (a failure logs and shows on the
status surfaces, never blocks readiness). A plugin with no startup work
still declares a lifespan (media) so every built-in shares the same
complete-MCP-lifecycle shape.

**Startup convergence gate.** The service opens for user input only after
every attempted plugin spawn has converged — each reached ready, skipped, or
failed. Readiness is binary: the initialize handshake either completed
(ready) or the spawn failed; there is no start-time "degraded" — runtime
degradation of subordinates lives in the status surfaces. The inbox consumer
and the TUI input both await the single event (`_startup_settled`), so no
turn runs and nothing can be typed while plugin startup is still settling —
this is what keeps user input from racing ahead of core services. The gate
is event-driven: set by the last spawn's completion, never polled, never
time-bounded; the only timeout is a 30 s hang-guard on a stuck spawn so
convergence still fires.

**Required plugins (`plugins.required`).** A plugin named in the
`plugins.required` list of `slife.json5` is a **core component**: failing to
become ready — a FAILED spawn, a raised spawn, or the 30 s hang-guard —
**aborts startup** with a red message, stops all plugins, and exits non-zero
(never limps on without a core component). `memdb` and `memfiles` are
required in the standard configuration because memory is core. Every other
plugin defaults to non-required: a broken memory backend that is *not*
required fails loudly where it is used (the inbox freezes with a red banner
on the first unsavable turn) instead of aborting startup. Required plugins
are declared per instance in the config, so which components are
non-negotiable is a deployment decision, not a hardcoded set.

No base class, no import hook, no SDK. Built-in plugins are auto-discovered by scanning `slife.plugins.*` for packages with a `server.py`; standalone distributions (e.g. `mcp-plugin`, the external MCP gateway) register instead via `plugins.external` in `slife.json5` and are spawned through the same generic lifecycle. Each `server.py` uses `create_plugin_server(...)` for logging + FastMCP setup and `run_plugin_server(mcp)` (or `run_plugin_server(mcp, sockets=[sock])`) for the single entry call. The parent reads the port line with a 30 s readiness budget, then connects once. Because the signal is deferred until the app is ready, slow lifespan startup (e.g. sharefile's ngrok tunnel, a2a's MQTT connect) cannot race the handshake — the parent simply waits for the signal. In practice uvicorn finishes mounting the Streamable HTTP endpoint ~1 s *after* the lifespan signals, so a session established in that window can get a bad SSE transport on Windows/Proactor that hangs `tools/list`; the harness runs that call through `asyncio.timeout` (which, unlike `asyncio.wait_for`, breaks the hang reliably) and, on a timeout, reconnects a fresh session and retries once — by then the plugin is serving, so the race self-heals instead of failing the load.

The MCP client keeps bounded retry (6 attempts, 0.5 s apart, each attempt time-boxed at 10 s including transport setup) as **defense-in-depth**: a plugin that signals early (violating the contract) still loads instead of hanging.

### Localhost Never Goes Through a Proxy

Every `MCPClient` connection is loopback — the harness connects only to
`http://127.0.0.1:{port}/mcp` (main process ↔ local plugins, and subagents ↔
the shared local plugin; verified across all call sites in `process.py`,
`service.py`, and `plugins.py`). Loopback traffic must therefore **never**
consult the OS proxy.

The MCP SDK's `streamable_http_client` builds a default httpx client with
`trust_env=True`, which reads the OS proxy configuration (Windows system
proxy, or `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` env vars) and applies it
to *every* request — `127.0.0.1` included. On any machine with a proxy
configured (e.g. a Windows system proxy `127.0.0.1:7890` for GitHub pushes, or
a corp proxy exported in the shell), the plugin's connect / `tools/list`
requests get routed through the proxy, which typically answers `502` for
loopback — the connect retry (6 × 10 s) burns up, `start_plugin_server` never
returns, and the TUI shows **no** `🔌 插件已加载` messages at startup (only
`SKIPPED` results — which return instantly — still surface). This is a latent
bug, not a config problem: it fires for any user with *any* proxy configured,
and we cannot know their `NO_PROXY` setup.

**Fix (2026-08):** `MCPClient.connect` supplies its own
`httpx.AsyncClient(trust_env=False)` to `streamable_http_client` — a provided
client is owned by `MCPClient` (the SDK does not manage its lifecycle), so it
is closed in `_cleanup`. `trust_env=False` also drops `NO_PROXY` on these
connections, which is deliberate: there is nothing a `NO_PROXY` list would
legitimately exclude for loopback-only traffic, and it removes the dependency
on the user's proxy config being correct.

**Scope — external MCP servers are unaffected.** Outbound traffic to the
configured external MCP servers (`mcp-plugin.json5` → `servers`) goes through the *mcp-plugin*
gateway's own client (`mcp-plugin/mcp_plugin/connection.py`), which keeps
`trust_env=True` — a remote server that genuinely needs the proxy still gets
it. Only the local
plugin client is proxy-free. Regression test:
`TestMCPClientConnect::test_connect_passes_proxy_free_http_client`.

### Watchdog (Auto-Restart)

Each plugin runs with a **watchdog** background task that monitors the child process and auto-restarts it on unexpected exit:

| Feature | Detail |
|---------|--------|
| Detection | `await subprocess.wait()` — blocks until the child exits |
| On crash | Unregisters the plugin's proxy tools (`unregister_by_prefix("{name}__")`), then restarts the process |
| Backoff | Exponential: 1 s → 2 s → 4 s → … → 30 s max |
| Max restarts | 5 consecutive failures → watchdog gives up and logs an error |
| Success reset | A successful restart resets the backoff and retry counter |
| Scope | **mcp** (respawns wrapper + reconnects external servers), **memdb**, **wechat** (restores poll loop), **memfiles**, **a2a**, **media** |

Auto-discovered third-party plugins get the same watchdog: `_spawn_plugin_generic` creates a `PluginLifecycle` for any plugin not in the built-in set, so a crash restarts it with the same backoff as the built-ins.

Subagents do **not** have their own watchdog — they connect to the main agent's plugin processes via HTTP, so a subagent crash only kills the subagent, not the shared infrastructure.

Processes communicate through environment variables:

| Variable | Purpose |
|----------|---------|
| `SLIFE_SESSION_ID` / `SLIFE_AGENT_NAME` | Log correlation, agent identity |
| `SLIFE_DATA_DIR` / `SLIFE_CONFIG_DIR` | Directory overrides |
| `SLIFE_{NAME}_PORT` | Published port of each plugin (MCP / MEMDB / WECHAT / MEMFILES / MQTT / MEDIA). Key is the uppercased plugin name with dashes normalised to underscores (`local-embed` → `SLIFE_LOCAL_EMBED_PORT`) — via `slife.agent.plugins.plugin_port_env`. |
| `SLIFE_SHAREFILE_URL` | Public ngrok URL (set inside the sharefile plugin process) |

### Built-in Plugins

| Plugin | Transport | Role |
|--------|-----------|------|
| **slife-mcp** | Streamable HTTP | Gateway for external MCP servers (stdio / SSE / Streamable HTTP). Manages connection lifecycle — spawn/connect, route tool calls, persist config — and keeps a persistent **tool catalog** (`mcp-plugin.db`) of every loaded tool (name, description, enabled), searched via `mcp_tool_search` and loaded on demand via `mcp_tool_load` (per-server `auto_load` restores wholesale registration). |
| **local-embed** | Streamable HTTP + `/v1/embeddings` | External embedding service provider — OpenAI-compatible `POST /v1/embeddings` + `GET /v1/models` (+ `/v1/models/{id}`) on the same port, from one local GGUF/transformer model loaded once and shared by memdb, memfiles, and the mcp tool catalog. Sole MCP tool is the internal `__embed_status`. |
| **slife-memdb** | Streamable HTTP | Turns database (backing table `diary`). Hybrid search (FTS5 + vec0 vector). Turn persistence, session restore, embedding configuration. |
| **slife-wechat** | Streamable HTTP | Bidirectional WeChat messaging via iLink ClawBot. Long-poll loop for incoming messages, typing indicators, dispatch for replies. |
| **slife-memfiles** | Streamable HTTP | Private notes/diary/files cabinet — tools (`note_save`, `diary_write`, `file_save`, `url_save`, `note_list`, `diary_list`, `note_read`, `diary_read`, `list_files`, `cabinet_search`, `cabinet_read`, `memfiles_semantic_status`, plus the scheduled-task and report tools `scheduled_task_set`/`remove`/`list`, `scheduled_run_list`/`skip`, `save_cron_report`, `report_list`/`read`), internal `__cabinet_status`. Notes & diary dual-written to markdown + a SQLite index (`{agent}.files/.index.db`, FTS5 + vec0) that reuses memdb's `SemanticManager` and RRF `merge_hybrid`. |
| **slife-sharefile** | Streamable HTTP + `/share` route | Public file sharing — sole LLM-visible tool `share_file`; internal tools `__tunnel_status`, `__register_file`; `GET /share/{token}` serves file bytes on the same port (one port, two protocols). Owns the ngrok tunnel (eager start, non-blocking) and the in-process token registry. |
| **slife-a2a** | Streamable HTTP | A2A mesh over the MQTT binding (paho-mqtt v5, LWT). Only starts when the broker is reachable (TCP probe). Hosts the LLM-visible `a2a_*` tools; only the drain/dispatch internal tools (`__a2a_*`) stay `__`-prefixed. |
| **slife-media** | Streamable HTTP | Non-chat AI generation (image, video, TTS, ASR) from any provider. Owns the `media:` config section (plugin-read, ignored by the main `Config` parser) and a provider-agnostic adapter layer (`dashscope-aigc`, `openai-images`). Tools: `generate_image`, `generate_video`, `text_to_speech`, `transcribe_audio` . Long renders use the harness's universal `_async: true` + `check_async`. Artifacts are saved to the working directory (or a `folder` passed to the tool) — work products, never memfiles cabinet files. |

### slife-mcp — External MCP Gateway

Three wire transports, one raw JSON-RPC connection class (`MCPServerConnection` — deliberately no `ClientSession`/anyio TaskGroups to avoid event-loop conflicts with FastMCP):

| Transport | Mechanism | Use |
|-----------|-----------|-----|
| **stdio** | Spawn subprocess, JSON-RPC over pipes | Local MCP servers (npx/uvx/bunx) |
| **http (SSE)** | GET with `Accept: text/event-stream`, POST to message endpoint | Remote SSE endpoints (tried first for URLs) |
| **http (streamable)** | POST JSON-RPC + `mcp-session-id` header; single-JSON **or SSE-streamed** responses | Remote Streamable HTTP endpoints (fallback) |

For `url`-configured servers the gateway probes with `GET + Accept: text/event-stream`: a `text/event-stream` reply switches to **SSE** mode (the `endpoint` event yields the POST message URL); otherwise the same client falls through to **Streamable HTTP**. A Streamable response may be a single JSON body or an SSE stream — both are parsed (the first matching JSON-RPC message; later events are server-initiated notifications and are dropped).

Exposed management tools (LLM-visible as `mcp_set`, `mcp_set_enabled`, `mcp_remove`, `mcp_list`, `mcp_list_tools`). Live status is reported by `check_mcp` via the internal `__mcp_connection_status`. The tool-call bridge `__mcp_call_tool` is an internal tool — LLM-invisible, invoked only by the `server__tool` proxies.

`mcp_list` is a static config view — the configured servers (name, transport, command/args or url, enabled/disabled, description), with no live state and no secrets (env/headers/auth omitted). `check_mcp` (a standalone tool, also run by `system_health`) calls the internal `__mcp_connection_status` for the raw live server state and adds health levels (ok/warning/info) with remediation hints. The separation keeps "what is configured" distinct from "what is connected", so the LLM picks the right tool.

**Tool catalog & on-demand loading.** The gateway persists every loaded
external tool into a tool catalog (`mcp-plugin.db`, next to its config): one
row per `{server}__{tool}` with name, description, and a per-tool `enabled`
flag derived from its server's state. The catalog survives restarts and is
indexed twice — FTS5 (keyword) and f32-BLOB vectors (semantic) produced
against the gateway's own `embeddings` section of `mcp-plugin.json5`
(`mcp_embeddings_set` / `mcp_embeddings_remove` manage it; `mcp_semantic_status`
reports readiness). `mcp_tool_search` runs hybrid/keyword/grep retrieval over
the catalog, degrading to keyword-only when no embedding endpoint is
configured or the semantic index is still building.

External tools are **loaded on demand**: the host registers none of them by
default. The model discovers a tool with `mcp_tool_search` and loads it into
its toolset with the native `mcp_tool_load(full_name)`, which fetches the live
schema + enabled state via the internal `__mcp_get_tool` and refuses a
disabled tool. A server with `auto_load: true` keeps the older wholesale
registration — its tools are registered whenever it connects. Enable/disable
is always **server-granular** (`mcp_set_enabled`): disabling a server
disconnects it, marks its catalog tools disabled (refused at call time), and
drops its loaded proxies; there is no per-tool toggle. Every
`tools/list_changed` notification runs a host-side reconcile
(`_sync_mcp_proxies`) that validates each loaded on-demand proxy via
`__mcp_get_tool` and unregisters any whose tool vanished, server disconnected,
or was disabled.

`mcp-plugin build` rebuilds the catalog and both indexes from live
connections — connecting every configured server (disabled ones included,
their tools then marked disabled), re-syncing the tool rows, rebuilding FTS,
and re-embedding the whole catalog against the configured endpoint.

Server lifecycle:

```
disabled ──[mcp_set_enabled(name, enabled=true)]──→ enabled (connected, catalog tools enabled)
enabled  ──[mcp_set_enabled(name, enabled=false)]─→ disabled (disconnected, catalog tools disabled)
enabled  ──[mcp_set(changed config)]───────────────→ restarted with new settings
```

All state changes persist to `mcp-plugin.json5` (self-hosted by the gateway).
Servers needing OAuth use a device-code flow (see below); tokens are stored in
the credential store via credstore (`mcp_oauth_*`).

### Subagent MCP Tool Discovery

Subagents inherit `SLIFE_MCP_PORT` from the parent environment, connect to the existing MCP wrapper over Streamable HTTP, and share the same on-demand model: they register the wrapper's management + tool-catalog tools, and load an external tool with `mcp_tool_load` exactly like the main agent. A `tools/list_changed` notification runs the same reconcile (`_sync_mcp_proxies`) that validates loaded proxies. This keeps tool naming consistent between main agent and subagents (`server__tool` in both).

## Memory (MemDB)

> **Terminology.** This store is the **Turns DB** (Glossary § Part II): the memdb
> plugin is its implementation, whose backing SQLite table is named `diary` — a
> codebase alias. It is distinct from the File Cabinet's **Diary** records
> (Glossary § Part II), which the memfiles plugin writes. In this document
> "diary" always means that table unless it names a cabinet file.

Every turn permanently recorded as an independent row — no session concept, a continuous time-ordered log in `~/.slife/<agent>.db`.

**Memory is core — the agent never runs silently without it.** A fatal turn-save failure (the memdb plugin returns `{"error": …}` for a broken schema / corruption / disk error) is a hard stop, not a skip: `save_to_memory` sets the memory-broken state, **freezes the inbox** (queued turns are dropped, never run — a turn that can't be persisted isn't worth running), and the TUI shows a **persistent red banner** with the reason until the DB is fixed and the agent restarted. Transient MCP timeouts are only warned, not fatal. Restore-side failure is also fatal (startup abort — see [Session Restore](#session-restore)).

### Schema

`diary` table:

| Column | Purpose |
|--------|---------|
| `user_message` | What the user said |
| `messages` | Assistant response as OpenAI JSON array (thinking, tool calls, results, text) |
| `images` | User image attachments as a JSON array (local paths / https URLs) |
| `summary` | 1–2 sentence gist (LLM-written) |
| `tags` | Comma-separated topic tags |
| `created_at` | ISO 8601 with timezone (B-tree indexed) — user input time (Enter-press moment, threaded from the TUI) |
| `completed_at` | ISO 8601 — assistant completion time (captured after the final turn ensure, before the MCP save) |
| `channel` | Channel identity: `human`, `wechat`, `subagent`, `heartbeat`, `system`, or the A2A peer name (the peer name doubles as the identity so full-text search still matches it) |
| `who_helped` / `what_model` | Agent identity + model used |
| `token_count` | Cumulative billed tokens for this turn |
| `prompt_tokens` | Context size at the last API call (restore primes the footer / `_sys_note` with it) |

Supporting structures: `diary_fts` (FTS5 content-sync table over message/summary/tags/channel with insert/update/delete triggers — the update trigger keeps `turn_summarize`'s summary/tags visible to keyword search), `diary_semantic` (sqlite-vec `vec0` table: embedding + rowid + chunk index + summary/tags/created_at), `diary_meta` (key-value store tracking the embedding model identity for migration detection), and `turn_channel` (a sibling row per turn holding the channel's JSON payload — A2A peer name, subagent name/task, … — written atomically with the diary insert under the same lock/commit, and created on existing databases by `CREATE IF NOT EXISTS` with no migration).

Turns are saved **unconditionally** after every turn (cancel, error, or max-iterations) via the internal `__memory_save_turn` tool. The save-side invariant is enforced by the harness: a turn with an orphaned `tool_call` is repaired (`_ensure_turn_consistent`) before it reaches the plugin, so the diary never persists an incomplete pair.

`completed_at` is written for every new turn; fresh databases get the column from `schema.sql`. The `images` column (user image attachments), the `prompt_tokens` column (context size at the last call), and the `turn_channel` table likewise live in `schema.sql` for fresh databases. There is **no migration layer**: databases that predate any column are out of scope (backward compatibility is not supported — reset the DB), and `restore` still tolerates a missing `prompt_tokens` value by falling back to the token estimate.

Per-turn token consumption is queryable via **`turn_token_usage`** (`rowid`, `since`/`until`, `limit`) — returns each matching turn's `token_count` (billing) and `prompt_tokens` (context size) plus totals/averages.

### Search

Three indexes: FTS5 (BM25 keyword), sqlite-vec `vec0` (cosine KNN), B-tree on `created_at` (time range).

| Mode | Best for |
|------|----------|
| `grep` | Exact strings — error messages, file paths, code |
| `fts5` | Topic / keyword search with ranked snippets |
| `hybrid` | Semantic recall (FTS5 + vec0 → RRF merge) |
| `time` | Browse by date |

Hybrid mode uses Reciprocal Rank Fusion (RRF, k=60). Without an embedding backend, hybrid degrades to FTS5-only gracefully.

Search hardening: `search_grep` escapes `%`/`_` with `ESCAPE '\'`; `turn_count` honors `since`/`until` in fts5 mode; LLM-facing search clamps `limit` to `[1, 200]`; the index drainer counts only stored embeddings so it gives up on a persistently failing embedder instead of spinning. Semantic search is gated on index completeness (`SemanticManager.semantic_ready`) — hybrid degrades to FTS5 while any turn lacks an embedding. `turn_search` is a pure read of the gate (no reindex side effect); the drainer converges the index on its own and the gate re-opens when `count_unembedded() == 0`. Known remainder: the `turn_count` grep branch still emits `LIKE ?` without the `ESCAPE` clause.

### Embedding

Embeddings are a **first-class top-level `embeddings` section** in `slife.json5`,
shared by memdb + memfiles and managed by the native tools
`embeddings_model_list` / `embeddings_model_set` / `embeddings_model_switch` /
`embeddings_model_remove` / `embeddings_probe` /
`embeddings_enable` (category `embeddings`).  The shape mirrors the LLM
`models.providers` two-level hierarchy:

- **provider** = one OpenAI-compatible endpoint (`base_url` + `api_key`), with
  an optional `models` list (`{model, dim?}`).
- **`active_model`** (`"provider/model"` or bare `"provider"`) is
  configuration-authoritative: slife embeds against that provider and POSTs
  the configured model id.  Many endpoints (e.g. OpenAI official) have no
  `active` flag on `/v1/models`, so the endpoint's active model is only a
  fallback when no model is configured.
- **dimension**: configured `dim` → matched from the endpoint's `/v1/models`
  → probe embed (`_probe_api_dim`).  The embedder (`EmbeddingClient`,
  `embeddings.py`) exposes `available` / `loaded` / `dimension` /
  `max_tokens`; every embed is serialised on a per-client `threading.Lock`
  (`_embed_lock`), and local-model backends (gguf/transformer, served via
  the local-embed plugin) run on daemon threads (`slife.threads.run_daemon`).

**Vector store.** `diary_semantic` is a sqlite-vec `vec0` table (`turn_embedding float[dim]`, `+diary_rowid`, `+chunk_index`, `+summary`, `+tags`, `+created_at`). One turn → multiple chunks: text is split at paragraph boundaries (~2000 chars ≈ 500 tokens, 1-paragraph overlap), and the embedded text is the user message plus all assistant/tool contents. Semantic search dedupes by `diary_rowid`, keeping only the best (lowest-distance) chunk per turn.

**Write path is insert-only.** `save_turn` persists the row and never embeds on the save path (a slow GGUF embed of a large turn previously tripped the harness's 10s save timeout — a false alarm; the row was saved anyway). Embedding is an internal plugin concern: after each insert `__memory_save_turn` calls `manager.on_saved()` — a non-blocking `event.set()` that wakes the idle drainer, so the turn becomes semantically searchable shortly after save once the gate re-opens. `turn_summarize` writes only the `summary`/`tags` columns — a recall clue for keyword (FTS5) search — and never touches the semantic index, which keeps the turn's full-text vectors intact. A passed `rowid` annotates that specific turn; **omitting it captures the current (in-flight) turn** — the tool returns "captured" without writing, and `save_to_memory` extracts the call and rides the summary/tags onto the new row at save (`_extract_turn_annotation`), so the model can annotate the turn it is completing mid-loop with no `latest_rowid()` race.

**SemanticManager — the lifecycle actor.** `SemanticManager` (`semantic.py`) owns the binary gate, the embedder instance, and an event-driven index drainer as one object — the only place the gate is written. It is document-generic (a store contract: `count_unembedded` / `get_unembedded_docs` / `replace_embedding_chunks` / `reconfigure_for_embedding`), so memdb's `SessionStore`, memfiles' `MemfilesStore`, and mcp-plugin's `ToolStore` each drive their own instance — the three gates are independent. mcp-plugin's tool catalog is a lighter variant of the same pattern: one tool = one embedding of its name + description (no chunking), vectors stored as f32 BLOBs and matched by brute-force cosine in Python (the corpus is small, so no sqlite-vec dependency), and a model change drops the stored vectors before re-embedding. The gate (`semantic_ready`) opens exactly when `embedder_ready ∧ count_unembedded() == 0`; there are no intermediate states. `enable(cfg)` / `disable()` are blocking config transitions (load model, migrate vec0 in place, start/stop the drainer); `on_saved()` is a non-blocking `event.set()` wake. The drainer loops: `count_unembedded() == 0` → gate ON, wait on the `asyncio.Event` (no polling); else → gate OFF, embed one batch (atomic `replace_embedding_chunks`). A persistently failing embedder is bounded by a no-progress limit → state `stalled`, and only `enable()` (a config change) resets it. The state machine (`disabled | loading | indexing | ready | stalled`) and a human `reason` are reported separately from the binary gate — `memdb_semantic_status` surfaces both. While the gate is OFF, hybrid degrades to FTS5-only with a hint naming the reason — partial semantic results are never served. The embedder is owned in-process (no cross-module `reload_embedder` global mutation), so the `python -m` double-module hazard that once left the gate stuck is structurally impossible.

**Search.** `turn_search` has four modes (`grep` / `fts5` / `hybrid` / `time`). `hybrid` runs the FTS5 keyword query and a vec0 KNN side by side, then merges via Reciprocal Rank Fusion (k=60, `search.py`). sqlite-vec forbids any auxiliary-column constraint or JOIN inside a KNN query, so the KNN runs alone, time-window filtering happens in Python (with a wider fetch pool), and `user_message` is fetched in a second query.

**Model / dimension change.** The `embeddings_*` native tools persist the top-level `embeddings` section and then hot-reload: they call the internal `__memory_reload_semantic` / `__memfiles_reload_semantic` tools, which `await manager.enable()` — stopping the drainer, migrating the vec0 table in place (`reconfigure_for_embedding` — a width/model change drops and recreates the table since old vectors live in a different vector space), and restarting the drainer to rebuild. `embeddings_enable(false)` calls `manager.disable()` instead. A failed reload degrades to "takes effect on restart" (never blocks the persist). The same path catches a model/dimension change made by a manual `slife.json5` edit before the gate re-enables.

### Session Restore

On startup, recent turns are read **directly from SQLite** — no MCP transport, no plugin dependency. The UI rebuilds the last session immediately (user messages, assistant text, tool-call widgets, images whose files still exist); plugins start in parallel. Restored messages carry their stored timestamps — user messages read `created_at`, assistant messages read `completed_at` — matching the live display.

**Turn headers on restore.** Each restored user message gets a compact `[INFO: {"turn_id": N, "begin": …, "end": …}]` footnote (rowid + begin → end) concatenated into the message text — without it the whole restored history would read as "just happened". The LLM can also use N with `turn_read` / `turn_summarize`, and the human reads the same line in the TUI. Restored turns get it from persisted columns at restore; a just-completed live turn gets the same footnote appended by `save_to_memory` once `__memory_save_turn` returns its rowid — so the model can reference the previous turn precisely, and `turn_summarize`'s latest-rowid default stops being racy. **The footnote is runtime-only and never persisted:** the stored `user_message` / `messages` are written *before* the append, restore regenerates it from columns, so the DB carries the clean original in both paths. The current in-flight turn carries none (it is the one that IS now), and live TUI bubbles are not retro-updated — a missing footnote is itself the "current session" signal. Heartbeat turns are excluded — their user message is the synthetic `[Heartbeat]` trigger, not a real query. The two machine annotations — the turn footnote and the trim note — share one `[INFO: …]` envelope; an unreadable image attachment now raises a `ValueError` from `add_user_message` instead of being silently dropped — upstream (`@path` parsing, `attach_image`) validates files first, so a failure here is a bug signal, not a hidden marker. Heartbeat stays its own sentinel: it is a stored turn identity (old diary rows start with it), so renaming it would misclassify every stored heartbeat on the next restore.

Restore rebuilds the **exit-time context verbatim**. The `diary_meta.context_start` row (an exclusive rowid) marks the live-context boundary: the internal trim advances it past every turn it evicts (`advance_context_start`), `clear_context` flushes it to the latest row (`set_context_start_latest`), and `get_recent_turns` reads it directly from SQLite. Turns after the boundary are returned **verbatim — no ceiling re-slicing**: the boundary already encodes the trimmed state, so restore replays the exact slice that was live at exit, and the agent picks up where it left off. `get_recent_turns` returns `(turns, skipped=0, budget=0)` — skipped/budget are kept only for call-site compatibility; the only cap is a defensive 2×-ceiling guard against a stale `0` boundary from a not-yet-trimmed DB (normal operation never reaches it). The just-restored history is exempt from the first-turn trim (`_just_restored_history`), so nothing is compacted before the user's first exchange. Older turns stay in the diary, searchable via `turn_search`.

The restored context footer is primed with the **latest restored turn's persisted `prompt_tokens`** — the exact context size at exit (what `_sys_note` would have reported) — so the first `_sys_note` / status bar shows the real occupancy instead of an estimate. A missing/zero value (e.g. a cancelled turn that never made an API call) falls back to the token estimate. The `prompt_tokens` column ships in `schema.sql`; no migration for older databases.

**Restore failure is fatal, never silent.** A present-but-broken memory DB (missing column, corruption, disk error) makes `get_recent_turns` raise `MemoryDatabaseError` instead of returning `[]` — the TUI shows the error and **aborts startup**. The agent must not begin a memory-less session as if nothing happened. memdb and memfiles are likewise **required plugins** (declared via `plugins.required` in `slife.json5`, an empty list by default): a required plugin that fails to *load* (its process never becomes ready — including a bounded 30 s timeout on a hung spawn, or an embedding-model load that stalls the lifespan) likewise aborts startup with a red message, stops all plugins, and exits — never silently limping on without a core component.

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

Only MQTT is implemented.  A `transport` other than `"mqtt"` in the `a2a` config section disables A2A with a warning at config load instead of crashing startup.

The LLM-facing `a2a_*` tools live in the a2a plugin (one uniform prefix; the MCP proxy keeps the exact names).  Subagents are **not** part of A2A — they are local workers (see "Subagent Workers" below).

### MQTT Mesh

- Topics: `Slife/<agent_name>/presence`, `Slife/<agent_name>/tasks/inbox`, `Slife/<agent_name>/tasks/result`
- Presence heartbeat every 15 s (configurable); peers silent for 45 s are pruned. LWT publishes `{"status":"offline"}` (QoS 1) so crashes are visible
- Client id is `<agent_name>-<pid>` to allow multiple processes per agent id
- Duplicate agent detection: after subscribing, the client listens 1.5 s for an existing presence with the same id and exits with a clear error rather than splitting the identity
- Slife only **probes** the broker (TCP connect) — Mosquitto is started by the user; if the probe fails, the a2a plugin is not started (A2A disabled) and this is reported via `system_health`
- The mesh connects **eagerly** when the plugin starts (lifespan hook) so presence is announced at launch; a failed eager connect is tolerated and mesh tools attempt a lazy connect on demand
- Peer presence **transitions** (online/offline/timeout) reach the LLM context: the a2a plugin queues them; `AgentService._a2a_poll_loop` drains them and appends the TUI-identical line (via `format_presence_line`, which also filters heartbeat-driven `status_change`) to a buffer that `AgentLoop` drains read-once into the `_sys_note` footer each turn. The footer carries only *changes* — the current roster stays queryable via `a2a_list_agents`, so a missed event never leaves the LLM with stale state
- Async task results **auto-push by default**: a peer's result arrives on `Slife/<agent_name>/tasks/result` over MQTT → the client fires `on_task_result` → the plugin queues a completion → `_a2a_poll_loop` drains it into the history ("Peer X completed async task (ID: …): …"). The agent sends async and the result simply arrives — no polling or blocking (MQTT subscription is implicit, so the HTTP/SSE-style `a2a_subscribe_task` was dropped). `a2a_send_task_async` takes `mode="auto"` (default — auto-push) or `mode="poll"` (no push; retrieve via `a2a_get_task_result`), mirroring the subagent delivery mode so a caller that polls a result in-turn isn't also handed the same result as a new turn.

### Unified Inbox

All messages flow through a single `asyncio.Queue`:

```
Human keyboard ──→ Inbox.post() ──→ Queue ──→ Inbox.run() ──→ AgentLoop
MQTT tasks     ──→ Inbox.post() ──→
WeChat messages──→ Inbox.post() ──→
Subagent results─→ Inbox.post() ──→
```

Messages are processed sequentially — only one AgentLoop runs at a time. Human and WeChat sources keep persistent histories; remote agents get fresh one-shot histories. Status flips to `busy`/`idle` around each turn; `on_turn_complete` fires unconditionally (in `finally`), so memory persistence survives cancellation.

### Task Store

Mesh tasks are tracked in memory (`TaskRecord`: id, agent, preview, status, transport, timings, result capped at 2000 chars; 500-record soft cap). The store is **not persisted across restarts** — `a2a_list_tasks` after restart is empty by design. Worker (subagent) tasks are **not** in this store — they live in per-worker local records (`SubagentProcess._task_records`).

### Subagent Workers

Local child-process workers, always available — no config toggle.  A subagent (agent worker) is **not** an A2A peer: no network identity, no presence, and no mesh tooling of its own — when it reaches the mesh it sends as the parent via the shared a2a plugin.

- **headless.py**: Slife without TUI, worker-scoped JSON-RPC 2.0 over stdin/stdout — methods `worker/send`, `shutdown`; notifications `worker/complete`, `worker/progress`; a `{ready: true}` result signals startup
- **SubagentManager**: spawn/stop/list lifecycle; auto-names `sub-1`, `sub-2`, …; `max_subagents` default 5, `task_timeout` default 120 s
- **Serial processing + visibility**: a worker runs one task at a time. `SubagentProcess` tracks the in-flight count (`is_busy` / `queued`); a sync `subagent_send_task` to a busy worker is automatically queued as async and reported (never a silent timeout, never a resend). `subagent_list_tasks` lists worker tasks across workers.
- **Async delivery mode**: `subagent_send_task_async` takes `mode="auto"` (default — the result is auto-pushed to the parent's inbox when the worker completes, starting a new turn) or `mode="poll"` (no push — retrieve via `subagent_get_task_result`). The mode is chosen at send time so the caller's intent is explicit: `poll` for a result to be integrated into the current turn, `auto` for fire-and-forget notification. This removes the redundant double-delivery (poll *and* push) of a single-mode design; `auto` (the default) still guarantees delivery.
- **Shared plugins**: subagents connect to the main agent's plugin servers (MCP / memdb / wechat / a2a / memfiles) via inherited ports — no isolation; they can send but never drain the inbound queue (all replies and management belong to the main agent)
- **Recursion**: subagents can spawn their own descendants (each level has its own SubagentManager + watchdogs)

## Image & Memfiles

### Image Input

User attaches images with `@` directives — **one `@` = one image source**, any number per input, parsed independently. The user message stays verbatim (the `@` reference remains visible like any text); the extracted sources are handed to the loop, which **auto-invokes** the `attach_image` tool once with the whole `sources` list via the harness-call machinery (`_auto_invoke`, same as `_sys_note`) — a single history shape (one assistant(tool_use) + tool-result pair), no LLM iteration spent deciding to attach.

#### `@` syntax

Each `@` is followed by exactly one source, in any of these shapes:

| Shape | Example |
|---|---|
| Bare path | `@D:\photos\a.png` |
| URL | `@https://example.com/x.png` (no extension / query / fragment OK) |
| Data URI | `@data:image/png;base64,AAAA` |
| Quoted (spaces OK) | `@"D:\my photo.png"` / `@'a.jpg'` |
| Bracketed | `@[D:\a.png]` / `@{a.png}` / `@(a.png)` |

Multiple `@` may sit **adjacent without spaces** — `@a.png@b.png`, `@a.png @b.png`, and `@https://a.com/x.png和@http://b.com/y.png` each yield two sources. Quoted/bracketed forms read the inner content (spaces allowed); bare tokens run to whitespace, a quote, or the next `@`.

**Shape gating.** A bare path must end in an image extension (`.png .jpg .jpeg .gif .webp .bmp .svg .ico .avif .tiff .heic`), so `@someone` (no extension) is skipped as plain text. **URLs and data URIs are self-identifying via their scheme** (`http://` / `https://` / `data:`) — they need no extension gate, so `@https://example.com/photo`, query strings (`?v=2`), and fragments (`#x`) are all valid. **No filesystem check here** — whether a source actually exists / is reachable is validated downstream by `attach_image` (which reads the file or returns an error). Nothing is rendered in-terminal.

#### Boundary characters (token-end)

The extract regex ends a token at these characters — deliberate, so adjacent `@` directives split cleanly and CJK text isn't swallowed:

- **Whitespace, quote (`"` / `'`), `@`** — universal boundaries (the next directive).
- **CJK characters** — a natural word boundary when typing `@a.png和@b.png` or `@https://a.com/x.png和@http://b.com/y.png`, so both directives parse.
- **Comma — URLs only** — `@url,@url` separates; a URL with a comma in its query string is truncated at it (rare; percent-encode instead). **Data URIs keep commas** (base64 payload is `,`-heavy).

**CJK inside a URL.** A bare URL must not contain raw CJK — `@https://example.com/photo?v=我` truncates at the CJK. Two correct ways to attach a URL whose query carries non-ASCII:

- **Percent-encode** the value: `@https://example.com/photo?v=%E6%88%91`
- **Wrap the whole URL in quotes**: `@"https://example.com/photo?v=我的照片"` (the quoted branch reads everything inside, CJK included)

#### Parsing: two-phase regex

`slife/ui/app.py` parses with a two-phase regex — **locate then extract** — deliberately *not* a single-line grammar:

1. **Locate** — `_AT_RE = re.compile("@")` finds every `@`.
2. **Extract** — `_SOURCE_RE` matches a source pattern on the slice after each `@` (data URI / URL / quoted / bracketed / bare-with-extension).

The boundary set is shared via `_TOKEN_END` (whitespace / quote / `@` / CJK) with `_URL_END` adding comma for URLs only. This locates-then-slices structure is more robust than one regex against special characters (spaces, CJK, commas) that would otherwise corrupt a single `\S+` token, and keeps the existence check in one place downstream. A non-matching `@` (e.g. `@someone`, an unclosed quote) is skipped whole — the text stays, nothing is attached.

#### Pipeline

The loop passes the parsed `sources` list to `attach_image` in a single call. `include_image_urls()` (in `slife/agent/multimodal.py`) turns each source into a vision content block (HTTP(S) URLs pass through, local files base64 as `data:` URIs), returning `(blocks, failed)` — the valid blocks are injected into the in-memory user message in one shot, the failures are reported in the tool result. Exact duplicate sources are deduped (order-preserving) so a repeated `@a.png` attaches once. Blocks are **live-session-only**: injected into the in-memory user message, never persisted, restore is text-only. Each backend converts blocks to its wire format (Anthropic `image.source`, Responses `input_image`).

### Image Display

Images are never rendered in the terminal — the Sixel / Half-cell render stack was removed, and the multimodal content blocks a user message carries feed the LLM context only: the TUI renders just the text parts of a message. To surface a file to the user, the agent hands back a path or URL (clickable in the chat, opened with the OS default app) or publishes a public HTTPS link via `share_file`.

### Memfiles — Notes / Diary / Files Cabinet (Private)

A standard Streamable HTTP plugin (`slife/plugins/memfiles/server.py`) — self-contained and replaceable exactly like memdb / media.  The harness is a thin MCP client: it spawns the plugin, registers the memfiles tools, and never touches file-serving state directly.  Public sharing lives in a separate plugin (`sharefile`) — memfiles is the **private cabinet only**.

Three typed knowledge stores, each **dual-written** to a human-browsable markdown file and a SQLite index (`{agent}.files/.index.db`):
- `note_save(subject, …)` — a note keyed by **subject**, appended to `notes/<subject>.md` (each call adds a timestamped section);
- `diary_write(date, …)` — a day's entry keyed by **date**, appended to `diary/<YYYY-MM-DD>.md`;
- `file_save` / `url_save` — saved attachments under `files/<category>/` (bytes stay on the filesystem), auto-filed by extension (images / documents / archives / code / audio / video / data / other) with an optional `category` override; an LLM `summary` given at save time makes them semantically searchable (one pass — no separate summarize tool).

Each kind owns its FTS5 + vec0 tables (`notes_*` / `diary_*` / `files_*`). `cabinet_search(query, kind, mode)` runs hybrid (FTS5 + vec0 KNN, RRF via the shared `merge_hybrid`) or keyword search across them; `cabinet_read(path)` re-opens a file with a path-traversal guard. Browsing by key: `note_list` / `diary_list` list entries (newest first, diary optionally by date range) and `note_read(subject)` / `diary_read(date)` return full content. The index mirrors memdb's design and **reuses its code**: the shared `SemanticManager` (document-source contract) drives the drainer over all three kinds, and `memfiles_semantic_status` reports this index's own gate — independent from `memdb_semantic_status`, because each plugin reindexes its own DB (one shared top-level `embeddings` config, independent availability).

All save tools return the saved **local path** (clickable) — they never auto-publish, so nothing is registered in any token registry as a side effect of saving.  Publishing is always the LLM's explicit choice, via the separate sharefile plugin's `share_file`.

### Sharefile — Public File Sharing

A standard Streamable HTTP plugin (`slife/plugins/sharefile/server.py`) — self-contained, discovered and spawned exactly like media.  Its **sole LLM-visible tool is `share_file`** (registered bare as `share_file`).  The plugin owns everything: the in-process token registry, the ngrok tunnel, and serving the file bytes on the **same port** via a custom HTTP route (one port, two protocols: `/mcp` for Streamable HTTP, `/share/{token}` for plain HTTP):

1. `share_file(path)` (MCP) → registers the file under a random 30-char hex token (`secrets.token_hex(15)`) → returns `https://xxx.ngrok-free.dev/share/<token>`.  When the tunnel is offline the tool returns a graceful error rather than being hidden.
2. `GET /share/{token}` streams the file in 64 KB chunks (403 unknown token, 404 file gone).

No BLOBs, no database, no HMAC — token→path mappings are an in-process dict (server and tunnel share one process, so no shared registry file).  `__tunnel_status` (harness health check) and `__register_file` are internal `__`-prefixed tools, never exposed to the LLM.  `attach_image` is **not** part of this plugin — it is a native vision helper (`slife/tools/models.py`) that injects image blocks into the main-process history.

`GET /share/{token}` streams the file with an RFC 5987 `Content-Disposition` — a non-ASCII filename (e.g. CJK) is emitted as an ASCII fallback in `filename=` plus the real name percent-encoded in `filename*=UTF-8''`, because HTTP header values must be Latin-1 (a raw CJK filename otherwise raises `UnicodeEncodeError` → HTTP 500).

### Ngrok Tunnel

Started **by the sharefile plugin** (eagerly in its lifespan, non-blocking; graceful failure) via the official ngrok Python SDK (embedded agent — no external binary). Authtoken resolution: credstore `NGROK_AUTHTOKEN` → environment. Uses **endpoint pooling** (`pooling_enabled=True`) so multiple slife instances (WSL + Windows, sub-agents on different machines) share the same dev domain — ngrok load-balances across all online agents. Initial start retries up to 3 times with linear backoff (2/4 s); a background monitor performs one follow-up retry if the first start failed; share tools fall back to an on-demand start.

A tunnel failure is **temporary, not permanent** — free-tier sessions get recycled and another instance may free the tunnel at any time.  So the sharefile plugin **always loads** (even if the eager tunnel start fails): the monitor keeps retrying in the background, and the tunnel comes up automatically the moment it's available.  The harness surfaces a one-time warning only on a terminal `failed` state.

ngrok free tier limits: **1 online agent** (one tunnel per token — only the first agent to start gets the sharefile tunnel; subsequent agents fail to bind), 1 GB transfer/month, 20k HTTP requests/month. Endpoint pooling requires no paid plan. Subagents reuse the main agent's sharefile plugin via Streamable HTTP (`SLIFE_SHAREFILE_PORT`) instead of spawning a second tunnel.

## Scheduled Tasks

Recurring tasks the agent runs on a cron schedule. The design separates three concerns: **timing** (a thin main-process loop), **execution** (a subagent worker), and **record** (the memfiles DB). The agent both starts and finishes each task visibly — it dispatches the work and later sees the completion — rather than the scheduler silently running it.

**Timing — `schedule_loop` (`slife/agent/schedules.py`).** Main-agent-only (subagents are workers, never the scheduler), started alongside the heartbeat in `start_inbox`. Every 30 s it recomputes each enabled task's next fire **from the DB, not from memory**: the anchor is the newest `due_at` across *all* of a task's runs (any recorded run advances the anchor, so a fire is never re-detected), falling back to `created_at`. Cron parsing uses `croniter` (`slife/schedules.py` is a thin wrapper: validation, next-run, timezone policy). The loop **fires only**: compared against a short grace window (120 s), a fire due within it is fired; anything older means slife was down. It carries no missed branch at all — the poll cadence runs well inside the grace window, so while the process is up a fire can never be missed; runs a previous lifetime left undone are the startup sweep's concern (see *Failed runs* below).

**Trigger → execution.** The loop injects a `[Schedule <name>]` message into the unified inbox under the **system channel** (source `SYSTEM`, `channel=system`, silent handler like heartbeat — excluded from "remote" handling in `inbox.py`); the `[Schedule <name>]` text is the *schedule marker* riding that channel's content. The run itself is recorded when the agent dispatches, not at fire time, and a short in-memory pending-fire guard keeps the 30 s poll from re-firing while the agent is mid-turn. The agent handles the trigger by delegating, not doing: it calls `run_schedule_now` — the single dispatch tool, also used to backfill missed runs — which records a `scheduled_runs` row (`status='pending'` — success is unconfirmed until a report lands), spawns/reuses the subagent named after the task, and sends it a deterministic task text via `subagent_send_task_async` (mode=auto) that instructs the worker to call `save_cron_report` when done and to notify the user. Completion rides the existing subagent auto-push back to the main agent, which reports the task as finished (reworded to hide the subagent). Idle schedule workers are recycled by the loop once their task has no run still in flight.

**Record — three memfiles tables.** `scheduled_tasks` (definition: name, description, cron, timezone, enabled — stored in the DB so tasks are runtime-mutable, added in conversation), `scheduled_runs` (per-fire state + report link), and `reports` (a fourth cabinet document kind, `reports/<slug>.md`, with FTS5 + vec0 indexes via the `_KIND_SPECS` extension). `save_cron_report` writes the report and backfills the newest un-linked run's `report_id` at the store layer, so the run→report link closes without depending on a second LLM call. Fresh DBs get the tables from `schema.sql`; the runtime self-heals `reports_semantic`. Older databases are out of scope (backward compatibility is not supported).

**Run outcomes are optimistic-pending.** A fire is recorded `pending`; a report arriving flips it to `ran` (the *only* success writeback, at the store layer alongside the `report_id` backfill). Everything else is a failure by default: `run_schedule_now` marks the run failed immediately if the dispatch itself fails, and at startup the one-shot `schedule_startup_sweep` reaps any surviving `pending` runs to `failed` — a previous process lifetime can never complete them.

**Failed & missed runs — settled at startup, never announced.** Runs a previous process lifetime left unfinished are settled **once, at startup**, by the one-shot `schedule_startup_sweep` (a dedicated task started alongside `schedule_loop`, not part of the poll): every surviving `pending` run becomes `failed`, and fires that were due while slife was down are marked `missed`. It posts no message — the agent and user are not interrupted by a startup turn. Both surface via `scheduled_run_list` and can be backfilled (`run_schedule_now` fires a task immediately, recording a run) or closed (`scheduled_run_skip` — a *skipped* run is the user's "don't backfill", never a success marker). The sweep runs exactly once per process and never re-sweeps. Scheduled tasks fire **only while slife is running** — a fire due while closed is recorded `missed`, and the next scheduled fire after startup triggers normally.

Tools: `scheduled_task_set` / `scheduled_task_remove` / `scheduled_task_list`, `scheduled_run_list` / `scheduled_run_skip`, `save_cron_report` / `report_list` / `report_read` (all memfiles MCP tools, visible to main agent and worker alike), plus the native `run_schedule_now` (the single dispatch tool for both cron fires and backfills — it records the run and dispatches the worker directly, reached through the `fire_schedule_now` tool-context hook). `scheduled_task_set` validates that a task name is a safe identifier, because it doubles as the worker name.

## UI

Textual TUI with minimal chrome:

- **ChatView** — scrollable message container; printable keys redirect to the input
- **UserMessage** — dim `[HH:MM]` (user input time) + prefix-styled user text, optional image attachments
- **AssistantMessage** — dim `[HH:MM]` (assistant completion time) on the response text — **not** before the thinking block, so a thinking-only message shows no time — plus streaming text with collapsible thinking blocks (Enter/Space toggle)
- **ToolCallWidget** — collapsible amber headers: status icon, label, primary-arg preview, iteration counter; Ctrl+Y copies the result
- **StatusBar** — model name, thinking indicator, inbox state, last-call context tokens + usage % (per history, so a heartbeat turn never drags the human reading down)
- **ApprovalPrompt** — inline approve/deny row for `_approve: true` tool calls (Y / N / Esc), re-renders to ✓ Approved / ✗ Denied
- **Auto-restore** — rebuilds last session's UI from the diary on startup

Timestamps: user messages display `created_at` (the input-box Enter-press moment); assistant messages display `completed_at` (the turn's completion time). Both format as `HH:MM` same-day, `MM-DD HH:MM` same-year, `YYYY-MM-DD HH:MM` older. Live display and restore read the same stored values, so the rebuilt chat matches what was seen live. The status bar's token count is the **per-call** prompt tokens of the history's last API call — not the turn's cumulative sum (that sum is the assistant message footer).

All user-supplied text is rendered with `markup=False` to prevent `MarkupError` injection.

### Keyboard

The end-user keymap is documented in the [README](README.md#keyboard-shortcuts). Design notes for the model picker and approval bindings live under [Model Switching](#model-switching) and [Approval Gate](#approval-gate).

### Progressive Disclosure

Not all tools are in every request. Several categories use lightweight summaries:

| Category | Browse | Load |
|----------|--------|------|
| MemDB | `turn_search` | `turn_read` |
| Skills | `skill_list` | `skill_use` |
| MCP | `mcp_list` / `mcp_list_tools` | `mcp_set_enabled(name, enabled=True)` |

### i18n

`slife/ui/i18n.py` — the bilingual layer (English + Chinese). Every
user-facing string routes through `t(key, **fmt)`; the language is resolved
once at import from the OS locale and is overridable via `set_language()`
(tests use this to pin `en`). See [Language policy](#language-policy) for
what is localized and what stays English.

## Config & Credentials

### Two-Layer Architecture

```
┌──────────────────────────────────────────────┐
│  Credential store (credstore)                │
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

`${VAR:-default}` fallbacks supported; resolution is recursive over strings/lists/dicts. Known gap: `${VAR:-default}` resolves from the shell env only and never consults credstore, so the fallback beats a credstore-held key for that form.

### Credstore Backend Matrix

Backend selection is **deterministic by platform** — `_init_system()` dispatches `os.name`/`sys.platform`/`is_wsl()` to exactly five backends, no keyring priority auto-discovery:

| Platform | Backend | Mechanism |
|----------|---------|-----------|
| **WSL** | WslBackend | PowerShell bridge → advapi32.dll CredReadW/CredWriteW (C# P/Invoke) — shares the Windows CredMan store |
| **Windows** | WinVaultKeyring | Windows Credential Manager (Vault API, via keyring) |
| **macOS** (GUI) | macOS Keyring | Logon keychain via keyring ctypes shim |
| **macOS** (headless) | macOS Keyring + isolated keychain | `CREDSTORE_KEYCHAIN` or `~/.credstore/credentials.keychain-db`, auto-created via `security create-keychain` |
| **Linux** | KeyutilsBackend | Kernel persistent keyring via ctypes syscalls (zero deps) |

Anything else raises a clear "unsupported platform" error. On a supported platform whose backend is unavailable (e.g. Linux where keyctl is blocked by policy), `_init_system()` returns `None` instead of raising — credstore keeps working in **cryptfile-only** mode (`set` stores in the AES backup with a notice; `set-password`, `status`, `get -p`, `delete` all function). `credstore set` dual-writes: cryptfile first, then the system keyring (rolled back if the keyring write fails). The CLI also provides `set-password`, `status`, `get`, `delete`, `copy`, `list`, `reset-keyring`, `reset-backup`, `inject`/`uninject` (shell-aware export: bash / powershell / cmd; Windows persistence via `HKCU\Environment`).

**sLife does not support credstore's cryptfile mode, but is fully compatible with environment-variable setup.** The sLife Python API (config `_try_credstore_lookup`, `credential_check`/`credential_inject`, OAuth, sharefile tunnel) calls credstore's password-free `get_credential`/`exists_credential`/`resolve_uri`, which read the system keyring only and return `None` (never prompting) when it's absent. So on a keyring-less box (cryptfile-only), sLife's `${VAR}`/`keyring:` secret resolution is inert — it silently falls through to env vars or defaults. This is by design: the master password lives in the CLI (`credstore set-password` / `get -p`), not in sLife. Three supported usage methods:

1. **Env-var only** — export secrets in the shell; `os.environ` is checked before credstore in `_resolve_secret`, so env-based credentials are fully supported.
2. **Cryptfile + inject** — keep managing secrets in credstore cryptfile mode, then `credstore inject KEY…` pushes them into the environment (in cryptfile-only mode `inject` prompts once for the master password and reads from the encrypted backup). sLife then resolves them from `os.environ`.
3. **Plaintext in config** — a literal `api_key` in `slife.json5` works (tolerated), but the secret sits on disk; not recommended.

(A pre-0.9.7 credstore *would* prompt, because auto-discovery made the cryptfile the active system keyring — the deterministic dispatch in 0.9.7+ removed that path.)

### Secret Sanitization

**The input and output gates are the authoritative trust boundary.** A secret may appear as plaintext anywhere *inside* the process (tool internals, `conn.error`, log lines) — that is **not** a vulnerability by itself. Judge any finding by two questions:

1. **Does the secret reach the LLM context / history as plaintext?** → blocked by the gates below.
2. **Does the secret cross the machine trust boundary as plaintext?** — network egress, a file published publicly (`/share`), anything readable outside the running user. → this is a real security finding.

Everything else — including plaintext in `~/.slife/logs/` (readable only by the running user) — is hygiene, not a security issue. Do not report a plaintext string that the gates will mask, or a local log line, as a security finding.

Two gates, single pattern-masking engine (`logfmt.sanitize_secrets`):
1. **Inbound** — `MessageHistory.add_user_message()` on every external message
2. **Outbound** — `AgentLoop._execute_tools()` runs `sanitize_secrets` on **every** tool result before it enters the history (tool-call arguments are also masked at `MessageHistory.add_assistant_message`). So even a tool that returns a secret verbatim (e.g. a config/env lookup) never puts a known-shaped value into the LLM context.

Known API key shapes (`sk-*`, `ghp_*`, `ya29.*`, `pypi-*`), `Authorization: Bearer` tokens, and credential-named `key=value` pairs are masked with `<MASKED>`. The engine is pattern-based, so a value that matches no known shape is **not** masked at the gates either — the honest boundary is "known-shaped secrets never reach the LLM"; an exact-match denylist from credstore remains a possible hardening.

### Config Sections

`slife.json5` structure parsed by `Config.from_json5`:

| Section | Purpose |
|---------|---------|
| `env` | `${VAR}` references, applied to the environment at startup |
| `models.providers` | Provider configs (api_key, base_url, api, models[]) |
| `active_model` | Currently active model ref (`provider/model`) |
| `agent` | `max_iterations`, `tool_timeout`, `context_floor`, `context_ceiling`, `tool_result_ceiling`, `heartbeat_interval` |
| `tools` | Per-tool overrides (timeout, enabled) |
| `mcp-plugin.json5: servers` | External MCP server configs (self-hosted by the gateway) |
| `embeddings` | First-class embeddings config: `providers` (OpenAI-compatible endpoints), `active_model`, `enabled` — shared by memdb + memfiles |
| `wechat` | `enabled` toggle |
| `media` | Non-chat generation config (defaults, providers → api adapter + models) — plugin-read, ignored by the main `Config` parser |
| `a2a` | A2A config (transport binding, broker host/port, heartbeat, task_timeout) |
| `subagent` | `max_subagents`, `task_timeout` |
| `cli_tools` | External CLI tool definitions (read by the CLI tools directly) |
| `rest_apis` | REST API registrations (read by the REST API tools directly) |

## Health Checks

Health checks fall into two categories. `system_health` runs all of them together, and every dynamic check is also exposed as a standalone native tool (`check_memdb`, `check_wechat`, `check_memfiles`, `check_local_embed`, `check_sharefile`, `check_mcp`, `check_a2a`, `check_watchdog`) so the LLM can probe a single subsystem directly without the full report. `check_mcp` additionally takes an optional `server` argument (default: all) to diagnose just one external server.

Because the standalone checks are subsets of `system_health`, their tool schemas state that relationship explicitly ("one subsystem of system_health", and `system_health` states it includes every `check_*` result). This keeps the LLM from calling `system_health` and then re-calling each `check_*` for the same data — each schema describes what it returns and how it relates to the aggregate.

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
| `check_memfiles` | File cabinet (notes / diary / files) connected? semantic index ready? (via the memfiles plugin's `__cabinet_status` internal tool) | Application state (memfiles plugin) |
| `check_local_embed` | Local embedding service online? active model, loaded models? (via the local-embed plugin's internal `__embed_status` tool) | Application state (local-embed plugin) |
| `check_sharefile` | File-sharing tunnel online? ngrok URL? | Application state (sharefile plugin) |
| `check_mcp` | Wrapper health + per-server diagnosis (connected/disconnected/disabled, hints) | Application state (MCP wrapper + external servers) |
| `check_a2a` | A2A mesh connection + peer status (via the a2a plugin's `__a2a_status` internal tool) | Application state (a2a plugin) |
| `check_watchdog` | Auto-restart status per plugin, deduplicated from health records (latest record per plugin) | Process layer |

The watchdog only monitors processes — it does not introspect application state. Each plugin owns its own runtime health check. Missing deps are recorded as warnings — Slife still starts; affected MCP servers won't work.

## Logging Convention

Structured log lines: `event_name key1=value1 key2=value2 …` (see `slife/logfmt.py`).

- Event name: snake_case, past-tense for completions (`tool_done`), present-tense for state (`mcp_connected`)
- Levels: `debug` = per-request detail, `info` = lifecycle milestones, `warning` = recoverable, `error`/`exception` = hard failure (use `exception()` to keep the traceback)
- Every line that could contain user input, tool args, tool output, or subprocess stderr passes `sanitize_secrets()` before logging
- Plugins inherit the session id and write to per-session files via `setup_server_logging`; their stderr is relayed by the parent at DEBUG
- No diagnostics on stdout (reserved for the TUI and the plugin port signal)

### Sinks: log is for developers, TUI is for the user

Three sinks, two audiences:

- **Session log file** (`logs/*.log`) — full truth: DEBUG+, every level keeps
  its real meaning.  `warning`/`error` events are *never* demoted to `info` to
  hide them from the terminal — that corrupts the file and makes log-based
  diagnosis (or an LLM reading the log) see "all OK" when failures occurred.
- **Console (stderr)** — never emits: the main harness runs its stderr
  handler at `CRITICAL + 1` (a no-op), so no log record ever prints to the
  terminal — the terminal belongs entirely to the TUI.  (Plugin/subagent
  processes run stderr at DEBUG, but that is a diagnostic pipe to the parent,
  not a user terminal.)
- **TUI** — a pure business channel, decoupled from logs.  User-visible status
  (plugin load results, memory health, tool outcomes, OAuth notifications) is
  surfaced explicitly via `_show_system_message` / callbacks — never by leaking
  `logger.warning` to the terminal.  The plugin never talks to the TUI: the
  harness owns surfacing.  E.g. the sharefile ngrok tunnel is eager-started on a
  background task inside the plugin process; after the plugin loads, the main
  process probes the internal `__tunnel_status` (state `active`/`starting`/`failed`) until the
  attempt settles, and surfaces a one-time "tunnel unavailable" warning via
  `_show_system_message` only on a terminal `failed` state.

Known gaps: several call sites log raw user/tool/task content without sanitization, a few use prose instead of `key=value`, and the MCP wrapper stderr relay re-implements `drain_stderr` (it masks via `sanitize_secrets`, but the duplication remains).

## Dev vs. Production Data Directory

`slife/paths.py` decides where session data (config, `*.db`, `*.files`, `logs/`) lives. Two modes only:

- **Production** (default): everything under `~/.slife/`.
- **Dev**: the project root (CWD) — `pyproject.toml` beside the source tree.

`is_dev()` requires **both** conditions to hold:

1. the CWD's `pyproject.toml` declares `project.name == "slife"` — the CWD *is* the project root; and
2. the loaded `slife` package's parent directory **is the CWD** — i.e. the source `slife/` subdir of that checkout, not a site-packages copy (an editable install, or `python -m slife` from the tree, both satisfy this).

A production install always loads from a site-packages dir whose parent is never the CWD, so it stays production no matter where it is launched from:

- **inside a checkout** — the checkout's `pyproject.toml` is in the CWD, but the loaded package is site-packages (a pyproject-only check would misfire here, scattering data into the checkout);
- **the home directory** — uv tools install under `~/.local` / `%LOCALAPPDATA%`, i.e. *under* the home dir, so a package-under-CWD check would misfire here too (seeding a fresh config and empty DB in home while ignoring the existing `~/.slife/` data).

Either condition alone is ambiguous; both must hold.

## Project Structure

```
slife/
  agent/               # LLM interaction
    loop.py            #   Function-calling loop (streaming, concurrent tool execution, harness auto-invoke)
    service.py         #   Lifecycle manager (plugins, inbox, model switching)
    history.py    #   Message storage + history (OpenAI-format, sanitization, _ensure_turn_consistent)
    llm_client.py      #   Backend router + StreamChunk
    system_prompt.py   #   Prompt rendering (static + dynamic Jinja2)
    templates/         #   agent.j2, subagent.j2, slife.j2, context_status.j2, schedule.j2, schedule_trigger.j2
    llm_backends/      #   API backends: openai.py, anthropic.py, openai_responses.py
    inbox.py           #   Unified message queue + MessageHistoryStore
    plugins.py         #   Plugin spawn/stop + watchdog (PluginLifecycle)
    multimodal.py      #   Image encoding for vision models
    heartbeat.py       #   Autonomous heartbeat scheduling
  tools/               # Native tools (auto-discovered, 52: 51 LLM-visible + _sys_note)
    base.py            #   Tool ABC + make_params/NO_PARAMS/require_params
    registry.py        #   ToolRegistry
    factory.py         #   Auto-discovery (pkgutil.iter_modules)
    _config_io.py      #   JSON5 read/write helpers
    system.py          #   system_health + per-plugin checks
    exec.py            #   Shell, Python, package install, run_schedule_now (+ _kill_process_tree)
    skill.py           #   Skill management (SKILL.md)
    cli.py             #   External CLI tool management
    rest_api.py        #   REST API tool management (OpenAPI → MCP)
    subagent.py        #   Local worker tools (spawn/list/stop + delegation + task mgmt)
    models.py          #   Model management + attach_image (vision) + _sys_note (harness)
    config.py          #   Config env var + native tool toggles
    credentials.py     #   Credential check/inject/uninject
    meta.py            #   list_native_tools, check_async, cancel_async, clear_context, notify_user
  plugins/             # Built-in plugins (auto-discovered server.py packages)
    memdb/             #   Turns database (store, search, embeddings, schema.sql)
    wechat/            #   WeChat messaging (iLink ClawBot client)
    memfiles/          #   Private notes/diary/files cabinet (server.py tools, store.py + schema.sql)
    sharefile/         #   Public file sharing (share_file, /share route, tunnel.py = ngrok)
    a2a/               #   A2A mesh (a2a_* tools + A2AClient, MQTT binding)
    media/             #   Non-chat AI generation (server.py, config.py, adapters/ for dashscope-aigc + openai-images)
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
    identity.py        #   AgentName, HUMAN/WECHAT sentinels, AgentMessage
  subagent/            # Local workers (agent workers, not A2A)
    headless.py        #   Headless worker-scoped JSON-RPC process
    identity.py        #   SUBAGENT unified-inbox source sentinel
    process.py         #   SubagentProcess + SubagentManager
    tools.py           #   Subagent-scoped tool set
  ui/                  # Textual TUI
    app.py             #   Textual App, bindings, HistoryInput, StatusBar
    chat.py            #   Chat message widgets (clickable paths/URLs)
    handler.py         #   TUIHandler (bridges events → widgets)
    tool_display.py    #   ToolCallWidget + display helpers
    restore.py         #   Session restore (rebuilds UI from diary)
    approval_prompt.py #   Inline tool approval (Y/N/Esc, no modal)
    model_picker.py    #   Ctrl+S inline model picker
    content.py         #   Message content model
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
  threads.py           # run_daemon — daemon threads for blocking calls

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

mcp-plugin/             # Standalone workspace member — the external MCP gateway
  mcp_plugin/           #   (raw JSON-RPC: stdio/SSE/streamable), registered via plugins.external
    cli.py              #   mcp-plugin CLI (set / remove / build)
    server.py           #   FastMCP gateway server; main() accepts --port
    connection.py       #   ConnectionPool / MCPServerConnection
    client.py           #   Streamable HTTP client (used by the harness & CLI)

skills/                # On-demand SKILL.md skills (seeded to ~/.slife/skills/)
```

## License

MIT
