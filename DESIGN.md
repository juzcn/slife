# Slife Design

> Developer documentation for the Slife codebase. For installation, configuration, and everyday usage, see [README.md](README.md). For plugin authors, [PLUGIN_CONTRACT.md](PLUGIN_CONTRACT.md) is the authoritative plugin spec; for how the model context is curated each turn, [CONTEXT_HARNESSING.md](CONTEXT_HARNESSING.md) is authoritative. This document is written for people who work on the code, and it assumes you have read the README.

## Contents

* [How to read this document](#how-to-read-this-document) — the reader's map
* [Part 1 · Orientation](#part-1--orientation) — what Slife is, the process model, core concepts
* [Part 2 · The Agent](#part-2--the-agent) — agent loop, context management, harness vs internal tools, system prompt, heartbeat
* [Part 3 · LLM Backends & Model Management](#part-3--llm-backends--model-management)
* [Part 4 · The Tool System](#part-4--the-tool-system)
* [Part 5 · Plugins & the MCP Gateway](#part-5--plugins--the-mcp-gateway) — lifecycle, built-ins, gateway, jobs, subagents
* [Part 6 · Memory, Search & Embeddings](#part-6--memory-search--embeddings)
* [Part 7 · A2A — Agent-to-Agent](#part-7--a2a--agent-to-agent)
* [Part 8 · UI, Config, Credentials, Health, Logging, Paths](#part-8--ui-config-credentials-health-logging-paths)
* [Part 9 · Project Structure](#part-9--project-structure)
* [Appendix A · Design Decisions & Hard-Won Lessons](#appendix-a--design-decisions--hard-won-lessons)

---

## How to read this document

The sections are layered — orientation first, then the deep mechanics, then reference/appendices. Pick the lane that matches what you're doing:

| If you are… | Read |
|---|---|
| New to the codebase, wanting the map | **Part 1** (orientation + concepts), then skim Part 2 |
| Working on the agent loop / context / prompts | **Part 2** |
| Adding an LLM backend or dealing with wire formats | **Part 3** |
| Adding or changing a native tool | **Part 4** |
| Writing or debugging a plugin, the MCP gateway, jobs, subagents | **Part 5** + [PLUGIN_CONTRACT.md](PLUGIN_CONTRACT.md) |
| Working on memory, search, embeddings, session restore | **Part 6** |
| Working on the A2A mesh | **Part 7** |
| Touching the TUI, config, credentials, health, logging, paths | **Part 8** |
| Looking for a file or module | **Part 9** |
| Asking "why is it designed this way?" or debugging a hard-to-see regression | **Appendix A** + the relevant part |

**Authority and freshness.** Where this document and a dedicated spec disagree, the dedicated spec and the code win: [PLUGIN_CONTRACT.md](PLUGIN_CONTRACT.md) is the authoritative statement of the plugin system; [CONTEXT_HARNESSING.md](CONTEXT_HARNESSING.md) is the authoritative statement of context injection (channels, markers, the `_turn_prompt` harness tool-pair) and the deep companion to Part 2's *Context Injection* section. This document is kept current with the code; if a sentence names something that no longer exists, file a fix.

**Terminology** is defined where it first matters; a quick glossary of the load-bearing terms (also used in the README):

| Term | Meaning |
|---|---|
| **Turn** | One user→assistant exchange, persisted to the diary as one row. Turn is the unit of conversation history, memory, and trimming. |
| **Diary** | The `diary` table in `memdb` — a continuous, time-ordered log of every turn. (Not to be confused with the memfiles **Diary** records that `diary_write` writes.) |
| **Channel** | The sender identity of a message entering the unified inbox (`human`, `wechat`, `subagent`, `heartbeat`, `system`, `a2a`), persisted with the turn. A marker never determines a channel and vice versa. |
| **Marker** | Machine-generated notation inside a raw message (`[Heartbeat]`, `[Wechat:…]`, `[A2A:…]`, `[INFO: …]`) telling the model or the TUI something the message text alone doesn't say. |
| **Harness tool** | A `_`-prefixed, LLM-visible-but-reserved tool the loop auto-invokes — `_turn_prompt` (per turn) and `_check_new_input` (mid-turn message injection at iteration boundaries, cut-in mode). |
| **Internal tool** | A `__`-prefixed plugin tool that serves the main process, not the LLM — filtered out of the schema before registration. |
| **Silence contract** | A bare `.` assistant reply is silence, never rendered, from any turn source. |
| **Plugin** | An independent child process declared by one row in the central plugin spec, speaking the MCP contract to the main process. |
| **The gateway / mcp-gateway** | The built-in plugin that proxies external MCP servers. Third-party capability enters only as a standard MCP server. |

---

## Part 1 · Orientation

### What Slife is

A single Textual TUI around a streaming function-calling loop. The LLM picks from a unified tool registry — native tools, built-in plugin tools, and external MCP tools are indistinguishable at the call site (all OpenAI function definitions). Every turn is persisted unconditionally to SQLite; the context the model sees is engineered explicitly (see [Part 2](#part-2--the-agent) and [CONTEXT_HARNESSING.md](CONTEXT_HARNESSING.md)). Plugins — including the MCP gateway to external servers — are independent child processes over Streamable HTTP, declared spec-driven and driven by one uniform lifecycle.

```
┌──────────────────────────────────────────────────────────────────────┐
│  UI (Textual TUI)                                                    │
│  slife/ui/app.py, chat.py, handler.py, tool_display.py,              │
│  restore.py, approval_prompt.py, model_picker.py                     │
├──────────────────────────────────────────────────────────────────────┤
│  Agent Service                                                       │
│  slife/agent/service.py — wires client + tools + loop + plugins      │
│  Unified inbox serializes human + WeChat + MQTT + subagent messages  │
├──────────────────────────────────────────────────────────────────────┤
│  Agent Loop                              │  MCP Client                │
│  Streaming function-calling              │  Streamable HTTP transport │
│  Context trim (internal, after save)     │  OAuth device-code flow    │
│  + _turn_prompt pair; concurrent tools   │  Tool proxy + adapter      │
│  Thinking support                        │                            │
├──────────────────────────────────────────┴───────────────────────────┤
│  Tool Registry — unified OpenAI function definitions                 │
│  Native · Built-in plugin tools · External MCP ({server}__{tool})    │
├──────────────────────────────────────────────────────────────────────┤
│  Plugins — independent child processes (Streamable HTTP)             │
│  mcp-gateway · memdb · wechat · a2a (MQTT) · memfiles                │
│  sharefile · media · job-coding                                      │
├──────────────────────────────────────────────────────────────────────┤
│  Platform (slife/platform.py)  │  Config (JSON5)  │  Health checks   │
├──────────────────────────────────────────────────────────────────────┤
│  Credstore — credential store + AES cryptfile backup                 │
│  Win · Mac · Linux (keyutils) · WSL (PowerShell)                     │
└──────────────────────────────────────────────────────────────────────┘

External to the process tree (started/owned by the user):
  · local-embed daemon   — local GGUF/transformer embeddings at :17347 (like Mosquitto)
  · Mosquitto (MQTT)     — the A2A transport binding
  · external MCP servers — stdio / SSE / Streamable HTTP, via the gateway
```

Two MCP directions, deliberately uniform: the main process is an **MCP client** to its own child plugins (`client.py` builds on the MCP SDK's `client_session`); the **gateway** plugin is simultaneously an MCP client to external servers and an MCP server to the main process. Slife itself can collapse to a server: `slife/mcp/host_server.py` exposes the live tool registry as an in-process FastMCP — "slife-as-plugin".

### Design principles

1. **Minimum harness, maximum distance from the model.** Assume a capable model: the prompt and harness are the smallest patch that keeps it effective and safe. No prompt-engineering scaffolding; tools are described by schema, usage lives in descriptions, not instructions.
2. **One registry, one wire.** Everything is an OpenAI function definition. Backends own their own wire conversion; the loop never branches on which backend generated a chunk.
3. **The model never runs silently without memory.** Every turn is saved, unconditionally; a broken memory DB is a hard stop, never a limp-along. (See [Part 6](#part-6--memory-search--embeddings).)
4. **Capability enters through standards.** Plugins are MCP servers; external capability is an external MCP server; nothing is a bespoke plugin API. (*"All are plugins"* — DESIGNER_NOTES §5.1.)
5. **Determinism where the model is the wrong tool.** Schedules dispatch to subagent workers; deterministic jobs are code-defined functions; the model only delegates, never inlines (see [Job System](#job-system-job-coding) and [Scheduled Tasks](#scheduled-tasks-the-timing-side)).
6. **Fail-open for what you can't control.** Subordinate dependencies (external servers, tunnels, WeChat login, brokers, embedding daemons) never gate startup; the core is core.

### Language policy

Two audiences, two languages. The model input reads uniformly in English; the human-facing TUI follows the OS locale.

**Model input — English (uniform):**

- **System prompt** (`agent.j2` / `subagent.j2` + `slife.j2`, `turn_prompt.j2`): English.
- **Native tool schemas** — tool `name`, `description`, parameter docs, and result strings: English.
- **Plugin tool schemas and result strings**: English (same policy as native tools — they are model-visible).
- **External tools** (MCP servers, skills, third-party commands): keep the language of the external source — do not translate. They are opaque and pass through as-is.
- **Logs** (session file + console): English — for developers, per the [Logging Convention](#logging-convention).

**TUI — bilingual (English / Chinese), by OS locale:**

- Detected once at import via [`sys-lang`](https://pypi.org/project/sys-lang/) (`get_sys_lang(region=False)`): `zh*` → Chinese, everything else → English, degrading to English on detection failure.
- `--lang en|zh` overrides detection — `python -m slife --lang zh` forces Chinese regardless of the OS locale (`parse_cli_lang` → `set_language` in `slife.ui.i18n`).
- The translation layer is `slife/ui/i18n.py` — a single `t(key, **fmt)` accessor over an `en`/`zh` string table. No catalogs, no YAML, no Pydantic.
- Everything the human reads is localized: system messages (plugin load results, memory health, restore outcomes), the approval prompt, the model picker, the tool-call widget labels, the status bar, thinking blocks.
- **What stays English regardless of locale:** key caps in the status bar (`Ctrl+C`, `Esc`, `Ctrl+S`, `Home/End`) — translating a key label breaks the key→action scan and mismatches what the user actually presses; and the `notify_user` / OAuth notification *body* — that is LLM- or system-supplied text, not Slife-authored chrome.
- Tests pin the language to `en` via an autouse fixture in `conftest.py`, so the suite's English assertions hold regardless of the dev machine's locale.

### Context injection — a preview of the taxonomy

The system introduces information into the context on its own initiative in three ways, distinguished by *what* is injected and *whether it persists*. Two orthogonal notions run through them (authoritatively described in [CONTEXT_HARNESSING.md](CONTEXT_HARNESSING.md)):

- **Channel** — the sender identity, recoverable from the message alone, persisted with the turn, by default **not** part of the LLM context.
- **Marker** — machine-generated notation an injection carries (`[Heartbeat]`, `[Schedule <name>]`, the `[INFO: …]` footnote/trim note).

The three forms:

1. **A marker-carrying synthetic user message — persistent.** `[Heartbeat]` and `[Schedule <name>]` triggers are posted to the inbox as normal turns and saved like any turn.
2. **A harness tool-pair — persistent.** `_turn_prompt` is auto-invoked once per turn and contributes a normal assistant `tool_call` + result to the history.
3. **A marker appended to an existing message — not persisted content.** The turn footnote (`[INFO: {"turn_id": N, …}]`) and the trim note decorate messages already present.

---

## Part 2 · The Agent

### Agent Loop

Single function-calling loop. Every tool is registered as an OpenAI function definition in one `ToolRegistry`. The LLM decides what to call and when.

```
User Input → MessageHistory.add_user_message()        (secrets sanitized)
  → loop (max_iterations):
    → cancel check
    → auto-invoke _turn_prompt (per-turn prompt)    (usage computed once)
    → LLM stream → thinking/text/tool deltas → handler callbacks
    → tool calls? → ToolRegistry.execute() concurrently (asyncio.gather)
                    → sanitize_secrets() on each result → truncate → loop
    → `_approve: true` on a call? → serialized ApprovalPrompt before execution
    → no tool calls? → response text → return
    → save turn to diary (unconditional — even on cancel/error/max-iterations)
    → trim after save (internal, real usage)        (see Context Window Management)
```

- **Streaming**: thinking and text tokens delivered in real time via `AgentEventHandler` callbacks.
- **Tool accumulation**: tool-call deltas accumulated across chunks, executed as a batch.
- **Concurrent execution**: all calls in a batch run via `asyncio.gather`; approval dialogs serialize behind a lock.
- **Tool timeout**: single enforcement point — `asyncio.wait_for()` wraps every call (default 120 s, `agent.tool_timeout`) as a **fallback** only — the LLM passes a per-call `_timeout`, and tools with a native `timeout` parameter (`execute_shell`) receive it directly instead of a double wrap. A bare `timeout` argument on a tool whose schema has none is also consumed and enforced (LLMs routinely append one), exactly like `_timeout`.
- **Background execution**: per-call `_async: true` schedules the tool as a background task and returns a task id immediately; poll with `check_async`, cancel with `cancel_async`. The async runner **sanitizes at storage time** (secrets are scrubbed the moment the task finishes, not when polled), and a **failed** async task surfaces with the `Error:` prefix — the same `is_error` contract as a synchronous call. Results are pruned past a bound (`_MAX_ASYNC_TASKS = 100`), so a very old poll can answer "Task not found".
- **Iteration limit**: `max_iterations` (default 30) prevents infinite loops; **0 = unlimited**. The cap is checked **live each iteration** (not fixed at `run()` start), so a mid-turn `set_max_iterations` applies **immediately** to the running turn and to the next. Hitting the cap returns a cancelled result and notifies the handler via `on_max_iterations` — the TUI shows `✗ Agent exceeded maximum of N iterations`.
- **Cancellation**: `Esc` sets a cancel event; checked before each iteration, after each stream, and before each tool batch.
- **LLM stream failure contract (one contract, every source)**: transient transport failures — `httpx.TransportError`, the SDKs' `*APIConnectionError` / `*APITimeoutError`, and the loop's own `StreamStallError` (below) — are retried by `_process_stream` with bounded linear backoff (`_LLM_STREAM_MAX_RETRIES` = 2 ⇒ 3 attempts at `0.5 s × attempt`), so the main agent, subagents, heartbeat, WeChat and A2A share a single resilience contract. Bad-request / content-filter / auth errors are **not** retried here (SDK + inbox concern). Exhaustion raises `RuntimeError("LLM stream failed after N attempts: …")` with a **non-empty** detail (`str(e) or type(e).__name__`). The history is kept intact on transient failures; only content-policy / bad-request errors roll back.
- **Stall watchdog (`stream_stall_timeout`)**: `_consume_stream` wraps every `anext()` in an `asyncio.timeout` that **resets on each chunk** — a provider that answers `200 OK` and then sends nothing (Bailian did exactly this: zero bytes for ~7 min before dropping the connection) is cut after the default 120 s (`_LLM_STREAM_STALL_TIMEOUT`) with `StreamStallError`, which the retry ladder handles like any other transient failure. A slow-but-live generation is never cut — this is an *inactivity* timer, not a *total* one (the "timer at the owner, no total" rule). The separate opt-in `stream_timeout` remains a **total** per-call cap (subagents inherit the agent `task_timeout`).
- **Turn consistency**: one function — `MessageHistory._ensure_turn_consistent()` — enforces two idempotent invariants before a history is persisted (and again on load), so it is always well-formed when it next reaches the wire:
  1. **No orphaned tool_calls** — an assistant `tool_call` whose result never arrived (an interrupted turn) gets a synthetic `(Tool execution interrupted)` result right after it.
  2. **Alternating roles** — a history ending on a `user`/`tool` message (a tool result is a `user` role on the Anthropic wire, which rejects two consecutive users with a 400) gets a closing assistant message (`"(Turn interrupted)"`).
  It has exactly **two call sites**: `save_to_memory` (before persisting — the save-side guarantee, which runs unconditionally after every turn) and `restore_session` (after loading — the load-side guarantee).
- **Context tracking**: `AgentLoop.context_tokens_for()` is the single source for the current context size (the last API call's actual prompt + completion tokens — the exact token count of the persisted history as the next request would re-send it — else the restore-time value primed on `_last_usage` — the latest restored turn's persisted `context_tokens` — else the chars÷3 live estimate). It drives `_turn_prompt`, the trim decision, and the TUI status bar — one value, no recompute. Usage is tracked **per history** (`_usage_by_history`, keyed by `id()`): the heartbeat, A2A, and WeChat turns run in their own (often tiny) histories, so a 9.6% heartbeat never drags the human history's status bar / `_turn_prompt` down from its real 26.5%.

### Context Window Management

Active history stays within `context_floor`–`context_ceiling` (default 20%–80% of `context_window`):

```
                context_window
┌──────────────────────────────────────────────────────────────┐
│   trimmed (in diary —        │  current context  │  headroom  │
│   recall via turn_search)  │  floor ~ ceiling  │  1-ceiling │
└──────────────────────────────────────────────────────────────┘
```

- **Detect**: usage is `context_tokens_for()` — the history's last API call's actual prompt + completion tokens after the first round (per-history), else the restore-time `_last_usage` (the latest restored turn's **persisted `context_tokens`**), else the chars÷3 estimate.
- **Trim**: happens **after a turn is saved** (`save_to_memory` → `AgentLoop._trim_after_save`) — by then the last API call's real prompt + completion tokens are known. When occupancy hits `context_ceiling` (default 80%), `extract_oldest_turns` removes the oldest **complete** turns down to `context_window × context_floor` (default 20%), always keeping the current (just-saved) turn. It is an **internal mechanism — no tool call, no LLM-visible pair**: the cut is marked with a runtime-only **`[INFO: N oldest turns have been removed from context]`** note appended to the last assistant message, mirrored in the live TUI as a dim/italic footnote. `advance_context_start` persists the boundary (via the memdb internal tool `__memory_context_start_advance`), and the tracked "Context covers" time range advances by the same count. A freshly-restored history is exempt from the first-turn trim (`_just_restored_history`).
- **Turn prompt**: once per turn the loop auto-invokes **`_turn_prompt`** (a normal tool-call pair) — it renders `turn_prompt.j2`: current time, context usage %, token usage, context time range, change notifications (model/CWD/shell/modalities), any A2A peer presence events since the last turn (drained read-once), open failed/missed scheduled runs, and the one-shot "system restarted" flag. On the first round after a restore, `context_tokens_for` falls back to `_last_usage`, primed with the latest restored turn's persisted `context_tokens` — so the first prompt reports the real exit-time occupancy.
- **Restore**: on startup, the diary rows recorded **after the persisted live-context boundary** are loaded directly from SQLite **verbatim** — no ceiling re-slicing. The boundary already encodes the trimmed state. It lives in `diary_meta.context_start` (exclusive rowid): the internal trim advances it by the turns it evicted and `clear_context` flushes it past the whole in-context slice. `get_recent_turns` returns `(turns, skipped=0, budget=0)` — skipped/budget are kept for call-site compatibility only. A stale boundary of `0` from a pre-boundary DB is defensively capped at 2× the ceiling. There is **no migration layer** (backward compatibility is not supported): schema changes land directly in `schema.sql` and apply to fresh databases only (the one exception: `scripts/migrate_context_tokens.py` renames `prompt_tokens` → `context_tokens`).
- **Tool result cap (HARD constraint)**: a single tool result is truncated at `tool_result_ceiling × context_window × 3` characters (default 20% of the window; ~3 chars/token heuristic) with an explicit truncation marker in the output. This is the deliberate window-safety limit — generous enough that a large-but-real file read is never truncated; only pathological outputs that could not fit the window at all are capped.
- **Permanent-memory compaction**: the diary does **not** hoard reproducible tool output. At `save_to_memory`, any tool result exceeding `memory_tool_result_chars` (default 8000) is stored as a head+tail digest with an explicit marker — original size plus which tool to re-run (`… [compacted at save: original N chars — full output retrievable by re-running <tool>]`). Small results are stored as-is. The live history keeps the full result — compaction only affects the persisted copy.
- **Truncation is announced in the tool output itself** (not the system prompt): both the live cap and the save-side compaction append a marker inside the result telling the model it was truncated and that re-running the tool retrieves the full version.

### Harness vs Internal Tools — a naming distinction

Two distinct concepts live under different prefixes. They are **not** two tiers of the same thing:

1. **`_` (single underscore) = harness, LLM-visible but reserved.** Harness tools are invoked by the agent loop *on the agent's behalf* — the LLM does not decide to call them. The only one is the native `_turn_prompt` (`slife/tools/models.py`): `AgentLoop._auto_invoke()` injects it each turn as a normal `assistant(tool_calls)` + `tool` pair. It **does** appear in the schema — required so the Anthropic / OpenAI-Responses backends accept its tool-call pair in history — and the system prompt tells the model to *read its latest result* rather than call it (an implicit don't-call; it is side-effect free if invoked anyway). Context trimming is **not** a tool. Note: `attach_image` is also auto-invoked via `_auto_invoke`, but it has no `_` prefix and is not schema-reserved, so it is not a harness tool.
2. **`__` (double underscore) = plugin internal tool, LLM-invisible.** This is a **plugin-spec marker**, not a harness concept. Plugin internal tools (`__memory_save_turn`, `__a2a_drain_incoming`, `__mcp_call_tool`, `__check`, …) are ordinary MCP tools that happen to serve the main process rather than the LLM. They are filtered out of the schema before registration (`is_internal_tool` in `slife/server_utils.py`, applied on every registration and reconcile path) and are called programmatically via `client.call_tool("__…")`.

| Tool | Shape | Category |
|------|-------|----------|
| `_turn_prompt` | Native tool, auto-invoked each turn | Harness — visible-but-reserved |
| `__memory_save_turn` / `__memory_get_recent_turns` / `__memory_reload_semantic` / `__memory_context_start_advance` / `__check` | memdb plugin | Internal — invisible |
| `__wechat_drain_incoming` / `__check` | wechat plugin | Internal — invisible |
| `__scheduled_*` (10) / `__memfiles_reload_semantic` / `__user_pref_append` / `__check` | memfiles plugin | Internal — invisible |
| `__a2a_drain_incoming` / `__a2a_dispatch_result` / `__check` | a2a plugin | Internal — invisible |
| `__check` / `__mcp_call_tool` / `__mcp_get_tool` | mcp-gateway plugin | Internal — invisible |
| `__check` / `__register_file` | sharefile plugin | Internal — invisible |
| `__check` | media plugin | Internal — invisible |
| `__set_mcp_gateway_port` / `__check` | job-coding plugin | Internal — invisible |

### System Prompt

The system prompt splits **identity** from **world** so each role reads one coherent document:

- **Identity** — `slife/agent/templates/agent.j2` (main agent) / `subagent.j2` (worker): who the agent is. Role framing only — heartbeat/persistence ownership for the main agent, ephemeral/send-only constraints for a worker. The only part that carries persona.
- **World** — `slife/agent/templates/slife.j2`, `{% include 'slife.j2' %}` by both identity templates: the runtime spec — context policy (floor/ceiling/tool-result %, the meta-parameter contract), host platform (OS, arch, shell, python), workspace paths (data/config/logs/db/skills), annotation/marker expectations, the credential resolution chain, MCP tool naming prefix, skills & jobs, subagents, and A2A broker info when configured. Byte-identical in both roles.
- **Dynamic** — `turn_prompt.j2`, rendered by the `_turn_prompt` tool (auto-invoked once per turn): current time + UTC offset and context usage % always; context time range when set; model/CWD/shell/modalities only when changed; pending A2A peer presence events; open failed/missed scheduled runs (the "backfill or skip?" list); the one-shot "system restarted" flag.

Identity + world are rendered once at startup and never change → maximal prompt cache hit rate.

Design principles:
1. **World spec is project-specific facts only** — if the LLM can infer it from tool schemas or training data, it doesn't belong.
2. **Tool schemas over prompts** — usage instructions live in function `description`/`parameters` (see [Schema Authoring](#schema-authoring)).
3. **No personality in the world spec** — role identity lives in the identity templates.
4. **No slash commands** — natural language only; the LLM interprets intent.
5. **Static baseline + change notifications** — constants at startup, deltas per-turn.

The system prompt additionally forbids nothing by list — it relies on scaffolding, not prohibitions: `_`, `__`, and the meta-parameters are each explained once, structurally, so the model reads the mechanism rather than a denylist.

### Autonomous Heartbeat

The agent is otherwise purely user-driven. A heartbeat gives it a periodic **autonomous window** (a precondition for emergent self-initiated behavior): while idle, every `agent.heartbeat_interval` seconds (default 60, shipped template 1800) the service posts a `[Heartbeat]` message to the inbox, which runs as a **normal agent-loop turn** (own history via the heartbeat source, saved to the diary like any turn).

- **Reply contract** (also in the system prompt): real content if the agent has something worth proactively saying, otherwise exactly `.` — never empty, satisfying the user→assistant role alternation.
- **TUI filtering** (live + restore): heartbeat turns are recognised by the `[Heartbeat]` mark and filtered — the trigger is never shown, and a real reply renders as `⚡ 自主`. More generally, a bare `.` reply is **silence** from any event. The status bar shows the last beat (`●` act / `·` quiet).
- **Main agent only**: subagents never start the heartbeat loop — they are task-driven workers.
- The heartbeat history is separate (source `heartbeat`), so autonomous reflections persist without polluting the human history.

### Scheduled Tasks (the timing side)

Recurring tasks the agent runs on a cron schedule, designed as three separated concerns — **timing** (a thin main-process loop), **execution** (a subagent worker), and **record** (the memfiles DB):

- **Timing — `schedule_loop` (`slife/agent/schedules.py`).** Main-agent-only, started alongside the heartbeat. Every 30 s it recomputes each enabled task's next fire **from the DB, not from memory**: the anchor is the newest `due_at` across all of a task's runs (a fire is never re-detected), falling back to `created_at`. Cron parsing uses `croniter` (`slife/schedules.py` is a thin wrapper). The loop **fires only**: against a short grace window (120 s), a fire due within it is fired; anything older means slife was down (the startup sweep's concern). An in-memory pending-fire guard keeps the poll from re-firing mid-turn.
- **Trigger → execution.** The loop injects a `[Schedule <name>]` trigger under the **system channel**; the run is recorded when the agent dispatches, not at fire time. The agent handles the trigger by delegating: `run_schedule_now` — the single dispatch tool, also used to backfill — records a `scheduled_runs` row (`pending`), spawns/reuses the subagent named after the task, and sends it a deterministic task text via `subagent_send_task_async` instructing it to call `report_save` and notify the user. Completion rides the existing subagent auto-push back to the main agent, which reports the task as finished (reworded to hide the subagent).
- **Record.** `scheduled_tasks` (definition), `scheduled_runs` (per-fire state + report link), and `reports` live in the memfiles DB. A `report_save` bound to a task backfills the newest un-linked run's `report_id` at the store layer — pending → ran is the **only** success writeback.
- **Failed & missed runs — settled at startup.** The one-shot `schedule_startup_sweep` reaps every surviving `pending` run to `failed`, and fires due while slife was down to `missed`. It posts no message. Both surface via `scheduled_run_list` and can be backfilled (`run_schedule_now`) or closed (`scheduled_run_skip`). Tasks fire **only while slife is running**.

Tools: `scheduled_task_set` / `scheduled_task_remove` / `scheduled_task_list`, `scheduled_run_list` / `scheduled_run_skip`, `run_schedule_now` — all native, "Schedule" category. `run_schedule_now` takes `due_at` (backfill) and `clone_context=True` (spawn the worker with a clone of the current conversation). `scheduled_task_set`'s `description` is **schema-required** — it is the worker's instruction.

### Context Injection

> The authoritative description of channels, markers, and the `_turn_prompt` harness tool-pair is [CONTEXT_HARNESSING.md](CONTEXT_HARNESSING.md); the condensed overview is in [Part 1 · Context injection](#context-injection--a-preview-of-the-taxonomy). Context trimming is internal and announced by the trim note, not by a harness pair.

## Part 3 · LLM Backends & Model Management

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

**Prompt caching (Anthropic system blocks):** `AnthropicBackend` emits each OpenAI `system` message as an Anthropic system content block and tags the **last** one with `cache_control: {type: "ephemeral"}` — the static base prompt becomes the cache breakpoint, so only the dynamic `_turn_prompt` status (a message-stream tool pair, never a second `system` message) changes per turn. Guarded by `_use_system_cache_control()`: on by default for `api.anthropic.com`, off for Anthropic-compatible providers (Bailian/Qwen) that may reject the field, overridable per model via `compat.cacheControl`.

**History validation.** Anthropic (and OpenAI-Responses) reject tool calls in history whose names aren't in the declared `tools` list. `_turn_prompt` is therefore a **declared native tool** (schema-present, auto-invoked by the loop), not a history-layer fabrication — so its pair validates. DeepSeek (Chat Completions) doesn't validate and is unaffected. Context trimming no longer needs schema validation at all — it is internal (`_trim_after_save`), not a tool call.

**History wire shape.** `OpenAIResponsesBackend` emits the Responses API's native `function_call` / `function_call_output` items for tool history — not the Chat-Completions `role:"tool"` / `tool_calls` shape (unit-tested; not yet exercised against a live endpoint).

**Outbound wire hardening.** `OpenAIBackend._normalize_messages` replaces empty assistant content (a reasoning-only turn, a max-tokens cut) with `"…"` — a copy, storage untouched — so openai-completions providers never 400 on an empty assistant message; Anthropic emits a single empty text block for an empty assistant turn.

### Known upstream interference (not a Slife bug)

An Anthropic-Messages gateway that runs LiteLLM's prompt sanitizer rewrites an empty `text` block sitting next to a `tool_use` into the literal `[System: Empty message content sanitised to satisfy protocol]` — `_EMPTY_TEXT_PLACEHOLDER` / `_sanitize_empty_text_content` in LiteLLM (issue BerriAI/litellm#24498; fix PRs #28987, #34822). An assistant turn that is `content: ""` + `tool_calls` is the ordinary shape between a tool call and its result, so Anthropic accepts it with the empty text block dropped — the substitution is a LiteLLM defect that runs **outside** the `modify_params` gate, with no config knob to disable it. Observed in slife via a `bailian_personal` provider (2026-08-21). It **poisons history**: the placeholder persists verbatim into the diary and replays into the next request. Slife stores it as ordinary assistant text — contrast with slife's own outbound hardening, which cannot see a placeholder the gateway already substituted into the **response**. If the gateway substitutes anyway, the only recourse is cleaning the persisted rows (a one-off data cleanup stripping that placeholder) before restore replays it. **Appendix A** records the incident class: a gateway defect that looks like a Slife bug.

### Model Management

Runtime model management via native tools — no config editing needed:

| Tool | Description |
|------|-------------|
| `model_list` | All configured models grouped by provider (active marked) |
| `model_set` | Add/update a model (creates provider if new) |
| `model_remove` | Remove by ref; auto-switches if it was active |
| `model_switch` | Switch active model by ref — persists to config and rebuilds the client live |

`model_set` is an **upsert that merges, not replaces**: a partial update (e.g. only `max_tokens`) keeps the model's existing `reasoning`, `input`, `compat`, and other fields. It also accepts a `compat` dict, so per-model compatibility overrides can be configured without hand-editing `slife.json5`. `model_list` surfaces the `compat` dict on each model.

Model switches fire callbacks that rebuild the LLM client, update loop parameters (vision, context window, modalities), and re-render the system prompt. See [Model Switching](#model-switching) for the picker rules.

## Part 4 · The Tool System

### Tool ABC

`Tool` (`slife/tools/base.py`) defines `name`, `description`, `parameters` (JSON Schema), `category`, and `async execute(**kwargs) -> str`. Required fields are validated at class-definition time via `__init_subclass__`. `from_config(cfg, config, ctx)` allows per-tool construction from the `tools:` overrides in `slife.json5`; `ctx` carries runtime references (registry, config, MCP client, history) as `self._ctx`.

`execute_shell` runs commands in the **detected shell** — `detect_current_shell()`: PowerShell / cmd on native Windows, `$SHELL` on POSIX incl. WSL — the **same value the system prompt reports**, so the LLM's shell syntax actually executes. Output is decoded with the system code page (GBK/cp936 on zh-CN Windows); `run_python_script` forces the child Python to UTF-8 via `-X utf8`.

Three tool tiers exist, indistinguishable to the LLM: **native** tools (own names), **built-in plugin** tools (bare names, `[<server>]` description prefix), and **external MCP server** tools (`{server}__{tool}`, loaded on demand). The naming rules are fixed; the developer-facing mechanism is described below.

### Schema Authoring

The schema is the model's only view of a tool — write it for the model, not the maintainer:

- **`description` = what the tool does.** One or two sentences: what it does and what it returns. Do **not** write when-to-use ("Use when…"), and do **not** restate knowledge the LLM already has (pip, timeouts, env-var concepts). Keep project-specific facts the model cannot infer — idempotency ("upsert — add + update in one call"), blocking ("BLOCKS until the model is loaded"), effect timing ("takes effect after restart"), or that a value comes from a sibling tool.
- **Parameter docs = how to use.** Per parameter: the accepted format, where the value comes from ("`turn_id` from `turn_list`"), what the values mean, and the default.
- **Mechanism.** Native tools carry parameter docs directly in the `parameters` dict. Plugin tools (`@mcp.tool`) get them from a Google-style `Args:` docstring — fastmcp parses it into the input schema, so a plugin tool whose parameters have no `Args:` yields an undocumented schema.
- **Language.** Model-visible strings are English (see [Language policy](#language-policy)). Content authored by an external source keeps the source language.

There is a deliberate asymmetry: tool schemas sent to the LLM carry **business parameters only**. The three meta-parameters (`_timeout`, `_async`, `_approve`), declared once in the system prompt, are popped by `_execute_tools` before dispatch — re-describing them on each of ~60 schemas would be the single biggest per-request context tax.

### Auto-Discovery

`slife/tools/factory.py` uses `pkgutil.iter_modules` to import every module in `slife.tools.*` (skipping `base`/`factory` and the `_skip_auto_register` base classes `_ModelConfigTool` / `_EmbeddingsConfigTool`), then walks `Tool.__subclasses__()` recursively. A new `.py` file is automatically picked up. Filtering applies `enabled: false` overrides and per-model requirements enforced at **execute time** rather than load time: tools are always registered, and a tool like `attach_image` refuses at runtime when the active model has no vision (`vision=false` error) instead of being silently-missing.

The current inventory — 60 native classes in 12 categories (~59 LLM-visible with the shipped config's `install_python_package: enabled: false`), plus the built-in plugin tools by server — is enumerated in the [README](README.md#tools). It is a *reference*, not a duplicate: the mechanism lives here, the catalog lives there.

### Tool Categories & Managed Surfaces

All tools are unified under `Tool` and registered in a single `ToolRegistry`. **Managed categories** (Skills / CLI / REST API / Models / MCP / embeddings) support a standard **`X_list` / `X_set` / `X_remove`** surface (plus `X_set_enabled` where a toggle applies). `X_set` is an idempotent upsert — add + update in one call. Config uses the `config_env_*` prefix (no `config_list`); Models substitutes `model_switch` for `X_set_enabled`; embeddings tools are `embeddings_model_*` + `embeddings_enable`.

### Registry

`ToolRegistry` is a name-keyed dict with `register` / `unregister` / `unregister_by_prefix` / `get` / `list_tools` / `to_openai_functions` / `execute`. Dynamic tools — built-in plugin tools and the MCP wrapper's own tools — are registered at runtime as `MCPProxyTool` instances under their **bare names**; external MCP server tools are named `"{server}__{tool}"`. Internal plugin tools (`__`) are filtered out before registration. External MCP tools are **on-demand**: registered only for an `auto_load` server, or individually when the model loads one with `mcp_tool_load`; a `tools/list_changed` reconcile (`_sync_mcp_proxies`) unregisters any loaded proxy whose tool vanished, server disconnected, or was disabled.

### Tool Result & Error Signaling

Every tool returns a single string (`async execute(**kwargs) -> str`). The failure contract is one rule, one token: **a failed call returns a string starting with `Error:`**. The harness derives the persisted `is_error` flag from exactly that prefix at both dispatch sites, judged **before** the args-truncation marker (`⚠ Provider truncated this tool call's arguments …`) is prepended — so a failed call whose result began with `Error:` still reads as an error even when the marker leads the text. The flag is stored on the tool message, and session restore reads the stored flag rather than re-deriving it (`tool_result_is_error`). Wrappers over a plugin's JSON error envelope (`{"status": "error", "error": …}`) translate it to an `Error:`-prefixed string before returning. There is deliberately no second failure token.

### Timeout Architecture

Single enforcement point at the Agent Loop level. The three meta-parameters (`_timeout`, `_async`, `_approve`) are a **system-prompt contract** (slife.j2, "Tool meta-parameters"): any tool call may carry them, and `_execute_tools` pops them before dispatch.

- Tools **without** a native `timeout` parameter → `asyncio.wait_for(timeout=…)` (default 120 s).
- Tools **with** a native `timeout` (`execute_shell`) → mapped to the native argument, no double-wrap.
- **Bare `timeout` alias** — LLMs routinely append a schema-less `timeout` arg to tools that define no such parameter; the loop pops it and enforces it as the per-call bound, exactly like `_timeout`.

`_async: true` (background, poll with `check_async`) and `_approve: true` (inline approval) are described in [Agent Loop](#agent-loop) — background tasks without a native timeout still get the loop's bound, and a background task's `_timeout` is honored.

### Approval Gate

Approval is **model-driven** (pure model judgment). When the LLM sets `_approve: true` on a call, execution pauses and an inline `ApprovalPrompt` row is mounted in the chat stream (Claude Code style, no modal: Y = approve, N / Esc = deny). Prompts serialize behind a lock. A denied call never mounts a `ToolCallWidget`; the prompt row itself carries the rejection state. There is no hardcoded `requires_approval` flag on any tool or MCP server — the model decides per-call. Headless (subagent) contexts have no handler and auto-approve.

The inline prompt declares its own `y → approve` / `n`/`escape → deny` bindings at `priority=True`; the App's `escape → cancel` is deliberately *not* priority so Textual's priority pass (which resolves the App before the focused widget) cannot steal Esc — Esc on an approval always denies.

### Model Switching

The active model is switched via the `model_switch` tool in normal operation. The `Ctrl+S` inline picker is an **emergency escape** for when the current model is unavailable and the LLM can't call `model_switch` itself — switching is config + runtime only, no API call. `AgentService.switch_model(ref)` validates, persists `active_model` to the config file, and rebuilds the LLM client / loop / system prompt. (`Ctrl+S` is not `ctrl+m` — Textual aliases that to enter — nor `ctrl+g`, which VSCode's goto-line steals.)

Picker rules (hard-won, kept with the code):

- Pure priority bindings — `↑`/`↓` move a cursor, `Enter` picks, `Esc` cancels. No `_on_key` / `on_click` overrides (they swallowed keys). The cursor opens on the active model, so a bare `Enter` re-selects it.
- The binding action must be **sync**: binding actions run inside the key-event handler, so awaiting the picker's future there deadlocks the TUI. The await lives in a background task (`_finish_model_switch`).
- Scroll to the picker **after layout** (`call_after_refresh(scroll_end)`) — an immediate scroll runs against the pre-mount content and pins the view above the fold.
- Every configured model is listed (no cap); the chat scrolls if the list is taller than the viewport.

## Part 5 · Plugins & the MCP Gateway

Eight internal plugins run as independent child processes: mcp-gateway, memdb, wechat, memfiles, sharefile, a2a, media, and job-coding. Every plugin is declared by one row in the central plugin spec (`slife/plugins/spec.py`) and driven by the same uniform lifecycle (spawn → MCP-handshake readiness → watchdog → health); the authoritative contract is [PLUGIN_CONTRACT.md](PLUGIN_CONTRACT.md). There is **no `plugins.external` mechanism** — third-party capability enters only as a standard MCP server in `mcp-plugin.json5`, connected by the internal **mcp-gateway** plugin. `local-embed` is **not** a plugin: a standalone daemon (started manually, like Mosquitto) serving OpenAI-compatible `/v1/embeddings`. Communication is **Streamable HTTP** (MCP protocol) for all plugins; the sharefile plugin additionally serves plain-HTTP file bytes on the same port via a custom route (`GET /share/{token}`).

**WSL note:** Custom env vars set via `create_subprocess_exec(env=…)` are NOT forwarded to Windows `.exe` processes through WSL interop (`WSLENV` is only read by the WSL `/init` at session start). Therefore **all MCP server runtimes on WSL must be Linux-native binaries** — the install script enforces this by detecting `/mnt/*` paths.

### The spec and the uniform lifecycle

Plugins are **spec-driven**: the `PluginSpec` table in `slife/plugins/spec.py` is the single source of truth (`name`, module to spawn, the `ctx_field` that carries its client, gateway/host flags, health-check name, reserved internal names), and `AgentService._plugins` derives the registry from it. Adding a plugin is one `PluginSpec` row plus its `server.py` package — no name-keyed harness tables. Auto-discovered third-party plugins (a package in `slife.plugins.*` with a `server.py` and no spec row) get a generic spec and the same lifecycle. The two public names with hyphens are `mcp-gateway` and `job-coding` (packages `mcp_gateway` / `job_coding`).

The uniform start chain: `start_plugin_server` → spawn the child with the inherited session env plus `SLIFE_PLUGIN_NAME` and its published port → MCP-handshake readiness (`initialize`, which the server only answers after its FastMCP lifespan succeeded) → `_after_ready_*` hook if the spec declares one → arm the watchdog. There are no per-plugin start methods left — every plugin routes through the spec.

Processes communicate through environment variables:

| Variable | Purpose |
|----------|---------|
| `SLIFE_SESSION_ID` / `SLIFE_AGENT_NAME` | Log correlation, agent identity |
| `SLIFE_DATA_DIR` / `SLIFE_CONFIG_DIR` / `SLIFE_LOG_DIR` | Directory overrides |
| `SLIFE_PLUGIN_NAME` | Which plugin this child is |
| `SLIFE_{NAME}_PORT` | Published port of each plugin (MCP_GATEWAY / MEMDB / WECHAT / MEMFILES / A2A / MEDIA / JOB_CODING / SHAREFILE). Key is the uppercased plugin name with dashes normalised to underscores (`job-coding` → `SLIFE_JOB_CODING_PORT`) — via `plugin_port_env`. Subagents read this env to share the parent's plugins. `local-embed` is a daemon, not a plugin, so it publishes no port. |
| `SLIFE_SHAREFILE_URL` | Public tunnel URL (set inside the sharefile plugin process) |

**Readiness** follows the MCP standard: a plugin is ready when its `initialize` handshake completes — the server only answers it after its own initialization (FastMCP lifespan) succeeded, during which the plugin establishes its own serving capacity. There is no `__ready` probe tool. The lifespan stays **handshake-fast**: heavyweight startup (e.g. a plugin's semantic index, the job-coding LLM client) is deferred to `warm_after_handshake`, which runs **after the first `tools/list`** (a short delay, then fire-and-forget) — never inside the lifespan. External/subordinate dependencies never gate readiness: they are uncontrollable, self-heal at runtime, and are surfaced separately via status tools.

**Required plugins.** `plugins.required` in `slife.json5` (empty by default; the shipped config sets `["memdb", "memfiles"]`) are core: failing to become ready **aborts startup** with an error instead of limping on. The spawn hang-guard is bounded by `PLUGIN_SPAWN_TIMEOUT` = **60 s** (a 30 s cap previously misfired on slow machines). The service opens for user input only once every plugin spawn has converged (ready / skipped / failed), so input can never race ahead of plugin startup.

**Watchdog (auto-restart).** Each plugin runs with a watchdog background task that monitors the child process and auto-restarts it on unexpected exit:

| Feature | Detail |
|---------|--------|
| Detection | `await subprocess.wait()` — blocks until the child exits |
| On crash | Unregisters the plugin's exact registered bare-name tools (plus any registry tools bound to the dead client), disconnects the dead client, then re-runs the uniform start (skipping the first-start gate) |
| Backoff | Exponential: 1 s → 2 s → 4 s → … → 30 s max |
| Max restarts | 5 consecutive failures → the watchdog gives up and logs an error |
| Counter reset | **Only when the crashed child had stayed up ≥ `_WATCHDOG_STABLE_UPTIME` (60 s)** — a fast boot-loop is deliberately NOT reset, so a crashing plugin accumulates toward the cap |
| Scope | Every plugin — built-in, the **mcp-gateway**, or an auto-discovered one. A restart re-runs the uniform start and tells subagents sharing the plugin its new port |

Subagents do **not** have their own watchdog — they connect to the main agent's plugin processes via HTTP, so a subagent crash only kills the subagent, not the shared infrastructure.

### Localhost Never Goes Through a Proxy

Every local `MCPClient` connection is loopback — the harness connects only to `http://127.0.0.1:{port}/mcp` (main process ↔ local plugins, and subagents ↔ the shared local plugin). Loopback traffic must therefore **never** consult the OS proxy. The MCP SDK's `streamable_http_client` builds a default httpx client with `trust_env=True`, which reads the OS proxy configuration (Windows system proxy, or `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`) and applies it to *every* request — `127.0.0.1` included. On any machine with a proxy configured, the plugin's connect / `tools/list` requests got routed through the proxy (typically `502` for loopback), the connect retry burned up, and plugins never appeared ready — a latent bug for any user with *any* proxy configured.

**Fix (2026-08):** `MCPClient.connect` supplies its own `httpx2.AsyncClient(trust_env=False)` — the provided client is owned by `MCPClient` and closed in `_cleanup`. `trust_env=False` also drops `NO_PROXY` on these connections, deliberately: nothing a `NO_PROXY` list would legitimately exclude for loopback-only traffic.

**Scope — external MCP servers are nuanced.** The gateway's `connection.py` SSE-first path keeps the SDK's proxy-reading default; the Streamable-HTTP **fallback** path explicitly builds `httpx2.AsyncClient(trust_env=False)` when the server is URL-routed. A remote server that genuinely needs the proxy should be configured deliberately; loopback-local behavior is proxy-free everywhere. Regression test: `TestMCPClientConnect::test_connect_passes_proxy_free_http_client`.

### The built-in plugins (internal only)

| Plugin | Transport | Role |
|--------|-----------|------|
| **mcp-gateway** | Streamable HTTP | Gateway for external MCP servers (stdio / SSE / Streamable HTTP) — a built-in plugin (`slife.plugins.mcp_gateway`). Manages connection lifecycle, keeps an in-memory **tool catalog** of every loaded tool, searched schema-aware and loaded on demand (see below). |
| **memdb** | Streamable HTTP | Turns database (backing table `diary`). Hybrid search (FTS5 + vec0). Turn persistence, session restore, embedding configuration. |
| **wechat** | Streamable HTTP | Bidirectional WeChat messaging via iLink ClawBot. Long-poll loop for incoming messages (a failed poll backs off the next poll exponentially to 30 s and resets on the next clean poll), typing indicators. Incoming messages enter the inbox as WeChat-channel turns prefixed `[Wechat:{...}]` (model-facing JSON carrying `peer_wechat_id` / `context_token`; the TUI strips the marker — the `Wechat>` bubble prefix already shows the channel). The model replies itself via `wechat_send_message` — no harness auto-dispatch. |
| **memfiles** | Streamable HTTP | Private notes/diary/files/reports cabinet — see [Part 6 · The File Cabinet](#the-file-cabinet-memfiles). Owns the scheduled-task *data* tables; the schedule *tools* are native (Part 2 · Scheduled Tasks). |
| **sharefile** | Streamable HTTP + `/share` route | Public file sharing — LLM-visible tools `share_file` / `sharefile_unshare`; internal `__check`, `__register_file`; `GET /share/{token}` serves file bytes on the same port (one port, two protocols), stat-pinned to the registered file so a share never silently serves replaced content. Shares are in-session only. Owns the pluggable tunnel (provider from `sharefile.json5`'s `active_provider`; eager start, non-blocking). |
| **a2a** | Streamable HTTP | A2A mesh over the MQTT binding (paho-mqtt v5, LWT). Only starts when the broker is reachable (TCP probe). Hosts the LLM-visible `a2a_*` tools (see Part 7). |
| **media** | Streamable HTTP | Non-chat AI generation (image, video, TTS, ASR) from any provider. Owns the `media:` config section (plugin-read, ignored by the main `Config` parser) and a provider-agnostic adapter layer (`dashscope-aigc`, `openai-images`). Tools: `generate_image`, `generate_video`, `text_to_speech`, `transcribe_audio`. Long renders use the harness's universal `_async: true` + `check_async`. Artifacts are saved to the working directory (or a `folder` passed to the tool) — work products, never memfiles cabinet files. |
| **job-coding** | Streamable HTTP | Deterministic Jobs as MCP tools — see [Job System](#job-system-job-coding). Tools: `job-list`, `job-write`, `job-remove`, `job-run` + one tool per job. |

The sharefile tunnel is **pluggable** (`sharefile.json5` names `active_provider`): every provider presents one surface (`start`/`stop`/`is_active`/`status`/`share_url_for`/monitors) and shares one lifecycle (`_TunnelProviderBase`): single-flight start guard with stale-start supersede (45 s), 3 retries with linear backoff, the `active`/`starting`/`failed`/`idle` state machine, and a background health monitor. Providers: `ngrok` (default, official SDK, endpoint pooling, free-tier splash for browser User-Agents), `localhost.run` (`ssh -R`, no account, rotating `*.lhr.life` host), `cloudflare` (`cloudflared tunnel --url`, no account, stable-for-process URL, binary not bundled — a missing binary is a terminal `failed` state carrying an install hint). A **missing dependency** is terminal and never retried; a **transport failure** is retried, and free-tier sessions recycle so the monitor keeps restarting the tunnel in the background. The plugin always loads — the tunnel is a subordinate dependency that never gates readiness.

### The MCP gateway

Three wire transports, one connection class (`MCPServerConnection`) built on the **official MCP SDK `ClientSession`** — the same SDK mechanism `client.MCPClient` uses to reach slife's own plugin children. The class supplies the lifecycle the SDK does not: OAuth device flow, transport selection, health monitor (ping + reconnect with backoff), stdio stderr relay, per-server connect locking, and the `needs_user_auth` pause.

| Transport | Mechanism | Use |
|-----------|-----------|-----|
| **stdio** | SDK `stdio_client` (JSON-RPC over pipes) | Local MCP servers (npx/uvx/bunx) |
| **http (SSE)** | SDK `sse_client` (GET with `Accept: text/event-stream`, POST to message endpoint) | Remote SSE endpoints (tried first for URLs) |
| **http (streamable)** | SDK `streamable_http_client` (POST JSON-RPC + `mcp-session-id` header) | Remote Streamable HTTP endpoints (fallback) |

For `url`-configured servers the gateway tries the SDK `sse_client` first: a non-event-stream reply makes its `enter` fail and the connection falls through to `streamable_http_client`. A Streamable response may be a single JSON body or an SSE stream — `ClientSession` parses both, delivering server-initiated `tools/list_changed` notifications to the host.

**`tools/list_changed` dispatch.** Notifications to connected hosts are **coalesced and sent from a detached task** — never inside a request handler's task/cancel scope. (A slow control tool previously held the session open in its own scope while its connect emitted a ~50-message burst; the burst interleaving into that scope desynced mcp 2.1.1's cancel-scope stack and crashed the session — every later MCP call died with `Session not found`. Coalescing folds bursts into one send with trailing-edge re-sends; `_active_sessions` is bounded.) Applied to `mcp_gateway`, `host_server`, and `job_coding`.

**Tool catalog & on-demand loading.** The gateway keeps every loaded external tool in an **in-memory** catalog (SQLite `:memory:`): one row per `{server}__{tool}` with name, description, the tool's **complete `tools/list` schema** as one compact JSON text, and a per-tool `enabled` flag derived from its server's state. The catalog is created at wrapper load and re-synced from the live connection pool on every (re)connect — by construction identical to what the runtime can use; nothing persists, so it can never drift or go stale. It is indexed twice — FTS5 (keyword, over name/server/description/schema text) and f32-BLOB vectors (semantic) produced against the **host-provided** embedding endpoint (slife.json5's top-level `embeddings`, passed via the `initialize` handshake — the gateway has no `embeddings` section of its own, and stale hints telling users to add one to `mcp-plugin.json5` have been removed). The semantic vector sources the **schema text alone** (name, description, each parameter, return description), making `mcp_tool_search` **schema-aware**; it runs hybrid/keyword/grep retrieval, degrading to keyword-only while indexing.

External tools are **loaded on demand**: the host registers none by default. The model discovers a tool with `mcp_tool_search` and loads it with the native `mcp_tool_load(full_name)`, which fetches the live schema + enabled state via the internal `__mcp_get_tool` and refuses a disabled tool. A server with `auto_load: true` keeps the older wholesale registration. Enable/disable is always **server-granular** (`mcp_set_enabled`): disabling a server disconnects it, marks its catalog tools disabled (refused at call time), and drops its loaded proxies. Every `tools/list_changed` runs a host-side reconcile (`_sync_mcp_proxies`) that validates each loaded proxy and unregisters any whose tool vanished, server disconnected, or was disabled. There is deliberately no offline rebuild command — the catalog syncs itself from the live connections.

Exposed management tools: `mcp_set`, `mcp_set_enabled`, `mcp_remove`, `mcp_list`, `mcp_list_tools`, `mcp_tool_search` (LLM-visible); `__check`, `__mcp_call_tool`, `__mcp_get_tool` (internal). `mcp_list` is a **static config view** — what is configured, with no live state and no secrets; `check_mcp_gateway` (also run by `system_health`) calls the internal `__check` for the raw live state (servers + semantic block) and adds health levels with remediation hints. The separation keeps "what is configured" distinct from "what is connected".

Server lifecycle:

```
disabled ──[mcp_set_enabled(name, enabled=true)]──→ enabled (connected, catalog tools enabled)
enabled  ──[mcp_set_enabled(name, enabled=false)]─→ disabled (disconnected, catalog tools disabled)
enabled  ──[mcp_set(changed config)]───────────────→ restarted with new settings
```

All state changes persist to `mcp-plugin.json5` (self-hosted by the gateway). Servers needing OAuth use a device-code flow; tokens are stored in the credential store (`mcp_oauth_*`).

### Job System (job-coding)

DESIGNER_NOTES §6.7 — *"大模型越聪明，越需要一个 Job System"*. A **Job** is a plain public function in `<data_dir>/jobs/*.py` (dev: `<project>/jobs/` — the repo's committed `jobs/` holds the bundled `translate`/`summarize`/`total_tokens` samples; prod: `~/.slife/jobs/`, seeded from those by the installers). Following standard MCP tool norms, the function's `__name__`, docstring, and typed signature become the job tool's name, description, and parameters schema. **The files are the source of truth — there is no job-config file**: a restart (or the watchdog) re-scans the directory and re-registers the tools. Creating/editing a job is *coding*: the `job-coding` skill in `skills/` is the authoring guide.

Execution is deterministic: the tool calls the job function with exactly its declared arguments; the only LLM access is an explicit `llm.chat(system=…, user=…, model=…)` one-shot on `job_coding_model` — a **top-level** `"provider/model"` ref in slife.json5 that reuses `models.providers` and is independent of `active_model`. It should name a *different* (usually smaller/faster) model: a nested one-shot job call neither churns the agent loop's prompt-cache prefix nor competes for its quota. Jobs that call `llm` are `async def`; pure-computation jobs stay plain `def` (the runner runs sync jobs on a **daemon thread** via `slife.threads.run_daemon` — never `asyncio.to_thread`, whose default-executor workers are joined at exit and would wedge plugin shutdown on a hung blocking job; the runner captures `contextvars.copy_context()` so a sync job's `llm` client stays visible). No system prompt, no conversation history, no agent loop ever reaches a job's model — a structural guarantee.

**DB-locating jobs.** `jobs/total_tokens.py` reads the memdb DB agent-scoped: `$SLIFE_AGENT_NAME.db` (fallback `slife.db`) in the data dir — verified by a `diary`-table probe — then any memdb-shaped `*.db` near the data dir / cwd, newest first.

A job that needs an **external capability** reaches it through the `mcp` handle: `from slife.plugins.job_coding import mcp`, then `await mcp.call(server, tool, args)` for ONE bare tool call (`server` a name from `mcp-plugin.json5`, `tool` without the `{server}__` prefix). The handle is a lazy proxy to the **mcp-gateway**: it forwards to the gateway's internal `__mcp_call_tool` — the same call shape the host's proxies use. Nothing external is ever spawned a second time: jobs ride the gateway's persistent connections, so a job can use **any tool on any connected server, loaded or not**. Port discovery is lazy and layered: the host pushes the port on every gateway connect/reconnect (`__set_mcp_gateway_port`), the spawn-time `SLIFE_MCP_GATEWAY_PORT` env is the fallback, and a port change drops the cached client. `mcp.call` returns the tool's text or a deterministic `Error: ...` string — it never raises.

**Management tools** (bare names, `DirectRoute`): `job-list`, `job-write` (writes `<name>.py`; (re)registers immediately, persists across restart, rolls back to the previous code on a broken write), `job-remove` (delete file + unregister), `job-run` (generic executor by name — also the execution path before the harness resync picks up a brand-new per-job tool). After any tool-set mutation the plugin pushes `notifications/tools/list_changed`; the host's generic `_rescan_plugin_tools` re-lists and diff-registers — the same dynamic-tool mechanism the mcp wrapper uses, so per-job tools appear/disappear live. Watched by the uniform watchdog and covered by `system_health` via `check_job_coding` (probes the internal `__check`).

### Subagents (local workers, not A2A)

Local child-process workers, always available — no config toggle. A subagent (agent worker) is **not** an A2A peer: no network identity, no presence, and no mesh tooling of its own.

- **headless.py**: Slife without TUI, worker-scoped JSON-RPC 2.0 over stdin/stdout — request methods `worker/send`, `worker/cancel`, `worker/plugin_restart`, `context` (cloned parent history), `shutdown`; notifications `worker/complete`, `worker/progress`; a `{ready: true}` result signals startup. Pure UTF-8 on stdout to dodge GBK.
- **SubagentManager**: spawn/stop/list lifecycle. **Names are explicit — a worker's name is its identity**; `spawn_subagent` requires a `subagent_name` (a short safe identifier) and never auto-generates one. `max_subagents` default 5, `task_timeout` default 120 s.
- **Serial processing + visibility**: a worker runs one task at a time. A sync `subagent_send_task` to a busy worker is automatically queued as async and reported (never a silent timeout, never a resend). `subagent_list_tasks` lists worker tasks across workers.
- **Async delivery mode**: `subagent_send_task_async` takes `mode="auto"` (default — the result auto-pushes to the parent's inbox, starting a new turn; **it also stays retrievable** via `subagent_get_task_result`) or `mode="poll"` (no push — retrieve explicitly). The mode is chosen at send time so the caller's intent is explicit.
- **Shared plugins**: subagents connect to the main agent's plugin servers (mcp-gateway / memdb / wechat / a2a / memfiles) via inherited ports — no isolation; they can send but never drain the inbound queue (all replies and management belong to the main agent).
- **Recursion**: subagents can spawn their own descendants (each level has its own SubagentManager).

## Part 6 · Memory, Search & Embeddings

> **Terminology.** The **Turns DB** is the memdb plugin's store, whose backing SQLite table is named `diary` — a codebase alias. It is distinct from the File Cabinet's **Diary** records that the memfiles plugin writes. In this part "diary" always means that table unless it names a cabinet file.

Every turn is permanently recorded as an independent row — no session concept, a continuous time-ordered log in `<data dir>/<agent>.db`.

**Memory is core — the agent never runs silently without it.** A fatal turn-save failure (the memdb plugin returns `{"error": …}` for a broken schema / corruption / disk error) is a hard stop, not a skip: `save_to_memory` sets the memory-broken state, **freezes the inbox** (queued turns are dropped, never run — a turn that can't be persisted isn't worth running), and the TUI shows a **persistent red banner** until the DB is fixed and the agent restarted. Transient MCP timeouts are only warned, not fatal. Restore-side failure is also fatal (startup abort — see [Session Restore](#session-restore)).

### Schema

`diary` table (`schema.sql`):

| Column | Purpose |
|--------|---------|
| `user_message` | What the user said |
| `messages` | Assistant response as OpenAI JSON array (thinking, tool calls, results, text) |
| `summary` | 1–2 sentence gist (LLM-written) |
| `tags` | Comma-separated topic tags |
| `created_at` | ISO 8601 with timezone (B-tree indexed) — user input time (Enter-press moment, threaded from the TUI) |
| `completed_at` | ISO 8601 — assistant completion time (captured after the final turn ensure, before the MCP save) |
| `channel` | Channel identity: `human`, `wechat`, `subagent`, `heartbeat`, `system`, or the A2A peer name |
| `who_helped` / `what_model` | Agent identity + model used |
| `token_count` | Cumulative billed tokens for this turn |
| `context_tokens` | Context size at the last API call = prompt + completion tokens (the persisted history the next request would re-send; restore primes `_turn_prompt` with it) |

There is **no `images` column** — image blocks live only in the in-memory user message and are never persisted (restore is text-only). Supporting structures: `diary_fts` (FTS5 content-sync table — the UPDATE trigger keeps `turn_summarize`'s summary/tags visible to keyword search), `diary_semantic` (sqlite-vec `vec0` table: embedding + rowid + chunk index + summary/tags/created_at), `diary_meta` (key-value store tracking the embedding model identity for migration detection), and `turn_channel` (a sibling row per turn holding the channel's JSON payload, written atomically with the diary insert). Turns are saved **unconditionally** after every turn (cancel, error, or max-iterations) via the internal `__memory_save_turn` tool; the save-side invariant is enforced by the harness (`_ensure_turn_consistent`) before the plug in sees it.

There is **no general migration layer** — backward compatibility is not supported (schema changes land in `schema.sql` for fresh DBs; reset the DB to upgrade). The one exception is the `prompt_tokens` → `context_tokens` rename, shipped as `scripts/migrate_context_tokens.py`.

Per-turn token consumption is queryable via **`turn_token_usage`** (`rowid`, `since`/`until`, `limit`) — each matching turn's `token_count` (billing) and `context_tokens` (context size) plus totals/averages.

### Search

Three indexes: FTS5 (BM25 keyword), sqlite-vec `vec0` (cosine KNN), B-tree on `created_at` (time range). All `since`/`until` bounds share one grammar via `slife.timeutil.normalize_time_bound`: an ISO datetime/date or the relative words `today` / `yesterday` / `tomorrow` / `now` (offset-aware inputs convert to local time). A bare-date `until` advances a day against a **timestamp** column (`created_at`), but not against a date-only column (memfiles `diary_list`).

| Mode | Best for |
|------|----------|
| `grep` | Exact strings — error messages, file paths, code |
| `fts5` | Topic / keyword search with ranked snippets |
| `hybrid` | Semantic recall (FTS5 + vec0 → RRF merge) |
| `time` | Browse by date |

Hybrid mode uses Reciprocal Rank Fusion (RRF, k=60). Without an embedding backend, hybrid degrades to FTS5-only gracefully (and reports its degraded mode + reason).

Search hardening: every `LIKE` path escapes `%`/`_`/`\` through a shared `_like_escape` helper paired with an `ESCAPE '\'` clause; **CJK queries** route keyword search to a LIKE substring fallback (FTS5 unicode61 can't segment Chinese); FTS5 MATCH operator words are quoted and stray symbols stripped so a user query can't crash the MATCH parser; `turn_count` honors `since`/`until` in fts5 mode; LLM-facing search clamps `limit` to `[1, 200]`; semantic search is gated on index completeness (`SemanticManager.semantic_ready`) — hybrid degrades to FTS5 while any turn lacks an embedding, and `turn_search` is a pure read of the gate (no reindex side effect).

### Embeddings & the SemanticManager

Embeddings are a **first-class top-level `embeddings` section** in `slife.json5`, shared by memdb + memfiles + the mcp gateway's tool catalog (the host passes its active endpoint to the gateway via the `initialize` handshake — the single source of truth), managed by the native `embeddings_model_list` / `embeddings_model_set` / `embeddings_model_switch` / `embeddings_model_remove` / `embeddings_enable` tools. The shape mirrors the LLM `models.providers` two-level hierarchy:

- **provider** = one OpenAI-compatible endpoint (`base_url` + `api_key`), with a `model` id.
- **`active_model` is a bare provider id** (e.g. `"local_embed"` or `"siliconflow"`), configuration-authoritative; the `"provider/model"` form belongs to the LLM `models` config, not here.
- **The dimension is deliberately not configured.** Resolution order: a known-model dimension table → the endpoint's `GET /v1/models` → a probe embed (`_probe_api_dim`).

The embedder (`EmbeddingClient`, `slife/plugins/memdb/embeddings.py`) exposes `available` / `loaded` / `dimension` / `max_tokens`; every embed is serialised on a per-client `threading.Lock` (`_embed_lock`); local-model backends (gguf/transformer, served via the local-embed daemon) run on daemon threads (`slife.threads.run_daemon`). A header-less `${VAR}` placeholder is skipped as a fake key; the API client uses a short timeout and zero retries so a blackholed endpoint degrades fast.

**Vector store.** `diary_semantic` is a sqlite-vec `vec0` table. One turn → multiple chunks (text split at paragraph boundaries, ~2000 chars ≈ 500 tokens, 1-paragraph overlap); the embedded text is the user message plus all assistant/tool contents. Semantic search dedupes by `diary_rowid`, keeping only the best (lowest-distance) chunk per turn.

**Write path is insert-only.** `save_turn` persists the row and never embeds on the save path (a slow GGUF embed of a large turn previously tripped the save timeout — a false alarm; the row was saved anyway). Embedding is an internal plugin concern: after each insert `__memory_save_turn` calls `manager.on_saved()` — a non-blocking `event.set()` that wakes the idle drainer. `turn_summarize` writes only the `summary`/`tags` columns — a recall clue for keyword search — and never touches the semantic index. A passed `rowid` annotates that specific turn; **omitting it captures the current (in-flight) turn**, and `save_to_memory` extracts the annotation and rides it onto the new row at save (`_extract_turn_annotation`), so the model can annotate the turn it is completing mid-loop with no `latest_rowid()` race.

**SemanticManager — the lifecycle actor.** `SemanticManager` (`semantic.py`) owns the binary gate, the embedder instance, and an event-driven index drainer as one object — the only place the gate is written. It is document-generic (a store contract: `count_unembedded` / `get_unembedded_docs` / `replace_embedding_chunks` / `reconfigure_for_embedding`), so memdb's `SessionStore`, memfiles' `MemfilesStore`, and the gateway's `ToolStore` each drive their own instance — the three gates are independent. **One implementation, one subclass**: memdb's `SemanticManager` is the base class; the mcp-gateway *subclasses* it, overriding the four hooks where the gateway genuinely differs — `_new_embedder` (to take the connecting host's embedding endpoint), `_start_enabled` (gate on a usable host-provided base_url), `_on_model_selected` (drop stale vectors via the gateway store's meta/drop contract), and `_unavailable_reason` (report "no embedding endpoint" clearly) — while sharing the gate, drain loop, no-progress bound and status readers verbatim (`_embed_doc` stays the base implementation; the gateway's catalog embeds each short tool schema whole via its own path). The gateway's tool catalog is the lighter variant: one tool = one embedding of its name + description + full schema (chunked at the model token limit for long schemas), vectors as f32 BLOBs matched by brute-force cosine in Python, and a model change drops the stored vectors before re-embedding.

The gate (`semantic_ready`) opens exactly when `embedder_ready ∧ count_unembedded() == 0`; there are no intermediate states. `enable(cfg)` / `disable()` are blocking config transitions (load model, migrate vec0 in place, start/stop the drainer); `on_saved()` is a non-blocking `event.set()` wake. The drainer loops: empty → gate ON, wait on the `asyncio.Event` (no polling); else → gate OFF, embed one batch (atomic `replace_embedding_chunks`). A persistently failing embedder is bounded by a no-progress limit → state `stalled`, and only `enable()` (a config change) resets it. The state machine (`disabled | loading | indexing | ready | stalled`) and a human `reason` are reported separately from the binary gate — each plugin's internal `__check` surfaces both to `system_health`. While the gate is OFF, hybrid degrades to FTS5-only with a hint naming the reason — partial semantic results are never served. The embedder is owned in-process, so the `python -m` double-module hazard that once left the gate stuck is structurally impossible.

**Model / dimension change.** The `embeddings_*` native tools persist the top-level `embeddings` section and then hot-reload: they call the internal `__memory_reload_semantic` / `__memfiles_reload_semantic` tools, which `await manager.enable()` — stopping the drainer, migrating the vec0 table in place (`reconfigure_for_embedding` compares the vec0 `float[N]` width and the current model identity (`backend:model`, persisted in `diary_meta.embedding_model`; the endpoint is not included, so two providers serving the same model name are not distinguished) against what the DB was built with; a mismatch drops and recreates `diary_semantic`, since old vectors live in a different vector space), and restarting the drainer. `embeddings_enable(false)` calls `manager.disable()` instead. A failed reload degrades to "takes effect on restart" (never blocks the persist).

**Search.** `turn_search` has four modes; `hybrid` runs the FTS5 keyword query and a vec0 KNN side by side, then merges via RRF (k=60). sqlite-vec forbids auxiliary-column constraints or JOINs inside a KNN query, so the KNN runs alone, time-window filtering happens in Python (with a wider fetch pool), and `user_message` is fetched in a second query.

### Session Restore

On startup, recent turns are read **directly from SQLite** — no MCP transport, no plugin dependency. The UI rebuilds the last session from the diary (user messages, assistant text, tool-call widgets — text-only, since image blocks are never persisted), and only then does the plugin spawn batch begin: restore completes before plugin startup. Restored messages carry their stored timestamps, matching live display.

**Turn headers on restore.** Each restored user message gets a compact `[INFO: {"turn_id": N, "begin": …, "end": …}]` footnote regenerated from persisted columns (rowid + begin → end). The footnote is **runtime-only and never persisted** — the DB carries the clean original in both paths. Heartbeat turns are excluded. The current in-flight turn carries none — a missing footnote is the "current session" signal.

**The boundary replays the exit-time context.** `diary_meta.context_start` (an exclusive rowid) marks the live-context boundary: the internal trim advances it past every turn it evicts, and `clear_context` flushes it past the whole in-context slice — one cut-op for both. Turns after the boundary are returned **verbatim — no ceiling re-slicing**: the boundary already encodes the trimmed state. `get_recent_turns` returns `(turns, skipped=0, budget=0)`; the only cap is a defensive 2×-ceiling guard against a stale `0` boundary. The just-restored history is exempt from the first-turn trim. Turn headers are re-appended to restored, non-synthetic turns; every restored turn is run through `_ensure_turn_consistent` before the UI is built. The restored turn prompt is primed with the **latest restored turn's persisted `context_tokens`** — the exact context size at exit — so the first `_turn_prompt`/status bar shows real occupancy. A missing/zero value falls back to the token estimate.

**Restore failure is fatal, never silent.** A present-but-broken memory DB raises `MemoryDatabaseError` instead of returning `[]` — the TUI shows the error and **aborts startup**. Required plugins that fail to *load* (including the bounded 60 s spawn hang-guard) likewise abort startup, stop all plugins, and exit.

### Agent Isolation

`--agent alice` uses `<data dir>/alice.db` (`~/.slife/alice.db` in production) — isolation is at the database-file level. Each agent has its own diary, FTS, and vector indexes; nothing is shared between agents (it also gets its own `alice.files` cabinet and A2A mesh name).

### The File Cabinet (memfiles)

A standard Streamable HTTP plugin — self-contained and replaceable exactly like memdb / media. **Four** typed knowledge stores, each **dual-written** to a human-browsable markdown file and a SQLite index (`<agent>.files/.index.db`):

- `note_save(subject, …)` — a note keyed by **subject**, appended to `notes/<subject>.md` (each call adds a timestamped section);
- `diary_write(date, …)` — a day's entry keyed by **date**, appended to `diary/<YYYY-MM-DD>.md`;
- `file_save` / `url_save` — saved attachments under `files/<category>/` (bytes stay on the filesystem), auto-filed by extension (images / documents / archives / code / audio / video / data / other) with an optional `category` override; an LLM `summary` given at save time makes them semantically searchable (one pass — no separate summarize tool);
- `report_save` — scheduled-task reports under `reports/<slug>.md`, with FTS5 + vec0 indexes via the `_KIND_SPECS` extension.

Each kind owns its FTS5 + vec0 tables. `cabinet_search(query, kind, mode)` runs hybrid (FTS5 + vec0 KNN, RRF via the shared `merge_hybrid`) or keyword search across them; `cabinet_read(path)` re-opens a file with a path-traversal guard. Browsing by key: `note_list` / `diary_list` / `note_read` / `diary_read` / `list_files`, plus the report trio. The index mirrors memdb's design and **reuses its code**: the shared `SemanticManager` drives the drainer over all kinds, and each plugin's `__check` reports its own gate — independent because each plugin reindexes its own DB (one shared top-level `embeddings` config, independent availability).

`url_save` guards against SSRF **before fetching — and re-runs the guard on every redirect hop** (bounded 5 hops): every resolved address must be globally routable, so loopback / private / link-local / cloud-metadata (`169.254.169.254`) targets are refused. One deliberate exception: the documented **fake-ip pools** — Clash/sing-box `198.18.0.0/15` and sing-box's IPv6 `fdfe:dcba:9876::/48` — are accepted, because those resolvers answer real public hostnames with addresses from them; any *other* private answer is still refused.

All save tools return the saved **local path** (clickable) — they never auto-publish, so nothing is registered in any token registry as a side effect of saving. Publishing is always the LLM's explicit choice, via the separate sharefile plugin's `share_file`.

### Images, Vision & the @-syntax

Users attach images with `@` directives — **one `@` = one image source**, any number per input, parsed independently. The user message stays verbatim (the `@` reference remains visible like any text); the extracted sources are handed to the loop, which **auto-invokes** `attach_image` once with the whole `sources` list via the harness-call machinery (`_auto_invoke`, same as `_turn_prompt`) — a single history shape (one assistant tool_use + result pair), no LLM iteration spent deciding to attach.

**Shapes.** Each `@` is followed by exactly one source:

| Shape | Example |
|---|---|
| Bare path | `@D:\photos\a.png` |
| URL | `@https://example.com/x.png` (no extension / query / fragment OK) |
| Data URI | `@data:image/png;base64,AAAA` |
| Quoted (spaces OK) | `@"D:\my photo.png"` / `@'a.jpg'` |
| Bracketed | `@[D:\a.png]` / `@{a.png}` / `@(a.png)` |

Multiple `@` may sit **adjacent without spaces** — `@a.png@b.png`, `@a.png @b.png`, and `@https://a.com/x.png和@http://b.com/y.png` each yield two sources. Quoted/bracketed forms read the inner content (spaces allowed); bare tokens run to whitespace, a quote, or the next `@`.

**Shape gating.** A bare path must end in an image extension (`.png .jpg .jpeg .gif .webp .bmp .svg .ico .avif .tiff .heic`), so `@someone` (no extension) is skipped as plain text. **URLs and data URIs are self-identifying via their scheme** — no extension gate — so `@https://example.com/photo`, query strings (`?v=2`), and fragments (`#x`) are all valid. **No filesystem check here** — existence is validated downstream by `attach_image` (it reads the file or returns an error).

**Boundary characters (token-end).** The extract regex ends a token at: whitespace, a quote (`"`/`'`), and `@` (universal — so adjacent directives split cleanly); **CJK characters** (a natural word boundary when typing `@a.png和@b.png`); and **comma — URLs only** (`@url,@url` separates; a URL with a comma in its query is truncated — percent-encode instead). **Data URIs keep commas** (base64 payload is `,`-heavy). A bare URL must not contain raw CJK (`@https://example.com/photo?v=我` truncates at the CJK) — percent-encode the value or wrap the whole URL in quotes.

**Parsing: two-phase regex.** `slife/ui/app.py` parses with **locate then extract**, deliberately not a single-line grammar: `_AT_RE` finds every `@`, then `_SOURCE_RE` matches a source pattern on the slice after each. Locating-then-slicing is more robust than one regex against special characters (spaces, CJK, commas) that would corrupt a single `\S+` token, and keeps the existence check in one place downstream. A non-matching `@` (e.g. `@someone`, an unclosed quote) is skipped whole — the text stays, nothing is attached.

**Pipeline.** `include_image_urls()` (`slife/agent/multimodal.py`) turns each source into a vision content block (URLs pass through, local files base64 as `data:` URIs), returning `(blocks, failed)` — valid blocks are injected into the in-memory user message in one shot, failures are reported in the tool result. Exact duplicate sources are deduped (order-preserving). Blocks are **live-session-only**: never persisted, restore is text-only. Each backend converts blocks to its wire format (Anthropic `image.source`, Responses `input_image`). Images are never rendered in the terminal — the model reads them, and the user opens files with the OS default app or a `share_file` link.

## Part 7 · A2A — Agent-to-Agent (mesh)

The A2A protocol (JSON-RPC operations `SendMessage` / `GetTask` / `CancelTask` / `SubscribeToTask`, and Message/Task/AgentCard data shapes mirroring the official a2a-python reference interface) runs over a pluggable transport **binding** — currently MQTT. The **`a2a` plugin** owns the mesh: it hosts the LLM-facing `a2a_*` tools and the `A2AClient`.

```
  a2a_send_task / a2a_list_agents / …   (LLM tools, hosted in the a2a plugin)
         │
   a2a plugin (slife.plugins.a2a)
         │  A2AClient (official operations + data model)
   MQTT binding (paho, LWT) — the transport
```

Only MQTT is implemented. A `transport` other than `"mqtt"` in the `a2a` config section disables A2A with a warning at config load instead of crashing startup. The LLM-facing tools: `a2a_send_task`, `a2a_send_task_async`, `a2a_send_message`, `a2a_send_message_async`, `a2a_get_task_result`, `a2a_cancel_task`, `a2a_list_agents`, `a2a_list_tasks`, `a2a_agent_card`, `a2a_broadcast` — one uniform prefix. Subagents are **not** part of A2A (they are local workers; see Part 5).

### MQTT Mesh

- Topics: `Slife/<agent_name>/presence`, `Slife/<agent_name>/tasks/inbox`, `Slife/<agent_name>/tasks/result`.
- Presence heartbeat every 15 s (configurable); peers silent for 45 s are pruned. LWT publishes `{"status":"offline"}` (QoS 1) so crashes are visible.
- Client id is `<agent_name>-<pid>` to allow multiple processes per agent id.
- Duplicate agent detection: after subscribing, the client listens 1.5 s for an existing presence with the same id and exits with a clear error rather than splitting the identity.
- Slife only **probes** the broker (TCP connect) — Mosquitto is started by the user; a failed probe means the a2a plugin is not started and this is reported via `system_health`.
- The mesh connects **eagerly** when the plugin starts so presence is announced at launch; a failed eager connect is tolerated and mesh tools attempt a lazy connect on demand.
- Peer presence **transitions** (online/offline/timeout) reach the LLM context: the plugin queues them; `AgentService._a2a_poll_loop` drains them, and `_turn_prompt` carries only *changes* (read-once) — the current roster stays queryable via `a2a_list_agents`, so a missed event never leaves the LLM with stale state.
- Async task results **auto-push by default** (`mode="auto"` — a peer's result arrives on `Slife/<agent>/tasks/result` → a completion enters the history: "Peer X completed async task (ID: …)"). `mode="poll"` suppresses the push; an auto-pushed result also stays retrievable via `a2a_get_task_result`, so a caller may poll it in the same turn.
- **Stateless message tools** (`a2a_send_message` / `a2a_send_message_async`) express the A2A standard's conversational message semantics — the same `SendMessage` wire, but **not recorded in the task store** (an unrecorded corr_id makes the store writes no-ops, so `a2a_list_tasks` / `a2a_cancel_task` never see messages). The plugin tracks outbound message corr_ids → peer so an async completion is framed as "Peer X replied to your message" instead of a task completion.
- **Channel A2A markers**: the sender's wire envelope carries `_slife.kind` (`"task"` or `"message"`). Inbound messages reach the model prefixed `[A2A:{"from": …, "task_id": …}]` — `from` names the sending peer (never the receiver); `task_id` present only for a task; auto-pushed async results use `[A2A-PUSH:…]` with the same shape. Both markers are machine-facing: the TUI shows `A2A(<peer>)>` and strips them.

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

Mesh tasks are tracked in memory (`TaskRecord`: id, agent, preview, status, transport, timings, result capped at 2000 chars; 500-record soft cap, terminal-first pruning). The store is **not persisted across restarts** — `a2a_list_tasks` after restart is empty by design. Worker (subagent) tasks are **not** in this store — they live in per-worker local records.

## Part 8 · UI, Config, Credentials, Health, Logging, Paths

### UI

Textual TUI with minimal chrome:

- **ChatView** — scrollable message container; printable keys redirect to the input.
- **UserMessage** — dim `[HH:MM]` (user input time) + prefix-styled user text.
- **AssistantMessage** — dim `[HH:MM]` (assistant completion time) on the response text — **not** before the thinking block, so a thinking-only message shows no time — plus streaming text with collapsible thinking blocks (Enter/Space toggle). Thinking text is truncated at 500 chars for display.
- **ToolCallWidget** — collapsible amber headers: status icon, label, primary-arg preview, iteration counter; Ctrl+Y copies the result.
- **StatusBar** — model name, thinking indicator, inbox state, last-call context tokens + usage % (per history, so a heartbeat turn never drags the human reading down).
- **ApprovalPrompt** — inline approve/deny row for `_approve: true` tool calls (Y / N / Esc), no modal.
- **ModelPicker** — the Ctrl+S emergency model switcher (binding rules in [Model Switching](#model-switching)).
- **Auto-restore** — rebuilds last session's UI from the diary on startup (see [Session Restore](#session-restore)).

Timestamps: user messages display `created_at`; assistant messages display `completed_at`. Both format as `HH:MM` same-day, `MM-DD HH:MM` same-year, `YYYY-MM-DD HH:MM` older. Live display and restore read the same stored values, so the rebuilt chat matches what was seen live. The status bar's token count is the **per-call** prompt tokens of the history's last API call — not the turn's cumulative sum (that sum is the assistant message footer).

All user-supplied text is rendered with `markup=False` to prevent `MarkupError` injection. The end-user keymap lives in the [README](README.md#keyboard-shortcuts); the design notes for the picker and approval bindings live in Part 4.

### Progressive Disclosure

Not all tools are in every request. Several categories use lightweight summaries:

| Category | Browse | Load |
|----------|--------|------|
| MemDB | `turn_search` | `turn_read` |
| Skills | `skill_list` | `skill_use` |
| MCP | `mcp_tool_search` | `mcp_tool_load(full_name)` |

### i18n

`slife/ui/i18n.py` — the bilingual layer (English + Chinese). Every user-facing string routes through `t(key, **fmt)`; the language is resolved once at import from the OS locale and overridable via `set_language()` (tests pin `en`). See [Language policy](#language-policy).

### Config & Credentials

**Two-Layer Architecture**

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

Secret resolution order is **shell env → credstore → literal default** — for both plain `${VAR}` and `${VAR:-default}` (the fallback form resolves the default from the shell env but still consults credstore first for the key; it is *not* "shell-only"). `keyring:service/key` URIs are accepted for `api_key` fields. Resolution is recursive over strings/lists/dicts; Slife itself never prompts and never reads credstore's cryptfile backup (see the README's three supported usage methods for keyring-less machines).

**Credstore Backend Matrix** — backend selection is **deterministic by platform** (`_init_system()` dispatches `os.name`/`sys.platform`/`is_wsl()`), no keyring priority auto-discovery:

| Platform | Backend | Mechanism |
|----------|---------|-----------|
| **WSL** | WslBackend | PowerShell bridge → advapi32.dll CredReadW/CredWriteW (C# P/Invoke) — shares the Windows CredMan store |
| **Windows** | WinVaultKeyring | Windows Credential Manager (Vault API, via keyring) |
| **macOS** (GUI) | macOS Keyring | Logon keychain via keyring ctypes shim |
| **macOS** (headless) | macOS Keyring + isolated keychain | `CREDSTORE_KEYCHAIN` or `~/.credstore/credentials.keychain-db`, auto-created via `security create-keychain` |
| **Linux** | KeyutilsBackend | Kernel persistent keyring via ctypes syscalls (zero deps) |

Anything else raises a clear "unsupported platform" error. On a supported platform whose backend is unavailable (e.g. keyctl blocked by policy), credstore keeps working in **cryptfile-only** mode — but Slife's own resolution reads the system keyring only and silently falls through to env vars, so the config layer is inert on such machines by design.

**Secret Sanitization** — the input and output gates are the authoritative trust boundary. A secret may appear as plaintext anywhere *inside* the process (tool internals, `conn.error`, log lines) — that is **not** a vulnerability by itself. Judge any finding by two questions: **(1)** does the secret reach the LLM context / history as plaintext? → blocked by the gates; **(2)** does it cross the machine trust boundary as plaintext (network egress, a public `/share`, anything readable outside the running user)? → a real security finding. Everything else — including plaintext in `~/.slife/logs/` (readable only by the running user) — is hygiene, not a security issue.

Two gates, single pattern-masking engine (`logfmt.sanitize_secrets`):

1. **Inbound** — `MessageHistory.add_user_message()` on every external message.
2. **Outbound** — `AgentLoop._execute_tools()` runs `sanitize_secrets` on **every** tool result before it enters the history (tool-call arguments are also masked at `add_assistant_message`, and the TUI's tool-call preview is masked too).

Known shapes: `sk-*`, `ghp_*`, `ya29.*`, `pypi-*`, `Authorization: Bearer` tokens, credential-named `key=value` pairs → masked as `<MASKED>`. The engine is pattern-based with bounded regexes (no catastrophic backtracking) and deliberately no generic hex/blob heuristics — an exact-match denylist from credstore remains a possible hardening. The honest boundary is "known-shaped secrets never reach the LLM".

**Config Sections** — `slife.json5` structure parsed by `Config.from_json5`:

| Section | Purpose |
|---------|---------|
| `env` | `${VAR}` references, applied to the environment at startup |
| `models.providers` / `active_model` | LLM providers (api_key, base_url, api, models[]) + the active `"provider/model"` ref |
| `job_coding_model` | Top-level provider/model ref for jobs (plugin-read, independent of `active_model`) |
| `agent` | `max_iterations`, `tool_timeout`, `context_floor`, `context_ceiling`, `tool_result_ceiling`, `memory_tool_result_chars`, `heartbeat_interval` |
| `tools` | Per-tool overrides (timeout, enabled) |
| `embeddings` | First-class embeddings config: `providers` (OpenAI-compatible endpoints), `active_model` (bare provider id), `enabled` — shared by memdb/memfiles + the gateway's tool catalog (host passes the active endpoint via handshake) |
| `wechat` | `enabled` toggle |
| `media` | Non-chat generation config (plugin-read, ignored by the main `Config` parser) |
| `a2a` | Transport binding, broker host/port, heartbeat, task_timeout |
| `subagent` | `max_subagents`, `task_timeout` |
| `cli_tools` | External CLI tool definitions (read by the CLI tools directly) |
| `plugins.required` | Required plugins (empty by default; the shipped config requires `memdb`, `memfiles`) |

External MCP server configs live in **`mcp-plugin.json5` → `servers`**, self-hosted by the gateway. REST-API registrations are also entries there (a server tagged `source.type == "rest_api"`); there is no top-level `rest_apis` section.

### Health Checks

Health checks fall into two categories. `system_health` runs them all together. Because the standalone checks are subsets of `system_health`, their schemas state that relationship explicitly — so the LLM never re-calls each `check_*` after running the aggregate.

**Static startup checks** — `check_external_deps()` in `slife/health.py` probes system tooling once at startup **on a daemon thread**, recording per-tool entries via `health.record()` (components `node` / `npm` / `bun` / `uv`); they surface through `system_health` via the startup-record merge. Missing deps are warnings, not failures — Slife still starts.

**Dynamic runtime checks** — spec-derived (`_SPEC_CHECKS` maps each plugin's health-check name to its client) plus two non-plugin checks:

| Check | What it monitors | Layer |
|-------|-----------------|-------|
| `check_memdb` | Database file + embedding backend (model, dimension, availability) | memdb plugin `__check` |
| `check_wechat` | Login status, session age, QR expiry | wechat plugin `__check` |
| `check_memfiles` | Cabinet connected? semantic index ready? | memfiles plugin `__check` |
| `check_local_embed` | Local embedding daemon online? model list? (probes the daemon's HTTP `GET /v1/models` — not a plugin) | local-embed daemon |
| `check_sharefile` | Tunnel online? URL? | sharefile plugin `__check` |
| `check_mcp_gateway [server]` | Gateway health + per-server diagnosis (optional `server` arg filters one) | gateway `__check` |
| `check_a2a` | Mesh connection + peer status | a2a plugin `__check` |
| `check_media` | Media provider availability | media plugin `__check` |
| `check_job_coding` | Jobs dir + registered job tools | job-coding plugin `__check` |
| `check_watchdog` | Auto-restart status per plugin, deduplicated from health records (latest per plugin) | Process layer |

Every plugin-backed check probes the plugin's internal `__check` tool, which reports only raw technical state (facts and measurements, like a physical-examination report) and **never triggers a connect**. The harness interprets those facts into health levels and remediation hints — a plugin `__check` has no levels of its own. The watchdog only monitors processes — it does not introspect application state.

### Logging Convention

Structured log lines: `event_name key1=value1 key2=value2 …` (see `slife/logfmt.py`).

- Event name: snake_case, past-tense for completions (`tool_done`), present-tense for state (`mcp_connected`).
- Levels: `debug` = per-request detail, `info` = lifecycle milestones, `warning` = recoverable, `error`/`exception` = hard failure (use `exception()` to keep the traceback).
- Every line that could contain user input, tool args, tool output, or subprocess stderr passes `sanitize_secrets()` before logging.
- Plugins inherit the session id and write to per-session files via `setup_server_logging`; their stderr is relayed by the parent at DEBUG.
- No diagnostics on stdout (reserved for the TUI and the plugin port signal).

**Sinks: log is for developers, TUI is for the user.** Three sinks, two audiences:

- **Session log file** (`logs/*.log`) — full truth: DEBUG+, every level keeps its real meaning. `warning`/`error` events are *never* demoted to `info` to hide them from the terminal — that corrupts the file and makes log-based diagnosis (or an LLM reading the log) see "all OK" when failures occurred.
- **Console (stderr)** — never emits: the main harness runs its stderr handler at `CRITICAL + 1` (a no-op), so the terminal belongs entirely to the TUI. Plugin/subagent processes run stderr at DEBUG — that is a diagnostic pipe to the parent, not a user terminal.
- **TUI** — a pure business channel, decoupled from logs. User-visible status is surfaced explicitly via `_show_system_message` / callbacks — never by leaking `logger.warning` to the terminal. The plugin never talks to the TUI: the harness owns surfacing.

Known gaps (tracked, not fixed): several call sites log raw user/tool/task content without sanitization, a few use prose instead of `key=value`, and noisy third-party loggers are silenced at WARNING (including the anthropic SDK's DEBUG trace that once wedged a subagent relay — see [A.8](#a8-bounded-relay-and-daemon-threads-the-stderr-pipe-wedge)).

### Dev vs. Production Data Directory

`slife/paths.py` decides where session data (config, `*.db`, `*.files`, `logs/`) lives. Two modes only:

- **Production** (default): everything under `~/.slife/`.
- **Dev**: the project root (CWD) — `pyproject.toml` beside the source tree.

`is_dev()` requires **both** conditions to hold: (1) the CWD's `pyproject.toml` declares `project.name == "slife"` (the CWD *is* the project root), and (2) the loaded `slife` package's parent directory **is the CWD** (the source `slife/` subdir of that checkout — an editable install, or `python -m slife` from the tree, both satisfy this). A production install always loads from a site-packages dir whose parent is never the CWD, so it stays production no matter where it is launched from — inside a checkout, or from the home directory (uv tools install under `~/.local` / `%LOCALAPPDATA%`). Either condition alone is ambiguous; both must hold.

## Part 9 · Project Structure

```
slife/
  agent/               # LLM interaction
    loop.py            #   Function-calling loop (streaming, concurrent tools, harness auto-invoke, context trim)
    service.py         #   Lifecycle manager (plugins, inbox, model switching, save_to_memory)
    message_history.py #   Message storage + history (OpenAI format, sanitization, _ensure_turn_consistent, turn/trim headers)
    llm_client.py      #   Backend router + StreamChunk
    system_prompt.py   #   Prompt rendering (static + dynamic Jinja2)
    templates/         #   agent.j2, subagent.j2, slife.j2, turn_prompt.j2, schedule.j2, schedule_trigger.j2
    llm_backends/      #   API backends: openai.py, anthropic.py, openai_responses.py
    inbox.py           #   Unified message queue + MessageHistoryStore
    plugins.py         #   Plugin spawn/stop + watchdog (PluginLifecycle), plugin_port_env
    multimodal.py      #   Image encoding for vision models
    heartbeat.py       #   Autonomous heartbeat scheduling
    schedules.py       #   schedule_loop, run records, startup sweep, trigger markers
    timer.py           #   [Timer] wake message helper (posted by wait_minutes)
  tools/               # Native tools (auto-discovered; 58 classes / 12 categories)
    base.py            #   Tool ABC + make_params/NO_PARAMS/require_params
    registry.py        #   ToolRegistry
    factory.py         #   Auto-discovery (pkgutil.iter_modules)
    context.py         #   ToolContext — runtime refs (registry, mcp_client, config, history)
    _config_io.py      #   JSON5 read/write helpers
    system.py          #   system_health, list_native_tools, async tasks, clear_context, set_max_iterations, notify_user
    exec.py            #   Shell, Python, package install (+ _kill_process_tree)
    mcp.py             #   mcp_tool_load — on-demand external tool loading
    schedule.py        #   Scheduled-task tools (scheduled_task_*/scheduled_run_* + run_schedule_now)
    skill.py           #   Skill management (SKILL.md)
    cli.py             #   External CLI tool management
    rest_api.py        #   REST API tool management
    subagent.py        #   Local worker tools (spawn/list/stop + delegation + task mgmt)
    models.py          #   Model management + attach_image (vision) + _turn_prompt (harness) + _ModelConfigTool base
    config.py          #   Config env var + native tool toggles
    credentials.py     #   Credential check/inject/uninject
    embeddings.py      #   embeddings_model_* — first-class embeddings section config
    timer.py           #   wait_minutes (pause the turn and resume automatically)
    user_prefs.py      #   add_user_pref (appends to USER.md)
  plugins/             # Built-in plugins (auto-discovered server.py packages) + the spec
    spec.py            #   PLUGIN_SPECS — the central spec table (single source of truth)
    memdb/             #   Turns database (server.py, store.py, search.py, semantic.py, embeddings.py, schema.sql)
    wechat/            #   WeChat messaging (server.py, client.py, config.py)
    memfiles/          #   Private notes/diary/files/reports cabinet (server.py, store.py, user_prefs.py, schema.sql)
    sharefile/         #   Public file sharing (server.py, config.py, providers.py = pluggable tunnel)
    a2a/               #   A2A mesh (server.py + A2AClient; MQTT binding)
    media/             #   Non-chat AI generation (server.py, config.py, adapters/ dashscope-aigc + openai-images)
    job_coding/        #   Deterministic jobs (server.py, runner.py, registry.py)
    mcp_gateway/       #   The MCP gateway — a built-in plugin
      server.py        #   FastMCP gateway server + tool-catalog/search/embeddings tools
      connection.py    #   ConnectionPool / MCPServerConnection (stdio/SSE/streamable)
      client.py        #   Streamable HTTP client (used by the harness to connect ALL plugins)
      config.py        #   mcp-plugin.json5 (servers, auto_load); REST-API tagged entries
      store.py         #   ToolStore — in-memory tool catalog (FTS5 + BLOB vectors, full-schema column)
      semantic.py      #   SemanticManager subclass (host-provided embedding endpoint)
      search.py        #   merge_hybrid (RRF)
      embeddings.py    #   EmbeddingClient (host override only)
      oauth.py / process.py / i18n.py / logging.py
      schema.sql       #   catalog DDL
  mcp/                 # Host-process MCP infra
    host_server.py     #   slife-as-plugin — in-process FastMCP exposing the live ToolRegistry
    tool_adapter.py    #   MCPProxyTool (bridges MCP → Tool ABC, ProxyRoute dispatch)
  subagent/            # Local workers (agent workers, not A2A)
    headless.py        #   Headless worker-scoped JSON-RPC process
    identity.py        #   SUBAGENT unified-inbox source sentinel
    process.py         #   SubagentProcess + SubagentManager
  ui/                  # Textual TUI
    app.py             #   Textual App, bindings, HistoryInput, StatusBar
    chat.py            #   Chat message widgets (clickable paths/URLs, collapsible thinking)
    handler.py         #   TUIHandler (bridges events → widgets)
    tool_display.py    #   ToolCallWidget + display helpers
    restore.py         #   Session restore (rebuilds UI from diary)
    approval_prompt.py #   Inline tool approval (Y/N/Esc, no modal)
    model_picker.py    #   Ctrl+S inline model picker
    content.py         #   Message content model
    i18n.py            #   t(key, **fmt) bilingual layer
    slife.tcss         #   Textual CSS
  config.py            #   JSON5 config parsing (models, env, plugins, embeddings, A2A, subagent)
  paths.py             #   Filesystem paths (dev vs prod, data dir, DB, memfiles, jobs)
  platform.py          #   OS detection, shell detection, process lifecycle, notifications
  logfmt.py            #   Structured logging + secret sanitization
  timeutil.py          #   Unified since/until search-bound grammar (normalize_time_bound)
  env.py               #   ${VAR} environment resolution
  schedules.py         #   croniter wrapper (validation, next-run, timezone policy)
  threads.py           #   run_daemon — daemon threads for blocking calls
  fifoset.py           #   Bounded first-in-first-out set
  server_utils.py      #   Plugin contract: create_plugin_server, run_plugin_server, is_internal_tool
  health.py            #   External dependency checks (node, npm, bun, uv)
  bootstrap.py         #   Logging setup, skill seeding, console restore
  os_detect.py         #   OS path detection for install scripts

credstore/             # Standalone package — cross-platform credential store (system keyring + cryptfile+backup)
cc-switch/             # Standalone package — generate ~/.claude/settings.json
local-embed/           # Standalone package — local OpenAI-compatible embeddings daemon (started manually)
skills/                # On-demand SKILL.md skills (seeded to ~/.slife/skills/)
jobs/                  # Bundled sample jobs (seeded to ~/.slife/jobs/) — translate, summarize, total_tokens
scripts/               # Standalone helper scripts (e.g. migrate_context_tokens.py)
```

## Appendix A · Design Decisions & Hard-Won Lessons

The body of this document explains *how* things work; this appendix records *why* a few load-bearing decisions are shaped the way they are, and the incidents that forced them. Each entry is small on purpose — the detail belongs next to the code.

### A.1 The mutable vs. immutable context split is prompt-cache-driven

Identity + world render once at startup and never change; the per-turn `_turn_prompt` pair is a message-stream tool pair, never a second `system` message. Both choices exist so the static prefix of every request stays byte-identical → the Anthropic prompt-cache breakpoint lands on the stable base prompt (Part 3). The harness is a *tool*, not system text, because injecting system text that changes every turn would evict the cached prefix.

### A.2 Every save path is a hard stop, not a skip

Memory save cannot silently fail: a turn that can't be persisted isn't worth running. `save_to_memory` runs unconditionally (finally), freezes the inbox on a broken DB, and aborts startup on a broken restore (Part 6). The flip side is equally deliberate: subordinate dependencies (external servers, tunnels, WeChat login, brokers, embedding daemons) *never* gate readiness — they are uncontrollable and self-heal.

### A.3 Deterministic execution is delegated, never inline

Scheduled tasks dispatch to a named subagent worker — the agent both starts and finishes each task visibly. Jobs are code-defined functions with exactly their declared arguments and a separate model (`job_coding_model`). Both choices exist because the agent loop is the wrong tool for deterministic work, and because a nested job call must never churn the loop's cached prefix (Part 3 / Part 5).

### A.4 Proxy-free loopback (2026-08)

Any OS proxy breaks local plugin connections (the SDK's `trust_env=True` routed `127.0.0.1` through the proxy → 502 → connect retry burn → plugins never ready). `MCPClient.connect` now supplies `trust_env=False`; the external-server Streamable fallback does the same (Part 5). Regression test: `TestMCPClientConnect::test_connect_passes_proxy_free_http_client`.

### A.5 The mcp `tools/list_changed` cancel-scope crash (mcp 2.1.1)

A slow control tool held its request scope open while a ~50-message `tools/list_changed` burst interleaved into that scope → mcp 2.1.1's dispatcher desynced its cancel-scope stack and every later call died with `Session not found`. Fix: notifications are coalesced and sent from a detached task (Part 5). Escape from the pattern — never emit async notifications from inside a request handler's scope.

### A.6 LiteLLM's empty-text placeholder poisoning

A gateway's prompt sanitizer rewrites `content:""` + `tool_use` into `[System: Empty message content sanitised to satisfy protocol]` in the **response** — a place Slife's outbound hardening cannot see. It persists into the diary and replays. Recognise this class: a provider-side defect that looks like a Slife bug (Part 3).

### A.7 The stall watchdog is an inactivity timer, not a total one

Bailian answered `200 OK` and then sent nothing for ~7 minutes. The fix wraps every `anext()` in an `asyncio.timeout` reset per chunk — a slow-but-live generation is never cut ("timer at the owner, no total"). A total cap remains opt-in (`stream_timeout`) (Part 2).

### A.8 Bounded relay and daemon threads (the stderr pipe-wedge)

A subagent hang traced to a single 315 KB anthropic SDK DEBUG line: it blew past the stderr-relay's buffer, the pipe filled, and the child blocked on log write — silent `LimitOverrunError` on the relay side. Fixes in `logfmt.py`: silence the SDK's noisy `_base_client` logger, cap relayed lines (and discard over-long protocol lines), and bound the sanitize regexes so they can't backtrack catastrophically. Companion rule: unbounded blocking calls (local-embed encodes, sync jobs) run on **daemon threads** via `slife.threads.run_daemon` — never `asyncio.to_thread`, whose default-executor workers are joined at exit and would wedge shutdown on a hung blocking call (Part 5).

### A.9 The 60-second spawn hang-guard

A 30 s cap on required-plugin spawns previously misfired on slow machines and aborted startup on a healthy install. The bounded guard is now `PLUGIN_SPAWN_TIMEOUT` = 60 s with heavyweight init deferred past handshake (Part 5). When tuning a startup timeout, the question is: *does the fast path finish on the slowest supported machine?*

### A.10 Model-picker bindings: priority, sync, and post-layout scroll

The Ctrl+S picker is an emergency escape, not a daily driver. Its bindings are pure priority (no `_on_key` overrides — they swallowed keys), the binding action is sync (awaiting the picker's future inside the key handler deadlocks Textual — the await lives in a background task), and the scroll happens after layout (Part 4).

### A.11 One embedding journey, three independent gates

Semantic availability is a binary gate, per store — `semantic_ready = embedder_ready ∧ count_unembedded() == 0`, with no intermediate states and no partial results. One `SemanticManager` implementation (memdb) is reused by memfiles and *subclassed* by the gateway (host-provided endpoint), so drift between the three is structurally impossible (Part 6).

## License

MIT