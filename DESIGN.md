# Slife — Design

> The design of the Slife codebase in one document, for people who will change the code: what each
> subsystem is, the mechanisms it is built from, and the invariants that must hold. Where this
> document and the code disagree, the code wins.
>
> Two documents sit beside it and are deliberately **not** duplicated here: **[README.md](README.md)**
> is the user's manual (install, configuration, the tool inventory, keyboard shortcuts);
> **[DESIGNER_NOTES.md](DESIGNER_NOTES.md)** is the author's own notebook — the philosophy, the
> trade-offs, the next refactor. Reference-grade detail (column lists, protocol tables, per-tool
> inventories) lives in the code and is cited rather than copied.

## Contents

1. [Orientation](#1-orientation) — what Slife is, the principles, the vocabulary
2. [The agent](#2-the-agent) — the loop, context, recall, prompts, timing, roles
3. [LLM backends](#3-llm-backends) — the router, the unified stream, the failure contract
4. [The tool system](#4-the-tool-system) — the ABC, the catalog, load/inject/evict, discovery
5. [Plugins](#5-plugins) — the spec, the lifecycle, the child contract, the gateway, jobs
6. [Subagents](#6-subagents) — the agent-worker model
7. [Memory](#7-memory) — the turns DB, search, embeddings, restore, the file cabinet
8. [The A2A mesh](#8-the-a2a-mesh)
9. [Surroundings](#9-surroundings) — UI, config, credentials, health, logging, paths
10. [Project structure](#10-project-structure)
- [Appendix A. Invariants](#appendix-a-invariants) — the rules that must not be broken

---

## 1. Orientation

### What Slife is

A Textual TUI around a streaming function-calling loop. The model picks from one unified tool
registry — builtin tools, built-in plugins' tools, user-written jobs and external MCP tools are
indistinguishable at the call site, all of them OpenAI function definitions. Every turn is persisted
unconditionally to SQLite, and the context the model sees each turn is *engineered* (§2.2–2.3)
rather than accumulated. Capability enters only through standards: every process component is an MCP
server, and third-party capability is an external MCP server.

```
┌──────────────────────────────────────────────────────────────────────┐
│  UI — Textual TUI                              slife/ui/             │
├──────────────────────────────────────────────────────────────────────┤
│  AgentService                                  slife/agent/service.py│
│  Unified inbox — human · wechat · heartbeat · system · a2a · subagent│
├───────────────────────────────────┬──────────────────────────────────┤
│  AgentLoop                        │  MCPClient                       │
│  streaming function calling       │  Streamable HTTP                 │
│  context rebuild · trim           │  tool proxy + adapter            │
├───────────────────────────────────┴──────────────────────────────────┤
│  ToolRegistry — unified OpenAI function definitions                  │
│  system (builtin · plugin) · job · external ({server}__{tool})       │
├──────────────────────────────────────────────────────────────────────┤
│  Plugins — child processes, each an MCP server over Streamable HTTP  │
│  mcp-gateway · memdb · memfiles · wechat · sharefile · a2a · media · │
│  job-coding · local-embed                                            │
├──────────────────────────────────────────────────────────────────────┤
│  platform · config · health · logfmt · paths                         │
└──────────────────────────────────────────────────────────────────────┘

Outside the process tree (the user starts and owns these):
  local-embed daemon · Mosquitto (the A2A binding) · external MCP servers
```

Two MCP directions, deliberately uniform. The main process is an **MCP client** to its own child
plugins; the **mcp-gateway** plugin is simultaneously an MCP client to external servers and an MCP
server to the main process. Slife can also collapse to a server: `slife/mcp/host_server.py` exposes
the live tool registry over MCP ("slife-as-plugin"), which is how a subagent reaches its parent.

### Design principles

1. **Minimum harness, maximum distance from the model.** Assume a capable model. The prompt and the
   harness are the smallest patch that keeps it effective. Tools are described by schema; usage
   lives in `description`, not in instructions.
2. **One registry, one wire.** Everything is an OpenAI function definition. A backend owns its own
   wire conversion; the loop never branches on which backend produced a chunk.
3. **The model never runs silently without memory.** Every turn is saved unconditionally. A broken
   memory DB is a hard stop, never a limp-along.
4. **Capability enters through standards.** Plugins are MCP servers; external capability is an
   external MCP server. There is no bespoke plugin API.
5. **Determinism where the model is the wrong tool.** Scheduled tasks dispatch to worker processes;
   jobs are code-defined functions. The model delegates, it never inlines.
6. **Fail-open for what you cannot control.** Subordinate dependencies — external servers, tunnels,
   WeChat login, brokers, embedding daemons — never gate startup. The core is core.
7. **Use the mature, official package.** Do not reimplement what a maintained library already does;
   adapt to a new upstream API rather than pinning an old version.

### Vocabulary

| Term | Meaning |
|---|---|
| **Turn** | One user→assistant exchange, persisted as one `diary` row. The unit of history, memory and trimming. |
| **Channel** | The sender identity of an inbox message: `human`, `wechat`, `subagent`, `heartbeat`, `system`, or an A2A peer name. Persisted with the turn; by default not part of the LLM context. |
| **Marker** | Machine-generated notation inside a raw message (`[Heartbeat]`, `[Schedule …]`, `[A2A:…]`, `[INFO: …]`) telling the model or the TUI what the text alone does not say. |
| **Recall** | The per-turn selection of history turns that becomes the context. Not an LLM tool — the harness calls it before each turn. |
| **Harness tool** | A `_`-prefixed, LLM-visible-but-reserved tool the loop auto-invokes: `_turn_prompt`, `_check_new_input`. |
| **Internal tool** | A `__`-prefixed plugin tool serving the main process, filtered out of the schema before registration. |
| **Plugin** | A child process declared by one row in the central plugin spec, speaking MCP over Streamable HTTP. |
| **Worker** | A subagent: a child process running the same loop with a declared, zeroed capability set. |
| **Silence contract** | A bare `.` assistant reply is silence — never rendered, from any turn source. |

### Language policy

**Model input is uniformly English**: the system prompt, harness and plugin tool schemas (names,
descriptions, parameter docs, result strings), job schemas and result strings, and logs. External
tools — MCP servers, skills, third-party commands — keep the language of their source; they are
opaque and pass through untranslated.

**The TUI is bilingual (English / Chinese) by OS locale.** Detection happens once at import, from the
OS itself: `GetUserDefaultUILanguage` on Windows (the C locale does not carry the UI language — an
English Windows in Spain reports `es_ES`), else `LC_ALL` / `LC_MESSAGES` / `LANG`. `zh*` → Chinese,
anything else → English, degrading to English on failure. `--lang en|zh` overrides it.
`slife/ui/i18n.py` is the whole layer: one `t(key, **fmt)` accessor over an `en`/`zh` table, no
catalogs. A missing key raises rather than rendering blank.

Key caps in the status bar (`Ctrl+C`, `Esc`, …) and notification *bodies* stay English regardless of
locale — translating a key label breaks the key→action scan, and a notification body is LLM- or
system-supplied text rather than Slife chrome. Tests pin the language to `en`.

---

## 2. The agent

### 2.1 The turn loop

One function-calling loop. Every tool is registered as an OpenAI function definition in a single
`ToolRegistry`; the model decides what to call and when. `AgentLoop.run` (`slife/agent/loop.py`)
drives the cycle, and `Inbox` (`slife/agent/inbox.py`) drives `run` once per queued message.

```
message posted to the inbox
  → per-turn context rebuild (keep ∪ recall — §2.3)
  → add_user_message()                            (secrets sanitized at this gate)
  → iteration loop:
      cancel check · cut-in check · refresh the injected tool snapshot
      → LLM stream → thinking / text / tool deltas → handler callbacks
      → tool calls? → execute the batch concurrently → continue
      → no tool calls? → return the reply text
  → save the turn to the diary (unconditional — cancel, error, max-iterations alike)
  → trim the context if it is over the ceiling         (§2.2)
```

- **Streaming.** Thinking and text tokens are delivered in real time through `AgentEventHandler`
  callbacks. Tool-call deltas accumulate across chunks and execute as one batch via `asyncio.gather`;
  approval dialogs serialize behind a lock.
- **Iteration limit.** `agent.max_iterations` (default 30; **0 = unlimited**) is checked live each
  iteration, so a mid-turn `set_max_iterations` applies immediately. Hitting it returns a cancelled
  result and notifies the handler.
- **Cancellation.** `Esc` sets a cancel event, checked before each iteration, after each stream, and
  before each tool batch.
- **Background execution.** A per-call `_async: true` schedules the tool as a background task and
  returns a task id; poll with `check_async`, cancel with `cancel_async`. The runner sanitizes
  secrets **at storage time**, and a failed async task surfaces with the `Error:` prefix — the same
  contract as a synchronous call. Results are pruned past a bound, so a very old poll can answer
  "Task not found".
- **Turn consistency.** `MessageHistory._ensure_turn_consistent()` enforces two idempotent
  invariants before a history is persisted and again on load: **no orphaned tool_calls** (an
  interrupted turn's call gets a synthetic `(Tool execution interrupted)` result) and **alternating
  roles** (a history ending on `user`/`tool` gets a closing assistant message). Two call sites only:
  `save_to_memory` and `restore_session`.
- **Why a turn stopped early** rides that closing assistant line, standardized as
  `(Turn interrupted, reason: esc)`. The reason is a short token, never provider text — the line
  lands in the model's context *and* in the diary, so the provider's message is never copied into
  it. Each layer labels what only it knows: the loop puts its own terminal state on `AgentResult`
  (`esc`, `max_iterations`), the inbox labels the failure it caught (`error (400
  invalid_request_error)` — HTTP status and the provider's code when the SDK exposes them, else the
  exception's class name one hop down its cause chain), and the save point forwards whatever it
  received into the repair. A repair on **load** has no reason to give — the process that knew it is
  gone — and reads `---`. A content-filter reject produces no closing line at all, because that
  turn is rolled back rather than saved.
- **The one rollback.** `pop_last_turn()` removes the last user message and everything after it. It
  is called from exactly one place — the inbox, on a **content filter** reject — and suppresses the
  save. Everything else keeps the turn and saves it: a malformed *request* (a part the provider would
  not read, an image it could not fetch) is not a bad history, and transient failures (5xx, rate
  limits, timeouts, 401/403) are not even about the payload. Filtered content is recognised by name
  rather than by status code — providers spell it differently (OpenAI/Azure `content_filter`,
  DashScope/Qwen `data_inspection_failed`, Anthropic only in the message text).
- **A rejected request still costs its attachments.** Any 400 drops the injected image blocks —
  `MessageHistory.strip_images()`, which is the only place a block lives. A block is session-only
  (there is no column), so it would otherwise ride every later request and be rejected there: one
  failed attach turned into a session that dropped every turn, from every source, until a restart. A
  rejected attachment is not kept. The TUI says the attachments were removed, so the model is not
  blamed for not seeing an image that is gone.

### 2.2 Context window management

Active history is kept between `context_floor` and `context_ceiling` (defaults 20% and 80% of the
model's `context_window`).

**Usage is measured, never estimated.** `context_tokens_for()` is the single source for the current
context size, resolution order: the history's last API call's actual prompt + completion tokens →
the restore-time value primed from the latest restored turn's persisted `context_tokens` → `0`. It
drives the per-turn prompt, the trim decision and the status bar. Estimates appear in exactly one
place — sizing what a recall may add to a context that has not been rebuilt yet — and are never
presented as usage.
Usage is tracked **per history**, because the main agent has one shared context that every channel
writes into while a worker gets a fresh one-shot history per task.

- **Trim** happens *after* a turn is saved, by which point the last API call's real usage is known.
  At the ceiling, `extract_oldest_turns` removes the oldest **complete** turns down to the floor,
  always keeping the current turn. It is internal — no tool call, no LLM-visible pair. The cut is
  announced by a runtime-only `[INFO: N oldest turns have been removed from context]` note appended
  to the last assistant message and mirrored in the TUI; the evicted ids are dropped from the
  persisted live-context list (§7.4) and the tracked "Context covers" range advances by the same
  count. A freshly restored history is exempt from the first-turn trim.
- **There is no summarization of evicted context.** Old turns leave the *context*; they stay in the
  diary forever. Recall is the only way to bring one back.
- **Tool result cap (a hard limit).** One tool result is truncated at `tool_result_ceiling ×
  context_window × 3` characters (default 20% of the window), with an explicit marker inside the
  output. Generous enough that a large-but-real file read is never truncated; it caps only outputs
  that could not fit the window at all.
- **Permanent-memory compaction.** At save, any tool result over `memory_tool_result_chars` (default
  8000) is persisted as a head+tail digest naming the original size and the tool to re-run. The live
  history keeps the full result — compaction affects only the persisted copy.
- **Truncation is announced inside the tool output**, never in the system prompt, so the model knows
  re-running retrieves the full version.

**Token counting is real BPE, not a heuristic.** `estimate_text_tokens` uses `tiktoken`
(`o200k_base`), and the whole estimator family — `count_tokens`, `extract_oldest_turns`, the trim's
stop condition, the recall budget — shares that one implementation so they cannot disagree. The
vocabulary is provisioned at install time and pinned via `TIKTOKEN_CACHE_DIR`, because tiktoken
fetches it over HTTP **with no timeout**: an unreachable fetch hangs the agent rather than failing.
Slife refuses to start on a missing or partial vocabulary rather than mis-count silently.

### 2.3 Recall — the context is selected

`agent.rebuild_message` (default **true**) makes the context **decided** rather than accumulated:
before every turn the agent says what to keep of the turns in hand and what to **recall** from
memory, and the turn runs on the two together.

```
run()
  ├─ recall step — once per turn, BEFORE the user message is added
  │    ├─ discriminator → {context, recall}    (one model call; never persisted)
  │    ├─ __memory_turn_recall(…) → turn ids to add, [] or None
  │    ├─ None / unfetchable / nothing asked → keep the existing context, continue
  │    ├─ union = kept ∪ recalled  (chronological)
  │    ├─ __memory_context_turns_set(union) | __memory_context_turns_clear()
  │    └─ history.rebuild_messages(kept ∪ recalled)
  ├─ add_user_message · attach_image · _turn_prompt
  └─ iteration loop
        └─ save_to_memory → the new rowid is appended to the persisted list
```

The step sits **before** `add_user_message` because the rebuild replaces the message list wholesale,
so anything appended first — the user message — would be destroyed. The live image blocks of the
turns it replaces go the same way: a rebuilt turn carries no blocks (§7.6). It also stays outside
the iteration loop. With the flag **false** the context grows append-only and the
trim bounds it; one persisted list and one save-append path serve both modes, so the flag flips with
no migration. What the flag never changes is the ceiling.

**The discriminator.** `_discriminate_recall` makes exactly one model call per turn. It is not in the
conversation and nothing it says is ever shown.

- **Sent**: the agent's **current context** — the live messages, system prompt included, with
  `rebuild_messages.j2` in place of the user message. That is the design note's shape
  ("判别器用当前上下文，user message 替换为 …"), and it is load-bearing twice over: the turn being
  recalled is usually a follow-up, and a follow-up names its subject only through the conversation
  in hand ("人工智能学院是什么时候成立的" after three turns about 首经贸) — a query written from the
  input alone drops that subject and retrieves nothing. And the decision's *first* field is answered
  from that same context: the ids a keep-list names are the ones in the `[INFO: …]` footnotes the
  model can read there, and it is what tells the model what it already has, so it does not ask to
  recall it again.
- **What the instruction states**: what the call decides (what to keep of the turns in hand, plus
  what to recall, with the turn running on the two together), the current input, how the query is
  matched (against stored turns — their user messages, the tools they called, their answers), one
  rule — *name what the turn needs, in the words a stored turn would contain* — and the reply
  surface itself (`system_prompt.RECALL_REPLY`), stated once in the agent. It cannot be read off a
  tool: the selector is an **internal** tool the model never sees, so there is no LLM-facing schema
  to quote. The loop is the only caller and its parser reads exactly these two fields, which is what
  keeps the two ends of this contract in step. When the store cannot be reached at all there is no
  call either — the availability check is the gate, so a turn is never spent asking a model to
  decide a recall that cannot run.

  The rule is carried by **one worked case per decision** — numbered 1–6, so the instruction's order
  *is* the enumeration and no mode has to be described in prose — each written as the reply itself,
  the JSON object the field list asks for, rather than as a shorthand for it. The three recall
  shapes ride three of them (a bare period at 4, a query at 5, a topic within a period at 6), so all
  six decisions and all three modes are shown without a case repeating another. A seventh case
  carries the one composition rule a worked case is uniquely able to teach: the follow-up whose
  subject came three turns earlier (`那人工智能学院呢？` → `{"recall": {"query": "首经贸 人工智能学院
  成立"}}`). Every bound in them is in `timeutil.BOUND_GRAMMAR` — an unparseable bound is answered
  as *no ids*, which no longer wipes the context but is still a wasted turn — and
  `test_every_worked_case_is_a_reply_the_loop_accepts` runs each example through the loop's own
  parser, so a case cannot drift from the spelling the parser accepts.

  Decision 5 covers the case the bare keep-all cannot: a turn the context has dropped is invisible
  from the context itself, so "the turns in hand are enough" reads as correct while the referent is
  gone.
- **Cost**: one context-sized call per turn — the pre-turn call is now about as expensive as the turn
  itself. That is what judging from the conversation costs; the log line's `msgs` / `chars` are what
  say whether it is being paid.
- **Not sent**: anything beyond that context. The runtime `_turn_id` on the message that opens each
  turn is stripped (the normal wire path pops it in `to_openai_messages`; this call does not go
  through that helper).
- **It never persists and never streams** — nothing it sends or receives touches the history, the
  diary or the TUI.
- **It degrades, it does not retry.** A timeout, a provider failure, or a reply that is not the
  requested JSON object all return `None`. Retrying would double the pre-turn latency of a call
  whose fallback — keep the context — is perfectly good.

**What a reply means — six decisions from two independent fields.** `context` is what to keep of the
turns in hand (`None` = all of them, `[]` = none, or the ids to keep); `recall` is what to add
(`None`, or the three search parameters). The two are decided separately, so the six decisions are
their six combinations and no mode has to be enumerated:

| keep | recall | new context |
|---|---|---|
| all | — | the turns in hand, untouched. The store is not asked. |
| some | — | those turns. |
| none | — | the system prompt alone. |
| all | ✓ | base ∪ recalled |
| some | ✓ | base ∪ recalled |
| none | ✓ | recalled |

`recall`'s own three shapes are the store's three branches: `since`/`until` alone is **time-only**
(the turns in that range, ranked by nothing but time — no similarity cap, because there is no query
to measure against); `query` alone is a hybrid search over the whole diary; `query` + a range is the
same search with both legs windowed. An empty-query branch must run **before** the hybrid legs —
they cannot express "no query": an empty query reaches FTS5 as `MATCH ''` (an error) and embeds to
noise.

**The union is what makes "keep this and add that" expressible** — and it is why an empty recall is
now *harmless*. Under the overriding selection every reply but `{}` discarded the context it
replaced, so a query that merely failed to match emptied it: the turn ran on the system prompt alone
and answered from nothing. `base ∪ ∅` is `base`. Clearing is therefore only ever the explicit
`"clear"`, and nothing a model gets wrong can empty the context by accident. `{"context": "clear"}` +
a recall is exactly the old behaviour, so the union is a strict superset of it.

**The recalled set — one fusion, three caps, one order.** The hybrid legs are FTS5 (with a LIKE
fallback for CJK, which FTS5's `unicode61` cannot segment) and sqlite-vec KNN, fused by reciprocal
rank fusion (`k=60`). The caps are `agent.recall_limit` (40 turns), `agent.recall_min_similarity`
(0.45), and a token budget: `context_floor` (20% of the window), narrowed to the headroom below
`context_ceiling` by whatever the decision kept. The floor is the *selection's* size, so it stays the
cap when nothing is kept — which is why the ceiling, not the floor, is the bound a kept context is
measured against: the trim compacts *to* the floor, so a live context sits at or above it for most of
a session, and subtracting the floor would grant no headroom and quietly make "keep this and add
that" unreachable. The similarity cap gates the **measured** `similarity`, never the fused
`rrf_score` — a fused score is a function of rank position and carries no magnitude to threshold.
Keyword-leg hits have no measured similarity and are **exempt**: an exact match is a stronger signal
than a cosine neighbourhood, and "no number" is not evidence against it. The caps are recall's own
configuration, never the discriminator's arguments — it chooses *what to look for*, never how much of
it to take, which is why they are absent from the schema it fills in.

**A keep-list is a statement about the context in hand**, read as an intersection: an id that is not
there names nothing, and is not a way to pull an arbitrary row into the context past every cap and
the budget. The ids are the ones in the `[INFO: …]` footnote of the message that opens each turn —
which is why **every** in-context turn carries one, autonomous turns included. Suppressing the
footnote on heartbeat / schedule / timer turns (the old rule) made them unnameable, so a keep-list
silently dropped them — including scheduled turns that did real work — with nothing in the
conversation to say why.

**The floor is calibrated, not chosen.** A cosine scale belongs to the pair that produces it — the
embedding model *and* the text the index holds — so `recall_min_similarity` is a measured number, and
it must be re-measured when either changes. It matters more than a tuning knob usually would because
the recalled set joins the context rather than replacing it: a floor below the noise band does not
degrade gracefully, it adds an arbitrary turn as though it had been matched, and the union then
carries it. The value in the config was measured on a recorded session — every relevant turn at
0.46–0.55, every irrelevant one at ≤0.45, and a query no turn answered topping out at 0.33, selecting
nothing.

The semantic leg's scale depends on what the index holds, which is not the raw turn: see §7.2 for
what `_turn_text_for_embedding` embeds and why tool *results* are absent from it.

**Order is chronological even though membership is by relevance**, because the list order is the
restore contract: a rebuilt turn must render byte-identically to the same turn restored. Both paths
share one builder (`messages_from_turns`) — a difference would cost a prompt-cache miss every turn.
Which is also why the rebuild is **skipped when the decision asks for exactly what is in hand**:
nothing was added and nothing dropped, so re-rendering the same turns from the store would cost a
round-trip, the turn's live image blocks and the prompt-cache prefix to arrive at the identical list.

**The recalled set is joined, not reconciled** — there is no incumbent to defend and no need to
exclude turns already in context: a turn the recall names that the decision also kept is *the same
turn*, and the union is by id.

**The store's answer is ids, or nothing, but never an error.** `__memory_turn_recall` returns the
turn ids and nothing else — a degraded semantic leg does not change the recalled set, so it is
logged rather than answered with. Everything that is not a fatal environment failure is answered as
**no ids**: a time bound the grammar rejects, a query the store cannot parse, an unexpected pipeline
failure. That answer is now safe by construction — no ids *adds* nothing, where under the overriding
selection it emptied the context. Two things are **fatal** instead — a store failure and an unusable
tokenizer, since every turn's cost and so the budget come from it — and both reach the harness as a
tool *error*, which keeps the context. Fatalness is no longer load-bearing for safety; it is kept
because a broken database and a missing vocabulary are real environment failures, and answering them
with a plausible-looking empty list hides them behind a turn that quietly ran without the history it
asked for.

**The model's own way into the Turns DB is separate.** `turn_search` (hybrid / fts5 / grep) and
`turn_list` (a time-windowed, paged browse) mirror the memfiles cabinet's `cabinet_search` and
`*_list` tools, and they only ever *read* — neither touches the context. The selector that does feed
the context is the harness's, and it is internal for exactly that reason: changing the conversation
under the model is not something the model asks for.

**What the decision does to the context.** The rebuilt set is the union above, built from the stored
rows by the same builder restore uses — so a context is reproducible from its id list, and a restart
renders what the live session rendered. Clearing is the explicit `"clear"`, and the persisted list is
emptied with it. Four things leave the context untouched instead: nothing was asked for; no reply
came back; the store returned `None`; or the turns cannot be fetched. Nothing was learned about what
the turn needs, so a guess is not an improvement on what is already there.

**Workers never rebuild** — a worker's history is one-shot per task, so there is nothing to select
from. The role decides this, not the config.

### 2.4 The system prompt

The prompt splits **identity** from **world** so each role reads one coherent document:

- **Identity** — `agent.j2` (main agent) / `subagent.j2` (worker): who the agent is. Role framing
  only; the one part that carries persona.
- **World** — `slife.j2`, `{% include %}`d by both: the runtime spec — context policy, host
  platform, workspace paths, marker expectations, the credential chain, tool naming, skills, jobs,
  subagents, and A2A info when configured. **Byte-identical in both roles.**
- **Dynamic** — `turn_prompt.j2`, rendered by the `_turn_prompt` tool once per turn (§2.5).

Identity + world are rendered once at startup and never change, so the static prefix of every request
stays byte-identical and the prompt-cache breakpoint lands on it. That is the whole reason the
per-turn status is a **message-stream tool pair** rather than a second system message.

Two derived rules: the world spec carries **project-specific facts only** — anything the model can
infer from tool schemas or training data does not belong; and the prompt **forbids nothing by list**.
`_`, `__` and the meta-parameters are each explained once, structurally, so the model reads the
mechanism rather than a denylist.

### 2.5 Channels, markers and harness tool-pairs

Three orthogonal notions describe how Slife introduces information on its own initiative: a
**channel** (the sender identity of an inbox message — recoverable from the message alone, persisted
with the turn, by default **not** part of the LLM context), a **marker** (machine-generated notation
inside a raw message, telling the model or the TUI what the text alone does not say), and a **harness
tool-pair** (a reserved `_`-prefixed tool the loop auto-invokes, contributing an assistant
`tool_call` plus its result to the history).

**A marker never determines a channel and a channel never forces a marker.** A scheduled task is the
canonical example: its trigger is a `[Schedule <name>]` marker riding the **system** channel, and its
completion arrives on the **subagent** channel carrying no `[Schedule …]` marker at all.

| Channel | Sender | Typical marker | TUI |
|---|---|---|---|
| `human` | the keyboard operator | — | `You> ` |
| `wechat` | WeChat peer | `[Wechat:json]` | `Wechat> ` |
| `subagent` | local worker completion | `[Subagent:{"subagent_name", "task_id"}]` | `Subagent(<name>)> ` |
| `heartbeat` | Slife — the periodic autonomous window | `[Heartbeat]` | trigger hidden; reply as `⚡ 自主` |
| `system` | Slife — schedule / timer triggers | `[Schedule <name>]`, `[Timer]` | trigger hidden |
| `a2a` | a mesh peer | `[A2A:json]` | `A2A(<peer>)> ` |

The **system** channel is never user input, and its turns are filtered from the TUI by both the
channel and the marker text. Classification helpers match on the prefix so live rendering and session
restore agree. Two `[INFO: …]` footnotes decorate messages that already exist rather than injecting a
turn: the **turn footnote**, appended to a user message after the turn saves so the next call can
reference the turn by id, and the **trim note** of §2.2. Both are runtime-only.

**`_turn_prompt`** (`slife/tools/models.py`) is the per-turn status prompt: current time, context
usage, changed model / CWD / shell, A2A peer presence events since the last turn, open failed or
missed scheduled runs, and the one-shot "system restarted" flag. It is a tool pair, deliberately, so
it persists and restores as a normal part of the turn — it must not live in the static system prompt,
where changing every turn would evict the cached prefix.

- **Injected by the loop, not chosen by the model**: `_auto_invoke` writes the pair into the history
  unconditionally at the top of every turn, computing context usage once and sharing it with the
  trim and the status bar.
- It executes the tool **directly**, not through the tool-execution path: no approval gate, no
  timeout wrap, no async wrapping.
- It must be a **schema-declared builtin tool**, not a history-layer fabrication — Anthropic and
  OpenAI-Responses reject a tool call in history whose name is not in the declared `tools` list.
- The harness tools sit in `HARNESS_WHITELIST` (§4.4) — always injected, never evictable, not
  unloadable — so the threshold squeeze cannot take away the mechanism the loop drives every turn.

**`_check_new_input`** is the zero-argument counterpart, auto-invoked at each *iteration boundary*
when a queued message may cut into the running turn. It is a mode — `agent.cutin_enabled`, default
**true**, toggled at runtime — and when false the boundary check is skipped entirely. The check asks
the inbox whether the queue is non-empty (no channel filter) and the tool's execution pulls the
first queued message, returning its bare text: the content already carries its marker, so no wrapper
is needed. The extraction is gated behind the same cancel guard so a cancelled turn never drops the
queued message. It is **main agent only**, and the injected message is a live input the model
addresses in the same turn.

### 2.6 Timing — heartbeat, schedules, timers

The agent is otherwise purely user-driven; these three mechanisms give it time.

**Heartbeat.** While idle, every `agent.heartbeat_interval` seconds (default 1800) the service posts
a `[Heartbeat]` message, which runs as a normal turn with its own history and is saved like any
other. The reply contract: real content if the agent has something worth saying proactively,
otherwise exactly `.`. The loop skips a beat when the inbox is busy or has pending work, so it never
competes with real input. **Main agent only** — a worker is task-driven.

**Scheduled tasks** are three separated concerns — *timing*, *execution*, *record*:

- **Timing.** `schedule_loop` runs every 30 s and recomputes each enabled task's next fire **from the
  DB, not from memory**: the anchor is the newest `due_at` across the task's runs (a fire is never
  re-detected), falling back to `created_at`. Cron parsing is `croniter` behind a thin wrapper. The
  loop **fires only**: against a 120 s grace window, a fire due within it is fired; anything older
  means slife was down, which is the startup sweep's concern. An in-memory pending-fire guard keeps
  the poll from re-firing mid-turn.
- **Trigger → execution.** The loop injects a `[Schedule <name>]` trigger on the **system** channel;
  the run is recorded when the agent *dispatches*, not at fire time. The agent handles the trigger by
  delegating: `run_schedule_now` — the single dispatch tool, also used to backfill — records a
  `pending` run, spawns or reuses the worker named after the task, and sends it deterministic task
  text instructing it to save a report and notify the user. Completion rides the ordinary subagent
  auto-push (§6.3).
- **Record.** `scheduled_tasks`, `scheduled_runs` and `reports` live in the memfiles DB. A report
  bound to a task backfills the newest unlinked run at the store layer — `pending → ran` is the
  **only** success writeback; everything else is failed-by-default.
- **Failed and missed runs are settled at startup.** A one-shot sweep reaps every surviving
  `pending` run to `failed` (a run from a dead process can never complete) and fires due while slife
  was down to `missed`. Both surface through the per-turn prompt and can be backfilled or closed.
  Tasks fire only while slife is running.

**Timer.** `wait_minutes` pauses the current turn and resumes it later by scheduling an in-memory
wake that posts a `[Timer]` message on the system channel. It dies with the process — anything that
must survive a restart is a scheduled task.

### 2.7 Roles — the main agent and the worker

Both roles run the **identical** `AgentLoop`. What differs is the harness around it, and that
difference is **declared once**, as capabilities in `slife/agent/roles.py`:

```python
MAIN   = Caps()                                    # the full harness
WORKER = Caps(**dict.fromkeys(ALL_CAPS, False))    # granted none of it
```

A capability is a *grant*: the process either owns the resource (the tool catalog's rows, its vector
index, the plugin child processes, the host MCP face, the heartbeat, the scheduler, the mesh inbox
drain) or holds the policy (turn persistence, the stream-retry ladder, the startup gate, mid-turn
cut-in). The main agent holds all of them; a worker holds none.

This is written down rather than spread around because it used to be ~two dozen `if not
self.is_subagent` branches, which made a worker's capability set an *emergent* property of wherever a
gate happened to be written — so a capability added to the main agent's path could silently never
reach a worker. Because `WORKER` is derived by zeroing **every** field, a newly added capability is
worker-denied by default. Two guards keep it honest: an AST gate that fails on any new `is_subagent`
branch outside the table, and a parity test asserting the two roles' observable difference is exactly
what the table declares.

The config a worker inherits is lossless by construction for the same reason: `Config.to_dict` /
`from_dict` are derived from one field list rather than hand-written, so a field cannot be dropped
silently.

---

## 3. LLM backends

### 3.1 The router and the unified stream

Three backends, equal citizens. The internal message format is OpenAI Chat Completions; each backend
owns its own wire conversion, and all three produce the same chunk type.

```
LLMClient (thin router, slife/agent/llm_client.py)
  ├── OpenAIBackend           api: "openai-completions"   (the default branch)
  ├── AnthropicBackend        api: "anthropic-messages"
  └── OpenAIResponsesBackend  api: "openai-responses"

StreamChunk(thinking=…, content=…, tool_deltas=…, usage=…)
```

The whole contract the loop uses is two methods: `chat()` (batch, text + usage only — **no tool-call
support**) and `chat_stream()`. Tool calling lives exclusively on the streaming path. `tool_deltas`
items have one uniform shape across backends.

### 3.2 Per-backend notes

| Backend | Thinking | Notes |
|---|---|---|
| **OpenAI Completions** | `extra_body.thinking.type = "enabled"` (+ optional `reasoning_effort`) | `compat.thinking` overrides per model: `"omit"` sends no thinking field (gateways that 400 on the enabled shape but reason natively), `"disabled"` forces explicit off, `"enabled"` is the default. DeepSeek gets an explicit `"disabled"` when off. The usage block is handled **before** the empty-`choices` guard — the final usage chunk has no choices, so otherwise no usage would ever be emitted and context accounting would collapse to an estimate. |
| **Anthropic Messages** | `thinking.budget_tokens = max(max_tokens // 2, 1024)` | `compat.thinkingFormat: "openai"` (Bailian/Qwen) sends no thinking param — the model always thinks. Sampling params go through `extra_body`. |
| **OpenAI Responses** | `reasoning.effort` (default `"medium"`) | Streams both `reasoning_text` and `reasoning_summary_text` deltas; emits the Responses API's native `function_call` / `function_call_output` items for tool history, not the Chat-Completions shape. |

**Anthropic prompt caching.** Each OpenAI `system` message becomes an Anthropic system content block
and the **last** one is tagged `cache_control: {"type": "ephemeral"}` — the static base prompt
becomes the cache breakpoint (§2.4). On by default for `api.anthropic.com`, off for
Anthropic-compatible providers that may reject the field, overridable per model via
`compat.cacheControl`.

**Anthropic alternation is mandatory.** Tool results are coalesced into one `user` message per batch
and a following user text message is merged into that same block — two consecutive users is a 400 on
Bedrock and Bailian/Qwen. An assistant with no text and no tool calls gets a single empty text block
rather than an empty content array.

**Outbound hardening.** `OpenAIBackend._normalize_messages` replaces empty assistant content (a
reasoning-only turn, a max-tokens cut) with `"…"` — a *copy*, storage untouched — so
openai-completions providers never 400 on an empty assistant message. When thinking is enabled,
`to_openai_messages` synthesizes `reasoning_content: ""` on **every** assistant message, including
the synthetic harness one, or DeepSeek/Qwen reject the request.

### 3.3 The stream failure contract

One contract, every source. Transient transport failures — `httpx2.TransportError`, the SDKs'
`*APIConnectionError` / `*APITimeoutError`, and the loop's own `StreamStallError` — are retried with
bounded linear backoff (`stream.retries` = 2, so three attempts). The main agent, heartbeat, WeChat
and A2A share it; **workers deliberately do not** (§6.4). Bad-request, content-filter and auth errors
are not retried here. The history is kept intact on transient failures; only the 400-class rejection
rolls back.

**The stall watchdog is an inactivity timer, not a total one.** `_consume_stream` wraps every
`anext()` in an `asyncio.timeout` that **resets on each chunk**, so a provider that answers `200 OK`
and then sends nothing is cut after `work.stall` (120 s) while a slow-but-live generation is never
cut. The separate opt-in `stream_timeout` remains a total per-call cap, set only for workers.

**Model switching** is config + runtime only, no API call: `switch_model(ref)` validates, persists
`active_model`, and rebuilds the client, loop parameters and system prompt. Context-usage state is
deliberately **not** wiped — it self-corrects on the next API call. The tools are `model_list` /
`model_set` / `model_remove` / `model_switch`; `model_set` is an **upsert that merges, not
replaces**, so a partial update keeps the model's other fields. The `Ctrl+S` inline picker is an
**emergency escape** for when the current model is unavailable and the model cannot call
`model_switch` itself — see Appendix A.

---

## 4. The tool system

### 4.1 The Tool ABC and schema authoring

`Tool` (`slife/tools/base.py`) defines `name`, `description`, `parameters` (JSON Schema), `category`,
and `async execute(**kwargs) -> str`. Required fields are validated at class-definition time via
`__init_subclass__`. `from_config(cfg, config, ctx)` allows per-tool construction from `tools.yaml`
overrides; `ctx` carries runtime references (registry, config, MCP client, history).

**Auto-discovery.** `slife/tools/factory.py` imports every module in `slife.tools.*` and walks
`Tool.__subclasses__()` recursively, so a new `.py` file is picked up automatically. Disabled tools
are still registered and refuse **at execute time** rather than being silently absent — a tool like
`attach_image` reports `vision=false` when the active model has no vision instead of vanishing.

**A schema is enforced, not advisory.** `__init_subclass__` adds `additionalProperties: false` to
every harness-authored schema that does not state its own answer, and `validate_args` checks each
call against it at the single dispatch point before the tool runs. A required parameter that never
arrived, or a name the tool does not declare, returns an `Error:` naming the parameters that *do*
exist. This closes the failure where a guessed parameter name landed in `**kwargs`, was dropped
without a trace, and the required parameter silently fell back to its default while the call reported
success. Closure is applied at class definition because authoring style is not the contract — the
schemas are written three ways (a literal dict, `make_params`, `NO_PARAMS`) and closing only one
style would leave the majority swallowing typos. Two deliberate exceptions: a schema that states
`additionalProperties` itself keeps that answer, and a **remote** schema is never touched — a
third-party server's schema is the server's contract to declare.

**How to write one.** The schema is the model's only view of the tool, so write it for the model:

- **`description` = what the tool does** — one or two sentences: what it does and what it returns.
  Do not write when-to-use ("Use when…"), and do not restate knowledge the model already has. Keep
  project-specific facts it cannot infer: idempotency ("upsert — add + update in one call"),
  blocking ("BLOCKS until the model is loaded"), effect timing ("takes effect after restart").
- **Parameter docs = how to use.** Per parameter: accepted format, where the value comes from
  ("`turn_id` from `turn_list`"), what the values mean, and the default.
- **Mechanism.** Builtin tools carry docs in the `parameters` dict. Plugin tools (`@mcp.tool`) get
  them from a Google-style `Args:` docstring — fastmcp parses it into the input schema, so a plugin
  tool whose parameters have no `Args:` yields an undocumented schema.
- **Language.** Model-visible strings are English (§1).

### 4.2 Families and naming

Three families exist by **ownership** — indistinguishable to the model at the call site.

| Family | Owner | Categories | What it is |
|---|---|---|---|
| **system** | the developer | `builtin`, `plugin` | Slife ships it: a module in `slife/tools/`, or a built-in plugin's own tool |
| **job** | the user | `job` | code the user wrote: a public function in `jobs/`, exposed as `job-<function>` |
| **external** | a third party | `mcp`, `rest-api` | someone else's server, reached through the gateway |

`skill` and `cli` belong to none: nothing owns them, there is nothing to spawn and nothing to
register — the row *is* the thing (a playbook file, a `tools.yaml` entry). They are tools all the
same, reached through search and then by using them.

**Naming rules are fixed.** System tools are bare. A job is `job-<function>`. An external tool is
`{server}__{tool}`. Two source-fed families are namespaced in the catalog: a skill row is
`skill:<dir>`, a cli row `cli:<entry>`. A name is the row's identity — the primary key, the
embeddings' foreign key, the key every search result is merged by — so two families cannot share one,
and sharing is not a mistake to prevent: `browser-harness` is a CLI *and* the skill documenting it.

**Semantic identity is not implementation.** `mcp_list` returns only the `mcp` section and
`rest_api_list` only `rest-api`, even though a REST API is currently served by an `mcp-openapi-proxy`
process that shares the transport, the pool and the config shape. That sharing is an implementation
choice, not an identity, and it is not allowed to show on the model's surface: returning a REST API
under `mcp_list` would report servers the `mcp_*` tools do not manage, indistinguishable from the
ones they do. All five `mcp_*` tools are gated the same way — naming a REST API is refused with the
`rest_api_*` twin that owns it. The gate reads a `category` the *caller* declares, so the
implementation is written once with two thin registrations each — and `category` is never a schema
parameter: a model that could declare its own family would declare its way past the gate.

### 4.3 The catalog — `tools.db`

**The catalog is the load/unload model.** Every tool is a row in one shared `tools.db`, read by the
main agent, subagents and the gateway child alike. One store class (`CatalogStore`) owns all SQL;
policy lives in `ToolCatalogService`. The schema is `slife/tools/catalog_schema.sql`.

The columns that matter conceptually (the rest are in the schema file):

- **`name`** — the row's identity, in the shapes of §4.2.
- **`category`** — `builtin | job | plugin | mcp | rest-api | skill | cli`. There is no derived
  `type` column: the load-state question is a membership test over the function categories, not a
  second thing to write and keep in sync.
- **`source_id`** — the owning server (mcp/rest-api) or plugin (plugin/job); `n/a` otherwise.
- **`schema`** — the tool def `{name, description, inputSchema}` for function rows, the SKILL.md text
  for a skill, a synthesized descriptor for a cli. This column is **both** the injected definition
  and the semantic index's document.
- **`status`** and **`load_status`** — see below. **`last_loaded`** is the LRU key.

Two running-state columns, deliberately separate questions: `status` is what the config says (or what
the runtime found), `load_status` is what the **model** decided. **No column is nullable** — "not
applicable" is a value, never NULL, so every read is a plain comparison.

**`status` is one column with three exclusive values**, because they answer one question and do not
coexist. `enabled` is the ordinary state; `disabled` means the config switched it off (per **server**
for mcp/rest-api — all of its tools move together, there is no per-tool enable — per entry
otherwise); `error` means its owner is unusable right now. Two writers move that value, each owning
one transition, and each is guarded: config writes `disabled ↔ enabled`, the runtime writes
`enabled → error` and back. So a server switched off while it was down is `disabled` — **off is not
down** — and coming back up does not resurrect a tool the config switched off.

`load_status` is the db's whole reason to exist, and **no verdict is ever written into it**: writing
the connectivity mark there would destroy the load state it landed on, so a blip would reset every
tool the model had loaded. It has exactly four writers (§4.4).

**Removal is a row DELETE, never a status mark** — one statement per set, so a whole server, a
category mirror or a single vanished tool drops cheaply, while a tool that is merely switched off
keeps its row with `status = disabled`. The families differ only in who reports the death:
`tools.yaml` for an external server leaving it, the server's own `tools/list` for a tool it stopped
publishing, the source mirror for a skill/cli/job, and the boot seed for a builtin whose class left
the code.

**There is no `server` table.** Which servers to bring up is decided by `tools.yaml`, what is live
right now is answered by the gateway's pool, and the db records the RESULT on the tool rows. A server
table would be a third copy of facts that already have owners — and one that goes stale the moment
the gateway child dies.

**Effective status** is derived from the row alone, with `status` outranking `load_status`:
`disabled` → `disabled`; `error` → `error`; a function row → its `load_status`; a skill/cli row →
`enabled`. One label per fact. The first two never overwrite the third, which is what lets a loaded
tool come back loaded. **Injection takes the function rows that are enabled and loaded** — that
single predicate is both the injection query and the effective-status rule, and the two move
together.

**Configuration — `tools.yaml`** carries one section per category (`builtin`, `plugin`, `mcp`,
`rest-api`, `job`, `cli`, `skill`) plus `tool_load.threshold`. Every entry carries the same two
policy flags. **`enabled`** mirrors onto the row's `status`. **`autoload`** means injected from
session start and never evicted — per *tool* where a tool has its own name (`builtin`/`job`), per
*server* in `mcp`/`rest-api`, because an external tool's name is not knowable before its server
connects. It is accepted and inert for `skill`/`cli`, which have no load state to seed. Unlike every
other mirror decision, `autoload` also **overrides** an existing row's state — the one place config
wins over the model.

A `rest-api` entry *is* a standard MCP server (an `uvx mcp-openapi-proxy` instance) that lives in the
other section. **The section is the whole fact** — nothing is tagged for it. `source` records where a
definition was *downloaded* from, which is a different question.

### 4.4 Load, inject, evict

**Boot seeding.** `sync_system_tools` gives every registered tool a row. A **new** row is born
`loaded` only from the two autoload sources — the whitelist and `autoload: true` entries — and
`unloaded` otherwise. An **existing** row keeps whatever the model decided, with the `autoload`
exception above. Every external row is then marked `error`, because no server is up yet.

**Per-request injection.** Before **every** LLM request the loop refreshes a snapshot — the rows with
`load_status = 'loaded'` (and not config-disabled) ∪ the whitelist — and builds the request's
function list from the catalog's `schema` column. The registry key always wins over the descriptor's
bare name, so the injected name is exactly what `registry.execute` resolves. Per-request is what
makes `func_tool_load` mean anything: a tool loaded in iteration *n* is in iteration *n+1*'s request.
Because the list is in registry insertion order, a proxy materialized mid-turn **appends** — the
request's prefix is untouched and the prompt cache survives the load. One request's own retries reuse
the list computed for it, so every attempt sends byte-identical tools.

**Threshold eviction** runs at the turn boundary: if the loaded count exceeds `tool_load.threshold`
(default 100), the excess is dropped least-recently-used-first, protected by the same two autoload
sources that seed a row loaded. Two rules keep the LRU honest — seeding never touches `last_loaded`,
and **every successful execute bumps it**, so a tool used this turn is never the next victim.
Eviction is main-owner only; a worker inherits the curator's budget and never squeezes it.

**Evicted tools stay registered and stay callable.** Eviction takes them out of the injection
snapshot and nothing else. **Load state governs what a turn injects, never what a call may do.** What
an evicted tool loses is its schema, and the next load restores that.

**`load_status` has exactly four writers**: the autoload override, `func_tool_load`,
`_func_tool_unload`, and eviction. Everything else about a row's state lives in `status`'s two lanes,
which is what keeps a disconnect or a restart from costing the model its set.

**The whitelist** (`slife/tools/whitelist.py`) is the always-injected carve-out: the three harness
tools, the five tool-system meta tools, and two pinned calls every session reaches for (`skill_use`,
`system_health`). It never evicts and is not unloadable — a design constant, not configurable.

### 4.5 Discovery — search and load

**`tool_search`** spans every category. Its filters *are* the catalog's columns — `category`,
`source_id`, `status`, `load_status` — one parameter per column, so the surface cannot drift from the
table. A filter the agent does not supply contributes no clause at all, and every one is a real SQL
predicate so filtering happens before the `LIMIT`.

Three retrieval routes, one row shape: `grep` (a real regex, so `summ.rize` matches; an invalid
pattern is reported, never a silent no-match), `keyword` (FTS5 BM25, CJK-routed to a LIKE fallback),
and `hybrid` (keyword + semantic KNN, merged by reciprocal rank fusion). **An empty query browses**:
with no text to match it returns the rows passing the filters, which is also how a family gets
enumerated. Results are scored on one 0–1 scale (`similarity`), shared with `turn_search` and
`cabinet_search` so the numbers are comparable; a keyword-only hit carries no `similarity`, because
nothing measured it and inventing a number would be a lie about the match.

**`func_tool_load`** loads a function tool by full name. Refusals come from the effective status and
name it — unknown → "see tool_search"; disabled → "enable it first"; error → "its server is not up
right now, check it with mcp_list, then retry". On success the row flips to `loaded` and the tool is
in the **very next** request. For external rows it also materializes the execution proxy from the
row's schema descriptor — loading and materialization are the same step, driven by the row.

**What a load does not do is unlock anything.** It never gates a call. A tool with an execution
instance is callable whether or not the model loaded it, so load-and-call in one message is
legitimate — the load is what puts the tool's **schema** in front of the model, which is what makes
the arguments read rather than guessed.

**`_func_tool_unload`** is the spare ticket: it frees a slot in the tool list without making the tool
uncallable, and is refused for the whitelist. One family is the exception on the execution side — an
external proxy is unregistered along with its row, because that proxy holds a live client. That is a
resource decision, not a gate.

### 4.6 Results, errors and meta-parameters

Every tool returns a single string. The failure contract is one rule, one token: **a failed call
returns a string starting with `Error:`**. The harness derives the persisted `is_error` flag from
exactly that prefix at both dispatch sites, judged **before** the argument-truncation marker is
prepended, so a failed call still reads as an error even when the marker leads the text. The flag is
stored on the tool message and session restore reads the stored flag rather than re-deriving it.
There is deliberately no second failure token.

**Meta-parameters.** Tool schemas sent to the model carry **business parameters only**. Three
meta-parameters — `_timeout`, `_async`, `_approve` — are declared once in the system prompt and
popped before dispatch; re-describing them on each of ~60 schemas would be the single biggest
per-request context tax.

### 4.7 Timeouts

**One registry, and the values are code.** Every timeout reads at call time from the typed dataclass
defaults of `slife/timeouts.py`, exposed as `_timeouts.timeouts.<role>.<key>` — developer-owned, with
no user-facing config section and no second seat. A structurally invalid edit fails loudly at import,
and consumers do **call-time lookups**, never import-captured constants, so tests can monkeypatch a
value. The roles are `work` (per-call execution budgets), `ready` (startup / spawn / connect /
liveness), `grace` (teardown and kill escalation), `transport` (HTTP and wire phases), `stream` (the
retry ladder), `storage` (bounded lock waits — DB *reads* are unbounded by design) and `deliver`
(mesh delivery). Two gates keep it evergreen: an AST scanner that fails CI on any hardcoded numeric
timeout unless allowlisted, and a companion that fails on a declared-but-unconsumed key.

**The model, in five rules.** (1) *Owner-of-await*: every await that can block has a bound, owned by
the layer that awaits it; a callee never sets a total for its caller. (2) **The only sanctioned
"total" is the tool-call budget** — there is no turn deadline and no chain-decreasing budgets.
Long-running-but-live work is bounded by *inactivity* watchdogs that reset on progress, never a wall
clock. (3) *Slots are contracts*: some values mirror an upstream wire contract and must be replicated
faithfully, not "improved". (4) *One semantic, one value*. (5) *No global defaults* — a process-wide
socket timeout would silently change every third-party socket.

**One bound outside that rule: the startup sync.** `ready.tool_sync_wait` is the single budget for the
boot tool sync — it bounds each mirror's wait on the gateway, decides when the tool-set line reports what
the set has instead of waiting, and is the age at which a wedged reconcile pass may be abandoned by the
next one. Its invariant (`>= ready.connect_startup + ready.list_tools`) is what keeps the line honest: it
may not claim "synced" before the gateway's own establishment and listing bounds have expired. The
corollary is why it had to be written down: **no blocking work may run on an event loop** — a sync
subprocess suspends every timer in that process, so one `# noqa-timeout` call that freezes the loop
invalidates every other deadline in it (measured: 137s of frozen gateway loop, thirteen expired connect
bounds firing at once). Blocking work goes to a daemon thread via `slife.threads.run_daemon`.

**Tool-execution precedence — one value per tool call.**

1. The agent injects a positive value (the `_timeout` meta-parameter, or the tool's own `timeout`
   argument) → that value is `T`. **The agent's timeout overrides all system defaults.** `0`,
   negative or missing are not overrides: they mean "use the default", never "no timeout".
2. Otherwise the chain default applies: a tool **with** a native `timeout` parameter keeps its own
   registry value and is the single enforcer; a tool **without** one gets `work.tool_budget` via
   `asyncio.wait_for`.

Enforcement is exactly one timer per call — native-`timeout` tools are never wrapped by the loop. A
tool's own default is therefore a **generous backstop**, never the operative bound for a call
carrying an effective `T`; a tight native default would preempt the injected value. If a native tool
has an internal run-timeout, it **must** expose it as a `timeout` parameter — a hidden inner timer
would silently clamp the injected value.

**Backgrounded calls are the exception.** A background call with an injected timeout follows the same
mapping; **without** one it is scheduled bare. The chain default is deliberately not applied:
`_async` exists to escape the in-turn budget, so its bound must not govern background execution.

### 4.8 The approval gate

Approval is **model-driven** — pure model judgment. There is no `requires_approval` flag on any tool
or MCP server; the model decides per call by setting `_approve: true`. Execution then pauses and an
inline prompt row is mounted in the chat stream (Y = approve, N / Esc = deny, no modal). Prompts
serialize behind a lock. A denied call never mounts a tool widget; the prompt row itself carries the
rejection state. A headless worker has no handler and auto-approves.

---

## 5. Plugins

Nine built-in plugins run as independent child processes: `local-embed`, `mcp-gateway`, `memdb`,
`memfiles`, `wechat`, `sharefile`, `a2a`, `media`, `job-coding`. There is **no `plugins.external`
mechanism** — third-party capability enters only as a standard MCP server in `tools.yaml`, connected
by the internal gateway plugin.

### 5.1 The spec — one source of truth

Every child plugin is declared by one `PluginSpec` (a frozen dataclass) in the ordered table
`PLUGIN_SPECS` (`slife/plugins/spec.py`). Nothing else in the harness hard-codes a plugin's module,
enablement or glue: every name-keyed table that used to exist — the start `if/elif` chain, the
connect-glue map, the health check list, the tool-adapter route set, the reserved-name list — is now
a lookup into this one table. The fields are `name`, `module`, `ctx_field` (the `ToolContext`
attribute receiving the live client), `gateway` / `host_params` (mcp-gateway only), `enable_method`
and `after_ready_method` (names of `AgentService` coroutines), `health`, `fixed_port`, and
`semantic_reload_tool`.

`spec.py` is **stdlib-only on purpose**, so the MCP child, the health tools and the tool adapter can
import it without pulling in `AgentService`. Per-plugin *behaviour* is declared as a method **name
string**, resolved once in `AgentService.__init__` (which asserts a spec never names a missing
method).

The table normalises a few naming rules: public names are hyphenated where a package cannot be
(`job_coding` → `job-coding`); the port env var is `SLIFE_{NAME}_PORT` with dashes → underscores; the
health function is `check_<name>`; the `ToolContext` field names are per-plugin and the historical
non-uniformities are kept. An external MCP server may not take a built-in plugin's name.

**Adding a plugin is one spec row plus a `server.py` package.** Auto-discovery returns every declared
plugin whose `server.py` exists, in spec order, then appends any undeclared package under
`slife.plugins.*` that has a `server.py` — it runs through the same generic lifecycle with a default
never-fails spec. Identity matching is by **module path**, not leaf name: matching on the leaf would
miss `mcp_gateway` ↔ `mcp-gateway` and spawn the same server twice.

### 5.2 The lifecycle

`AgentService.__init__` builds one **`PluginRegistry`** from `PLUGIN_SPECS`, eagerly creating a
`PluginLifecycle` per declared plugin before anything starts. A lifecycle owns the plugin's client,
process, port, supervised background tasks, watchdog state, readiness and the exact set of registered
tool names. Start, stop, watchdog, connect and health all iterate the registry — there is no
`if name == "…"` anywhere in the lifecycle engine.

**Readiness is protocol-defined, not probed.** A plugin is ready exactly when the harness's
connect-time **era negotiation** completes (`slife/mcp/era.py`): a modern plugin answers
`server/discover` and is adopted at the 2026-07-28 revision; a legacy one gets the `initialize`
handshake. A plugin server answers only after its own FastMCP lifespan finished, so the completed
negotiation *is* the ready signal. There is no readiness tool.

Each plugin's own serving requirement is encoded in its lifespan — memdb and memfiles require a
usable store, and a failure there means the port signal never fires, so the harness reports `FAILED`.
Dependencies that are **not** required to serve are deliberately outside the lifespan and never gate
readiness: the gateway's external servers, sharefile's tunnel, WeChat's login, media providers, the
A2A broker, embedding backends. They surface through their own status tools instead.

A **required** plugin (`plugins.required`; the shipped config names `memdb` and `memfiles`) failing to
become ready **aborts startup** rather than limping on. The spawn hang-guard is bounded at 60 s, and
the service opens for input only once every plugin spawn has converged, so input can never race ahead
of plugin startup.

**Start is one path for every plugin**: idempotent if already running → the spec's enable hook (first
start only; a watchdog restart skips it — a hook returning False is an *expected* no-op, and it also
purges the plugin's catalog rows) → the uniform start (spawn the child, set its port env, connect,
register bare-name tool proxies, filter internal tools, mirror them into the shared catalog, clear
any `error` mark, mark initialized) → re-point the `ToolContext` field at the live client → run the
after-ready hook → arm the watchdog. Spawn or hook failure → `FAILED`.

**The watchdog** supervises every started plugin identically. On an unexpected child exit it
unregisters the plugin's exact registered tools (plus any registry tool bound to the dead client),
disconnects the dead client — deliberately tearing it down rather than dropping it, so the SDK's
background tasks cannot hammer a dead port — and restarts through the full uniform start with
exponential backoff, up to five consecutive failures. The restart counter resets **only when the
crashed child had stayed up past `ready.spawn`**: a fast boot-loop is deliberately not reset, so a
crashing plugin accumulates toward the cap instead of restarting forever. The plugin's supervised
tasks are reaped before each respawn so a restart never stacks a second poll loop. A restart tells
every live worker sharing the plugin its new port. Subagents have no watchdog of their own.

**Stop** is uniform: set the stopping flag *before* touching the process (otherwise the watchdog's
wait races and triggers a spurious restart), cancel the watchdog, cancel the supervised tasks,
disconnect the client, stop the child.

**A plugin may spawn children of its own** — the tunnel's `cloudflared`, every external MCP server
the gateway runs — and those are reachable only through it. So both stop ladders kill the whole tree
rather than the child. On POSIX the descendants are read from `ps` **before** anything is signalled,
because a dead parent's children reparent to init and become unfindable. The stop path, however, only
runs while slife is alive to run it — a hard-killed parent unwinds no Python at all — so on Windows
each spawned child is additionally assigned to a **kill-on-close job object** at spawn, before it can
spawn anything of its own, and the kernel then terminates whatever is still inside when slife dies
for any reason. The assignment lives in the uniform spawn, so no plugin carries cleanup code for it.

### 5.3 The child contract

A plugin's `server.py` must: bind a free port; signal the parent **once ready** — `run_plugin_server`
wraps the lifespan and emits the port on stdout only *after* the app is ready to serve MCP, and a
plugin must never signal early; start FastMCP on Streamable HTTP with the pre-bound socket; expose
`@mcp.tool`s, where bare names are public and `__`-prefixed are internal; and be importable as
`python -m <module>`. There is no base class and no SDK — the contract is that shape plus the spec
row. Heavy post-readiness work goes through `warm_after_ready` rather than the lifespan. A public
tool becomes a catalog row the moment the child is ready, so it is findable by `tool_search` (born
`unloaded`: searchable, not injected, until loaded).

Two mechanics worth knowing. **stdout is the port channel only** and is closed immediately after the
signal, which is why OAuth instructions go to stderr — and one specific marked line is what the
parent turns into a desktop notification. And **plugin servers run in SSE mode**
(`json_response=False`): a listen stream *is* a response stream, and a single JSON body per POST has
nowhere to carry a change notification.

The parent hands the child its identity and its serving ports through the **process environment** —
there is no other in-band channel before the first request: the session id, the agent name, the
directory overrides, the plugin name, and `SLIFE_{NAME}_PORT` for each plugin. Workers read the port
vars to share the parent's plugins, so the env var is also the sharing mechanism.

**Everything the child logs is a diagnostic pipe, not a terminal.** The child runs stderr at DEBUG
with its own session file; the parent relays stderr at DEBUG, masking secrets and filtering banner
art and the child's own already-formatted lines. An uncaught exception writes the full traceback to
the file and prints exactly one line to stderr — that line is what the host relays as the
load-failure reason.

### 5.4 The built-in plugins

| Plugin | Role |
|---|---|
| **mcp-gateway** | Gateway for external MCP servers (stdio / SSE / Streamable HTTP). Owns the transports, the per-server tool snapshot and OAuth; the *catalog* is the host's. |
| **memdb** | Turns database, hybrid search, turn persistence, session restore, embedding configuration. |
| **memfiles** | Private notes / diary / files / reports cabinet (§7.5). Also owns the scheduled-task *data* tables; the schedule *tools* are builtin. |
| **wechat** | Bidirectional WeChat messaging. A long-poll loop feeds incoming messages into the inbox as `wechat`-channel turns; the model replies itself via `wechat_send_message` — no harness auto-dispatch. |
| **sharefile** | Public file sharing. Serves file bytes over a plain-HTTP `/share/{token}` route on the **same port** as its MCP endpoint, stat-pinned so a share never silently serves replaced content. Owns the pluggable tunnel. |
| **a2a** | The mesh (§8). Starts only when the broker is reachable. |
| **media** | Non-chat generation — image, video, TTS, ASR — behind a provider-agnostic adapter layer. Artifacts are work products in the working directory, never cabinet files. |
| **job-coding** | Deterministic jobs as MCP tools (§5.6). |
| **local-embed** | OpenAI-compatible embeddings on `/v1/embeddings`, from its own package because it is also runnable standalone. The one plugin with `fixed_port`, since its config pins the port a static embeddings `base_url` points at. |

**The sharefile tunnel is pluggable** (`sharefile.yaml` names `active_provider`). Every provider
presents one surface and shares one lifecycle: a single-flight start guard, retries with backoff, an
`active`/`starting`/`failed`/`idle` state machine, and a background health monitor. Three providers:
`ngrok`, `localhost.run` (`ssh -R`, no account) and `cloudflare` (`cloudflared tunnel --url`, no
account). A **missing dependency is terminal** and never retried; a **transport failure is retried**,
and free-tier sessions recycle, so the monitor keeps restarting the tunnel in the background. The
plugin always loads — the tunnel never gates readiness.

Two rules shape the tunnel's liveness, and both generalise:

- **A published URL is not a working one.** A provider declares whether a printed URL is itself proof
  of readiness; it is *not* for `cloudflare`, whose banner precedes the edge connection, so for that
  window the hostname exists and answers HTTP 530. Readiness is deliberately the *edge* signal, not a
  local fetch of the published URL: on a fresh tunnel a public resolver answers as soon as the
  hostname is printable while the local resolver is still negative-caching it, so a self-probe would
  measure this machine's lag and refuse URLs that work.
- **Liveness cannot be read off the child's stdout.** A `cloudflared` that outlives its edge
  connection keeps running while every published link answers 530, and a lost connection is logged as
  a retryable error with **no** unregister line following it — so a connector set scraped from
  stdout looks complete straight through the outage. Liveness is therefore asked of the transport.
  An unanswered probe is `None`, never `False`: a probe that cannot answer must not be the thing that
  declares an outage. And the monitor does not respawn a tunnel the moment it goes unreachable — a
  child that lost the edge re-registers on its own and **keeps its hostname**, while a respawn mints
  a new one and strands every link already handed out — so an unreachable transport gets a grace
  window to heal first.

`is_reachable()` is deliberately **narrower than `is_active`**: "a URL exists" and "that URL would be
served" are different facts, and `share_file` refuses on the second rather than hand out a link that
530s. `system_health` reports `unreachable` rather than `ok` for the same reason.

### 5.5 The MCP gateway

Three wire transports, one connection class built on the **official MCP SDK `ClientSession`** — the
same mechanism `MCPClient` uses to reach Slife's own plugin children. The class supplies the
lifecycle the SDK does not: OAuth device flow, transport establishment and re-establishment, stdio
stderr relay, per-server connect locking, and the `needs_user_auth` pause. For `url`-configured
servers the gateway tries SSE first and falls back to Streamable HTTP.

**Protocol era decides how a change reaches us.** The modern revision removed the connection-scoped
channel: `notifications/tools/list_changed` arrives **only** on a `subscriptions/listen` stream the
client asked for. So servers we own publish on a subscription bus and the consuming side keeps one
stream open per link, re-listening after a drop; a modern external server gets a per-server listen
stream inside the gateway; a legacy peer keeps the session channel. All three funnel into the same
notification handler, so the harness-side trigger is unchanged. The subscription-bus helper also
closes a FastMCP gap — FastMCP never registers `subscriptions/listen`, so a modern client's stream
would otherwise get "Method not found".

**Notifications are coalesced and sent from a detached task**, never inside a request handler's
cancel scope — an interleaved burst desyncs the SDK's cancel-scope stack and every later call dies.
That is implemented once and shared, not per-plugin.

**Health is a tool list, not a connection.** The modern protocol removed `ping` outright, so a
compliant peer answers "method not found" — which a probe can only read as death or as life. What
actually answers the question is `tools/list`, which is the same call the reconcile already makes, so
the connection keeps a per-server **tool snapshot** (the tools, its age, the peer's TTL, the last
error) instead of a connection state machine. The snapshot is re-read on the peer's own signals — a
change event, a dead transport, a failed call, which repairs on the failing request — never on a
timer. The one background job is acquiring a list for a server that has none; it retries with backoff
and **stops the moment a list succeeds**, so a healthy server is never polled.

**The catalog is shared, not gateway-local.** The gateway owns transports and the live tool surface;
every external tool's row lives in the host's `tools.db`, fed by the host's reconcile whenever a
server's tool surface may have changed. Auto-load servers get their proxies and rows wholesale;
on-demand servers (the default) get **row-only** mirrors so search and load can reach individual
tools one at a time. A server whose last `tools/list` failed has its rows marked `error` — the
runtime lane of the status column — so they leave the injected set while the load state the model
chose stays on the row and comes back with the server.

**The reconcile** (`_sync_mcp_proxies`) is driven by connect events, `mcp_*` mutations, change
notifications and the gateway-ready glue, and runs in the **background** so a plugin start never
waits on external servers. It projects the connectivity verdict onto the row status column, mirrors
each enabled server's rows **concurrently** (each awaits a real `tools/list`) and in one batched pass,
skips disabled servers entirely (asking for a tool list *is* connecting), unregisters proxies whose
server left the config, and purges the rows of removed servers by comparing against `tools.yaml` —
the authority — rather than the live pool, so a gateway restart cannot wipe a still-configured
server's rows.

**The boot window.** Because the reconcile runs in the background, there is a window after startup
where the registry holds the builtins but not yet the external tools; a call then fails with
`Unknown tool` before the row exists and the "is not loaded" refusal after it. Nothing is broken —
the pass has not finished. One line marks the moment the set is usable, emitted **once per process**
on the first pass that has converged (and always on failure): silence therefore means *still
syncing*. Its `total` is what is usable right now — every enabled catalog row, so skills and CLIs
count too — deliberately not the registry, which holds registered *instances* and which the two
registry-less families have none of. Convergence is "no enabled server is still starting": a bare
"no list yet" means three different things, so the gateway's own `spawn_settled` flag and a
`reachable` verdict decide whether a server is a failed spawn, a slow one still answering, or one
whose spawn is simply still in flight and must not be judged. The reported delta counts what the
startup **wrote to the catalog** — insert / update / delete — never a registry before-and-after,
which would announce the entire external tool set as new on every restart.

**Crash survival.** When the gateway child dies, every external row is marked `error` so none of them
keeps injecting a dead transport; the restarted gateway connects every enabled server again and the
reconcile clears each mark as its server comes up. A crash costs one reconnect, not the session's
toolset.

**OAuth** uses the device-code flow with tokens in the credential store. The `needs_user_auth` state
is real: while it is set, a background retry **refuses to re-run the device flow** — a device-flow
prompt must never be raised by a retry — and the list refresh and call paths raise immediately.

**External connections are proxy-free on loopback.** The SDK's Streamable HTTP client builds a
default HTTP client that reads the OS proxy configuration and applies it to every request, loopback
included, so on any machine with a proxy configured the gateway's connect and `tools/list` got routed
through it, the connect retry burned up, and plugins never appeared ready. `MCPClient` therefore
supplies its own client with `trust_env=False`. External servers are nuanced: the SSE path keeps the
proxy-reading default, and the Streamable fallback goes proxy-free only when the server is
URL-routed. A remote server that genuinely needs the proxy should be configured deliberately.

### 5.6 Jobs

A **Job** is a plain public function in `<data_dir>/jobs/*.py`. Its docstring and typed signature
become the tool's description and parameter schema, following ordinary MCP tool norms; its name is
the function's with a `job-` prefix, the namespace it shares with the plugin's own management tools —
and one a job may never take.

**The files are the source of truth — there is no job-config file.** A restart (or the watchdog)
re-scans the directory and re-registers the tools. Creating or editing a job is *coding*: a skill in
`skills/` is the authoring guide.

**Execution is deterministic.** The tool calls the job function with exactly its declared arguments,
and the only LLM access is an explicit one-shot call on `job_coding_model` — a top-level
`"provider/model"` ref that reuses `models.providers` and is independent of `active_model`. It should
name a *different*, usually smaller model: a nested one-shot job call then neither churns the agent
loop's prompt-cache prefix nor competes for its quota. No system prompt, no conversation history and
no agent loop ever reaches a job's model — a structural guarantee.

Jobs that call the LLM are `async def`; pure-computation jobs stay plain `def`, and the runner runs
those on a **daemon thread** (never the default executor, whose workers are joined at exit and would
wedge shutdown on a hung job), capturing the context so a sync job's LLM client stays visible.

A job that needs an **external capability** reaches it through a handle that is a lazy proxy to the
gateway, forwarding to the gateway's internal call tool — the same shape the host's proxies use.
Nothing external is ever spawned a second time, so a job can use **any tool on any connected server,
loaded or not**. The handle holds **no client between calls**: a connection is built, used and torn
down per call, so nothing can go stale and the plugin needs no reconnect bookkeeping. Port discovery
is layered — the host pushes the gateway's port at **both edges of the handshake**, so spawn order
never decides it, with the spawn-time env var as the fallback.

After any tool-set mutation the plugin pushes a change notification and the host's generic rescan
re-lists and diff-registers, so per-job tools appear and disappear live. The plugin is watched by the
uniform watchdog and covered by `system_health`.

---

## 6. Subagents

A subagent is an **agent worker**: a local child process running the same agent loop with the same
config and tools as the main agent, deliberately stripped of everything that makes the main agent a
*harness* — no TUI, no turn persistence, no scheduler, no cut-in injection, no A2A inbound drain, no
plugin spawn, no health or watchdog duties. It keeps no independent network identity: when it reaches
the mesh it sends **as the main agent**.

```
 Main agent (the harness)                     Worker child
 ┌──────────────────────────────┐             ┌──────────────────────────────────┐
 │ Inbox ─ one turn per message │             │ python -m slife.subagent.headless│
 │   ▲  [Subagent:…] auto-push  │             │ AgentService(role=WORKER)        │
 │   │                          │  JSON-RPC   │  inbox ─ worker/send = ONE turn  │
 │ _on_subagent_done ◄──────────┼─────────────┤  one-shot history per task       │
 │ SubagentProcess (pipes)      │  stdin/out  │  no TUI · no persistence         │
 └──────────────────────────────┘             │  shared plugin clients           │
                                              └──────────────────────────────────┘
```

Two agents, **one loop machine**. Both run the identical `AgentLoop`, including the `_turn_prompt`
harness pair and the internal trim, driven by the identical inbox. What differs is the harness around
it — the declared capability table of §2.7, not scattered branches. "Does not run the main agent's
harness" is a statement about the *service layer*, not the loop.

**A subagent is not a plugin, and not an A2A peer.** A plugin is spawned and owned by the parent,
speaks MCP over Streamable HTTP on a signalled port, and is watched. A subagent speaks JSON-RPC over
stdin/stdout, owns nothing, and has no watchdog — it dies alone.

### 6.1 The process protocol

The wire is JSON-RPC 2.0, deliberately not A2A: the worker is *local*, not a mesh peer.

| Direction | Message | Purpose |
|---|---|---|
| child → parent | `{"result": {"ready": true}}` | startup readiness — the spawn await blocks on it |
| parent → child | `worker/send` (`id` = rpc_id) | one task (one turn), correlated by id, never by text |
| parent → child | `worker/cancel` | drop-if-queued / preempt-if-running |
| parent → child | `worker/plugin_restart` | a shared plugin moved to a new port — reconnect |
| parent → child | `context` | the cloned parent history, sent on stdin at spawn |
| parent → child | `shutdown` | graceful stop |
| child → parent | `{"result": "<final reply>"}` | the task's one-turn result |
| child → parent | `worker/complete` | "the result above is final" |
| child → parent | `worker/progress` | parsed but never emitted — a reserved notification |

Four implementation details carry real weight:

- **Pure UTF-8 on stdout.** The Windows default stdout codec is the system code page and cannot
  encode emoji, so protocol writes go to the raw buffer, bypassing the codec.
- **Piped stdin is read on a dedicated thread.** asyncio's pipe registration fails for a
  parent-owned pipe on the Windows Proactor loop, so a thread does blocking reads and feeds the event
  loop. The reader stays live while a task runs, so `worker/cancel` can preempt.
- **Config never rides the process env.** The resolved config carries plaintext API keys, so it is
  passed via a `0600` temp file — never the environment, which is readable through the process table.
- **Over-long protocol lines are discarded, never fatal.** One line may legitimately be the whole
  cloned history or a many-megabyte result; a line past even the raised cap is dropped, tail and all,
  so a pathological line cannot kill the reader or wedge the worker.

### 6.2 One turn per task

`spawn_subagent(name, clone_context=False)` starts a named worker. **A worker's name is its
identity** — explicit, never auto-generated, and validated, because the name lands in the child's
system prompt *and* its log filename. Reuse is explicit: spawning a running name returns the live
worker.

Context is chosen once, at spawn: **clean** (the default) runs each task in a bare history; **cloned**
copies the parent's message history without the parent's system message (the worker renders its own)
and ships it on stdin. A clone is a **spawn-time snapshot** — a cloned worker re-seeds from that
fixed snapshot on *every* task, so it never accumulates context across tasks and never sees parent
turns that happen after spawn.

A task is one turn by construction: one `worker/send` becomes one inbox message, which becomes
exactly one `loop.run()`. Inside that run the loop may make many LLM calls and tool calls, bounded by
`max_iterations`, but from the task's point of view there is exactly one turn, one reply, one result.
**The worker processes tasks serially**; extra sends to a busy worker are queued by the *parent*,
never refused and never re-sent.

### 6.3 Identity and result delivery

The worker renders its own system prompt from `subagent.j2`, which frames it as a headless process of
the parent with the same capabilities, carrying **no identity of its own** — no presence, no
personality, and in all external communication it acts as the main agent, never introducing itself.
It is told it is ephemeral, and how it was seeded. Its completion posts back under a dedicated inbox
source, so it is distinguishable from human turns in memory search yet routed into the human history.

**The worker has no result-push tool.** Its reply goes out as an ordinary JSON-RPC result on stdout.
The **parent harness** does all the pushing: a sync caller's future resolves; an async result is
stored and the manager is notified; the manager posts an inbox message carrying the machine marker
`[Subagent:{"subagent_name", "task_id"}]` so the model can attribute it, and the channel records
whether it was a scheduled task. The TUI drops the marker and shows the `Subagent(<name>)>` bubble.
There is deliberately no subscribe call — async results are auto-subscribed, and the `poll` mode
suppresses only the *push*, never the retrievability.

### 6.4 Failure semantics

The worker has no error-handling loop of its own; every failure ends in a result coming back.

| Failure | Surface |
|---|---|
| LLM/provider error inside the turn | the reply text is `Error: …` — the caller's future resolves *successfully* with that text |
| protocol error frame | JSON-RPC error → the parent raises → the tool returns an error string |
| worker died before replying | the pending future fails with "closed before task was resolved" |
| **stall** — no reply at all | the task budget expires → `TimeoutError` → the tool reports the timeout |

The stall case is the interesting one. The abandoned task is **preempted in the child** — a worker is
serial, so a genuinely stuck task must never block later tasks. A **late** result is stored but never
auto-pushed, because the caller was already told it timed out; a push after a reported timeout would
double-announce a task the caller believes failed. Parent-side cancel does the same and discards the
late reply.

This is what "no error handling" means precisely: no retries, no recovery, no second attempt. The
worker is the one agent that does **not** participate in the stream-retry ladder (§3.3) — even a
transient transport failure is a single attempt, surfaced immediately, because there is no user to
wait on. The per-chunk stall watchdog still applies, but in a worker a stall is surfaced, never
retried. `max_subagents` defaults to 5; the task bound is the registry's `work.task_budget`, and a
per-call `timeout` on the send tool is the one model-facing override.

### 6.5 Sharing and recursion

Every plugin the parent started is shared **by port** — a manifest loop over the discovered plugins,
never a hard-coded subset. A plugin the parent skipped published no port and is skipped here too, so
the two processes agree on which plugins exist without either enumerating them. A worker never spawns
its own plugin processes, so a worker crash takes down only the worker.

**There is no isolation.** Shared servers are exactly the parent's servers — same turns DB, same
cabinet, same gateway. The worker is trusted, never fenced. The one deliberate asymmetry: the a2a
plugin registers the send-side tools in the worker so it can act as the parent, but the **inbound**
queue stays with the parent — a worker that could push into its own parent's inbox would confuse its
own history.

**Recursion is allowed**: a subagent can spawn its own descendants. There is intentionally no
subagent-specific gate — trust, not enforcement.

---

## 7. Memory

Every turn is permanently recorded as an independent row — there is **no session concept**, just a
continuous time-ordered log in `<data dir>/<agent>.db`. Agent isolation is at the database-file
level: `--agent alice` gives Alice her own diary, indexes, file cabinet and mesh name.

**Memory is core — the agent never runs silently without it.** A fatal turn-save failure is a hard
stop, not a skip: the memory-broken state is set, the **inbox freezes** (queued turns are dropped — a
turn that cannot be persisted is not worth running), and the TUI shows a persistent banner until the
DB is fixed and the agent restarted. Transient MCP timeouts are warned, not fatal. Restore-side
failure is likewise fatal: a present-but-broken database **aborts startup**.

### 7.1 The turns database (`memdb`)

The backing table is `diary`; the schema is `slife/plugins/memdb/schema.sql`. Conceptually the row
holds the user's message, the assistant side as an OpenAI JSON array (thinking, tool calls, results,
text), an LLM-written summary and tags, the user-input and completion timestamps, the channel
identity, the agent identity and model, the turn's billed token count, and `context_tokens` — the
context size at the last API call, which restore primes the first turn prompt with.

There is **no `images` column**: image blocks live only in the in-memory user message and are never
persisted, so restore is text-only — and so is a turn rebuilt by the per-turn decision, which comes back
as its text plus the `attach_image` call and result (§7.6). Supporting structures are an FTS5 external-content index (whose
UPDATE trigger exists because the summarize tool rewrites columns and an external-content index must
track that), a sqlite-vec table, a key/value `diary_meta` store holding the embedding model identity,
the index text contract's version and the ordered live-context list, and a sibling `turn_channel` row per turn holding the channel's
JSON payload, written atomically with the insert.

**There is no migration layer.** Backward compatibility is not supported: schema changes land in
`schema.sql` for fresh databases, and an old database is deleted and rebuilt rather than upgraded.
The data is derived, and a migration path is a permanent maintenance cost.

### 7.2 Search

Three indexes back the search modes: FTS5 (BM25 keyword), sqlite-vec `vec0` (cosine KNN) and a B-tree
on `created_at`. The modes are `grep` (exact strings — error messages, paths, code), `fts5` (topic /
keyword with ranked snippets), `hybrid` (FTS5 + vec0 merged by reciprocal rank fusion, `k=60`) and
`time` (browse by date).

The cosine metric is **declared in the vec0 DDL**, because that is what makes the raw distance
readable as a 0–1 `similarity`: `1 - distance` is a cosine only when the metric is one, and the
backends do not all normalize — a local gguf backend's raw output is not unit-norm, so an L2 table
could not yield a cosine at all.

All time bounds share one grammar (`slife.timeutil.normalize_time_bound`): an ISO datetime or date,
the day words, the calendar periods (`last week`), or `<N> days ago`. A period word anchors to the
period's **edge**, not to today's day-of-month. A bound in no known grammar **raises** rather than
passing through — SQLite would compare the text, match nothing, and report a bound nobody understood
as "no results".

**What a turn's vector is a vector *of* is the conversation, not the turn.** `_turn_text_for_embedding`
embeds the user message, the tool calls the turn made (names and arguments, bounded) and the
assistant's prose — **not tool results**. Measured on a live session, results were 56–99% of a turn's
text, so an index built on them describes "an agent ran tools" rather than what the turn was about:
every turn lands in one narrow cosine band (unrelated turns at 0.55–0.92 of each other) and a
similarity cap has nothing to separate. Nothing is lost to search — results live in `messages`, which
is what the keyword leg and `turn_read` read. Because that text contract is half of what makes two
vectors comparable, it is **versioned** (`INDEX_TEXT_VERSION`, recorded in `diary_meta`) and a
version change drops the stale vectors for the drainer to rebuild, exactly as a model or dimension
change does.

Two hardening rules are load-bearing: **CJK queries route keyword search to a LIKE fallback**; and FTS5
MATCH operator words are quoted and stray symbols stripped, so a user query cannot crash the MATCH
parser. Without an embedding backend, hybrid degrades to FTS5-only and reports its degraded mode and
reason.

The routing's real reason is narrower than "FTS5 cannot do Chinese, LIKE can". `unicode61` makes a
**contiguous CJK run one token**, so a Chinese query is an *exact-token* lookup: measured on a live
diary, `MATCH '校庆'` matched the four turns where that word sits next to punctuation or a digit, and
`MATCH '具体安排'` matched one where `LIKE '%具体安排%'` matched three — the other two hold the word
glued inside a longer run (`校庆的具体安排有吗` is a single token), which an exact-token lookup cannot
see. LIKE is a true substring matcher per word, which is what Chinese prose needs, so it is the leg
CJK routes to — but it inherits the AND below, which is what limits it in practice.

Both keyword legs **AND** their terms (`_to_fts5_query` and `_like_terms` share that rule, so
`turn_count` and the search cannot disagree). The consequence is worth knowing before trusting the
leg: it fires only on a turn containing *every* word, and for CJK that means every word literally, as
a substring. A query of one or two words the turn would actually contain is what it rewards; a
synonym-stuffed one matches nothing at all, which is what the discriminator's queries tend to be — so
on Chinese turns the semantic leg is often the only one contributing, and the floor above is the
only thing deciding the selection. Steering the query tighter (one or two words) was measured as a
wash and not adopted: the keyword leg starts firing, but relevant hits slip on the semantic leg
(0.53–0.55 → 0.49–0.52), which the floor cannot afford. The query's shape belongs to the
discriminator; the schema states the parameters, not a search strategy.

### 7.3 Embeddings and the semantic gate

Embeddings are a first-class top-level `embeddings` section of `slife.yaml`, shared by memdb,
memfiles and the host's tool catalog. A **provider** is one OpenAI-compatible endpoint with a model,
and `active_model` is a bare provider id. One rule surprises people: **the vector dimension is
deliberately not configured.** It is resolved from a known-model table, the endpoint's `/v1/models`,
or a probe embed.

**`SemanticManager` is the lifecycle actor** — one object owning the binary gate, the embedder
instance and an event-driven index drainer. It is document-generic, so memdb, memfiles and the host
catalog each drive their own instance and the three gates are independent. There is **one
implementation and one subclass**: memdb's is the base class, and the host's catalog subclasses it,
overriding only the four hooks where the catalog genuinely differs.

The gate opens exactly when `embedder_ready ∧ count_unembedded() == 0` — there are no intermediate
states, and **partial semantic results are never served**; while the gate is off, hybrid degrades to
FTS5-only with a hint naming the reason. A persistently failing embedder is bounded by a per-session
no-progress limit, after which the drainer parks in `stalled` rather than exiting: enabled stays
true, so the next save wakes it for a fresh bounded round. Idle therefore costs nothing and a
transient failure self-heals. The state machine and a human-readable reason are reported separately
from the binary gate.

The **write path is insert-only**: saving a turn never embeds on the save path (a slow embed of a
large turn once tripped the save timeout and raised a false alarm about a row that had in fact been
saved). Each insert wakes the idle drainer with a non-blocking event set. **A model or dimension
change** stops the drainer, migrates the vector table in place by comparing the declared width and
the model identity against what the database was built with, and restarts it; a mismatch drops and
recreates the table, because old vectors live in a different vector space.

The catalog's instance publishes its state into `tools.db`'s `meta` table rather than keeping it to
itself. That is not redundancy: the index is *shared*, so a process running no drainer — a worker —
must be able to report it rather than guess. A reader that finds no published row reports `unknown`,
which is a fact, where `disabled` would be a claim about a drainer that is not there.

### 7.4 Session restore, and the live-context list

On startup, recent turns are read **directly from SQLite** — no MCP transport, no plugin dependency.
The UI rebuilds the last session from the diary, and only then does the plugin spawn begin. Restored
messages carry their stored timestamps, so the rebuilt chat matches what was seen live.

**The id list replays the exit-time context.** `diary_meta.context_turns` is an **ordered JSON array
of rowids** naming the live context. Three things maintain it: the save appends the new rowid inside
the diary row's own transaction; the internal trim drops the turns it evicts, passing the **actual**
ids; and the per-turn rebuild replaces the list with what the turn kept plus what it recalled, with an
explicit clear for a context that is to be empty.

- **The list's order is authoritative.** Reads replay it as written and never re-sort by rowid — and
  the slice need not be contiguous.
- **Turns on the list are returned verbatim, with no ceiling re-slicing.** The list already encodes
  the trimmed state and is its own bound.
- **A set refuses an empty list by design** — its guard protects a *partial* selection, not a
  deliberate one; clearing is a separate, explicit operation.
- A database predating the list restores an empty context, and its turns stay searchable.

The restored turn prompt is primed with the latest restored turn's persisted `context_tokens` — the
exact context size at exit — so the first prompt and status bar show real occupancy. The
just-restored history is also exempt from the first-turn trim.

### 7.5 The file cabinet (`memfiles`)

A standard plugin, self-contained and replaceable. **Four** typed knowledge stores, each
**dual-written** to a human-browsable markdown file and a SQLite index: **note** (keyed by subject),
**diary** (keyed by date), **file** (saved attachments under `files/<category>/`, auto-filed by
extension with bytes staying on the filesystem, and semantically searchable from an LLM summary given
at save time — one pass, no separate summarize tool) and **report** (scheduled-task reports).

Each kind owns its FTS5 and vector tables and declares its own time axis, so a range query on a kind
uses the same column its list tool orders by. The index mirrors memdb's design and **reuses its
code**: the shared `SemanticManager` drives the drainer over all kinds, and each plugin reports its
own gate.

`url_save` guards against SSRF **before fetching and again on every redirect hop**: every resolved
address must be globally routable, so loopback, private, link-local and cloud-metadata targets are
refused. One deliberate exception — the documented **fake-ip pools** used by Clash and sing-box are
accepted, because those resolvers answer real public hostnames with addresses from them. (The same
pools are what sharefile's tunnel health *flags*, for the opposite reason: intercepted traffic breaks
the tunnel's control connection.)

All save tools return the saved local path and never auto-publish, so nothing is registered in any
token registry as a side effect of saving. Publishing is always the model's explicit choice, through
the separate sharefile plugin.

### 7.6 Images and the `@` syntax

Users attach images with `@` directives — **one `@` is one source**, any number per input, parsed
independently. The user message stays verbatim; the extracted sources are handed to the loop, which
**auto-invokes** `attach_image` once with the whole list through the harness-call machinery — a
single history shape, no LLM iteration spent deciding to attach.

A source is a bare path, a URL, a data URI, or any of those quoted or bracketed (so spaces work).
Multiple directives may sit adjacent without spaces. A bare path must end in an image extension, so
`@someone` is skipped as plain text; **URLs and data URIs are self-identifying by scheme** and need
no extension gate. A token ends at whitespace, a quote, the next `@`, a CJK character (a natural word
boundary when typing) and — for URLs only — a comma; data URIs keep their commas because base64 is
comma-heavy. There is no filesystem check at parse time: existence is validated downstream, where a
failure becomes a reported error rather than a silent drop.

Parsing is deliberately **two-phase** — locate every `@`, then match a source pattern on the slice
after each — rather than one regex, which is more robust against the special characters above and
keeps the existence check in one place downstream. Blocks are **live-session only**: never persisted,
so restore is text-only, and a rebuilt turn keeps the `attach_image` call and its result — which name
every source — but not the pixels. That is deliberate: the model re-attaches from its own history
when the turn needs the picture again, so nothing keeps a second copy of it. Images are never
rendered in the terminal — the model reads them, and the user opens the file with the OS or a share
link.

---

## 8. The A2A mesh

The A2A protocol runs over the official **A2A-over-MQTT** profile — the `a2a-over-mqtt` SDK — *not* a
self-built binding. The wire protocol, the topics, the QoS rules and the retry ladder are the SDK's;
Slife keeps only harness glue: the plugin, the inbox drain, the channel, task bookkeeping, presence
display and the LLM tool surface. Interop with the rest of the ecosystem follows, and standard A2A is
async-native — a send returns a task id immediately and the result is pushed back later, which is
machinery the harness already had.

**Layout.** `$a2a/v1/{category}/{org}/{unit}/{agent_id}` with four categories — discovery (a retained
Agent Card, with presence carried by an MQTT user property and a last-will), request, reply, and
event (fire-and-forget, QoS 0). Envelopes are JSON-RPC 2.0; the MQTT response-topic and
correlation-data properties carry the requester's reply routing, and a request lacking them is
rejected. A task flows ack → optional working updates → artifact → terminal, with per-task dedup.

**Two connections, distinct client ids.** The SDK `Responder` owns all presence publishing — the
retained card and the will — while a thin outbound driver publishes requests and subscribes to
discovery, its own reply sessions and events; it has no will and announces no presence of its own.
Connecting does not return until **both** are live, because without that gate an early send could be
dropped before the responder subscribed. (The SDK publishes the card only *after* subscribing, so our
own online card appearing on the discovery wildcard is the deterministic "inbound is live" signal.)

**Inbound.** The responder classifies a message by its declared type and blocks on a per-task
completion bridge **only** for a task request — only a request creates a task. A plain message is a
conversation, enqueued task-less with no bridge; a task response is not a task at all and is
acknowledged with nothing enqueued. **Completion is the model's explicit action**, whenever the task
is truly done — a task may take many turns — at which point the bridge resolves and the SDK publishes
the artifact and terminal state. A working keepalive keeps the stream alive while the harness thinks.
External cancellation is **not** turned into a harness preempt: the peer's withdrawal reaches the
model as an inbound message (`type: "cancel_task"`, carrying the peer and the task id) and the model
— the only party that knows whether it is still working on that task — decides what to do with it.
The harness keeps only the unambiguous half: a task whose message has not started yet is dropped
from the inbox outright, since nothing was done and there is nothing to judge. A withdrawal is
surfaced only on a **live** link: the SDK cancels every inflight handler when its own session ends,
so a dropped connection must not be reported as a peer cancel — the requester re-sends the task
instead, and the retry arrives as a fresh request.

A turn that fails is a **harness** failure, not an A2A one: the channel delivered the message and was
done, so the TUI draws no A2A failure line for it.

**Outbound.** Sending is typed, and only one type creates a task — one id serves as the JSON-RPC id,
the task id, the reply correlation and the store key. A conversation type creates no store record. A
task-response send completes an inbound task through the bridge rather than publishing a new request.

Delivery retries use the profile's standard values: re-publish the same payload under a fresh
correlation when no first reply arrives, with bounded exponential backoff and jitter, up to three
attempts; any reply at all — including a deduplicated working replay — confirms delivery and stops
the retries. **Every correlation a task was published under stays routed until its terminal reply
arrives**, because the peer answers on whichever correlation it first saw, which is usually a retry
correlation. Exhaustion plus a subsequently late reply still routes: the push model never abandons a
result.

**Presence** is online/offline only — the profile has no heartbeat, and the old timeout sweep and
busy chip are gone. Transitions reach the model: the plugin queues them and the per-turn prompt
carries only *changes*, read once, while the current roster stays queryable — so a missed event never
leaves the model with stale state. A **cold retained offline card** (a peer already gone before we
subscribed) is cached for the roster but not announced, so a dead card from a past session never
fires a fake offline event.

**The tool surface is the standard operations**, one prefix, nothing waits: `a2a_send_message`
(typed), `a2a_cancel_task`, `a2a_list_agents`, `a2a_broadcast`. There are no async or poll variants
and no `timeout` parameter, because nothing waits. **One envelope for every inbound message**, with
`type` distinguishing a task to answer, an auto-delivered result, a withdrawal, a conversation, or
an event; the TUI strips it and shows the peer prefix instead.

**Config and gating.** Only MQTT is implemented; another transport value disables A2A with a warning
at config load rather than crashing startup. Slife only **probes** the broker — Mosquitto is started
by the user — and a failed probe means the plugin is not started, which is reported through health.
The mesh connects eagerly when the plugin starts so presence is announced at launch, and a failed
eager connect is tolerated: the tools connect lazily on demand.

**Subagents are not part of the mesh.** A worker is a local process with no network identity; it can
send as the parent, but the inbound queue stays with the parent.

On Windows the plugin switches its own event loop to the selector policy, because the MQTT library
uses reader/writer callbacks the Proactor loop does not support — and deliberately inside `main()`,
never at import, so importing the module in a test process cannot change the suite-wide policy.

---

## 9. Surroundings

### 9.1 The UI

A Textual app with no screens and no modals: a chat view, an input, and a status bar. Streaming
thinking and text render into a message widget rebuilt per chunk; a collapsible thinking block
toggles with Enter/Space; tool calls mount collapsible widgets with a status icon and a
primary-argument preview; the status bar shows the model, a thinking badge, the heartbeat indicator,
inbox state, and the last call's context tokens with a usage percentage.

**Every piece of user data is rendered with markup disabled.** Strings Slife constructs may use
markup; tool output, arguments, results, file contents and tool names never do — the wrong path
raises a markup error on ordinary `&`, `[` and `]` in URLs and JSON. The one config-derived value the
status bar interpolates is escaped instead.

Four interaction rules are load-bearing, and each is recorded in Appendix A: the app's `Esc` binding
must **not** be priority, the approval prompt's and model picker's bindings **must** be, a binding
action must be **sync**, and tool widgets are cleared only at the genuine turn-end event.

A bare `.` reply is silence: the widget is discarded rather than rendered. `Ctrl+Y` copies a tool
result, going through the platform clipboard per OS — on Windows via PowerShell, because `clip.exe`
decodes piped input with the console code page and mangles anything non-ASCII.

Restore rebuilds the UI in three phases — reconstruct the message list (repairing turn consistency
first, then mapping channel to display prefix and skipping silence), replace the history and prime the
loop state, then rebuild the widgets inside one batched update with autoscroll suppressed and a
single scroll at the end. Scrolling per widget is the live behaviour and it made restore jitter.

**Following the tail is sticky.** The transcript scrolls to the end only while the reader is at the
end: the scroll offset is watched, so paging up mid-turn keeps its position instead of being undone by
the next streamed token, and coming back down to the tail resumes following. Paging does not depend on
focus either — the prompt forwards PageUp/PageDown to the transcript instead of letting TextArea page
its own draft, which is where those keys otherwise went while typing.

### 9.2 Config and credentials

**Two layers.** The credential store (`credstore`, a standalone package) holds secrets encrypted at
the OS level; `slife.yaml` holds *references* to them, not secrets.

Resolution order is **shell env → credstore → literal default**, for both `${VAR}` and
`${VAR:-default}`. The subtlety is that the fallback form still consults credstore *before* the
literal default — otherwise `${VAR:-default}` would resolve to the default even when the key is held
in the store. Resolution is recursive over strings, lists and dicts. Slife never prompts and never
reads the store's cryptfile backup. The backend is chosen **deterministically by platform**, not by a
keyring priority search: Windows Credential Manager, macOS keychain, WSL's PowerShell bridge into the
Windows store, and the kernel keyring on Linux. An unsupported platform raises clearly.

**The sanitization boundary is the input and output gates.** A secret may appear in plaintext
anywhere inside the process — tool internals, error strings, log lines — and that is *not* a
vulnerability by itself. Judge a finding by two questions: does the secret reach the LLM context as
plaintext, and does it cross the machine trust boundary as plaintext. Everything else, including
plaintext in the session log directory, is hygiene rather than a security issue.

Two gates mask with one pattern engine: **inbound**, on every external message, and **outbound**, on
**every** tool result before it enters the history (tool-call arguments are masked too, as is the
TUI's tool-call preview). The engine is pattern-based with bounded regexes and deliberately **no
generic hex or blob heuristics** — the honest claim is "known-shaped secrets never reach the LLM".
One regex detail is a performance requirement, not style: the key-name patterns bound their repeats
rather than using `*`, because an unbounded repeat backtracked quadratically and froze the parent's
event loop for minutes on a single large relayed line.

**Config sections.** `slife.yaml` carries `env`, `models.providers` + `active_model`,
`job_coding_model`, `agent` (the context policy and iteration knobs), `embeddings`, `wechat`,
`media`, `a2a`, `subagent`, and `plugins.required`. `tools.yaml` is the unified tool config (§4.3)
and its sections are the only knobs — the old `tools:` array in `slife.yaml` is retired. `media` and
`job_coding_model` are read by their plugins rather than by the main config parser; sharefile has its
own file. **There is no user-facing timeout section** — every timeout is a developer-owned constant
(§4.7).

**Config writes preserve the file's comments.** Every writer mutates a dict and calls `write_config`,
which then **edits the existing YAML document** rather than re-serializing the dict: the current text
is loaded as a round-trip document, the delta is applied to it, and the document is dumped. Comments,
indentation, key order and quote style survive because they never round-trip through a dict. This
matters because these files are hand-edited documentation — `sharefile.yaml` is roughly three
quarters comments — and a single config write used to erase all of it. Lists are replaced wholesale
rather than merged element-wise, because a wrong guess about element identity would move a comment
onto the wrong element. The write is then **verified**: it re-parses the edited document and falls
back to a plain render if it does not read back as the intended dict. Losing comments is bad; writing
a config that says something else is worse. The write itself is atomic (temp file, fsync,
`os.replace`, preserving the existing file's mode) and the whole read→mutate→write window is held
under a **cross-process** file lock, because the main process and a plugin child can both be editing
the same file. One rule about reading: a config parse failure **raises**, because returning an empty
dict would let a mutating caller write that empty dict back over the whole config.

### 9.3 Health

`system_health` is the **only** health tool registered for the model; the per-subsystem checks are
internal functions, so nothing re-calls them after the aggregate.

There are two kinds of input. **Static records** are pushed during startup — the active model, the
config's provenance and counts, and a daemon-thread probe of the external toolchain — and the host
facts come from **one recorder** shared by the TUI and a headless worker, so both views list the same
components rather than differing by counts no reader could account for. The toolchain probe runs on a
daemon thread, so a report read in that window states its own **scope** — which facts are not in yet
— rather than letting a smaller count read as a smaller system. **Dynamic checks** are the `check_*`
functions, enumerated **from the plugin registry** rather than hand-listed, with three non-plugin
checks appended for the catalog, the active embedding endpoint, and the watchdog.

**Health is layered on purpose.** Every plugin-backed check probes the plugin's internal `__check`,
which reports only raw technical state — facts and measurements, like a physical-examination report
— and **never triggers a connect**. The harness interprets those facts into levels and remediation
hints; a plugin's `__check` has no levels of its own. The watchdog only monitors processes; it never
introspects application state.

One rule shapes every entry: **`value` is the fact, `hint` is what to do about it**. A healthy entry
carries no hint at all, and a remedy must name a tool that exists — never a `check_*` function, which
is not callable. A startup record is dropped when a live entry covers the same component and key, so
a producer names its component after the live check that re-reports it; the live entry wins in both
directions.

**The report is plain text, not JSON**, and that is a consequence of the result budgets rather than a
style choice: a line-oriented report degrades to *fewer whole lines* when truncated, while a JSON
document degrades to an unparseable fragment. It is built to fit the save-side budget in the first
place, and it puts the verdict first, then problems, then one line per healthy component. Problems
lead; identical entries collapse into one line with a key list; the static environment records come
last.

### 9.4 Logging

Structured log lines: `event_name key1=value1 key2=value2 …`. Event names are snake_case — past tense
for completions, present tense for state. Every line that could carry user input, tool arguments,
tool output or subprocess stderr passes the secret sanitizer first. Nothing is written to stdout,
which is reserved for the TUI and the plugin port signal.

**Log is for developers; the TUI is for the user — two sinks, two audiences.**

- The **session log file** is the full truth: DEBUG and above, every level keeping its real meaning.
  A warning is *never* demoted to info to hide it from the terminal; that corrupts the file and makes
  log-based diagnosis see "all OK" when failures occurred.
- The **console** never emits: the main harness runs its stderr handler above every level, so the
  terminal belongs entirely to the TUI. Plugin and worker processes run stderr at DEBUG — that is a
  diagnostic pipe to the parent, not a user terminal.
- The **TUI** is a pure business channel, decoupled from logs. User-visible status is surfaced
  explicitly through callbacks; the plugin never talks to the TUI, the harness owns surfacing.

**The stderr relay must never die.** An orphaned relay leaves the child's pipe to fill, the child
blocks on its next log write, and a worker hangs mid-task with its task stuck pending forever. So an
over-long line is discarded rather than fatal — and the discarded tail is consumed *through its
newline*, or the next read returns that tail as if it were a fresh line and silently corrupts the
line accounting. The relay's reader limit is raised for the same reason, and the noisiest third-party
loggers are silenced at the source: one SDK dumps the entire request body, which with a large tool
registry is hundreds of kilobytes per request.

### 9.5 Paths

`slife/paths.py` decides where session data lives, in two modes only: **production** (everything
under `~/.slife/`) and **dev** (the project root). Dev detection requires **both** conditions to
hold: the current directory's `pyproject.toml` declares `project.name == "slife"`, *and* the loaded
`slife` package's parent directory **is** that directory. Either check alone misfires — a checkout
whose `pyproject.toml` is present while the loaded package lives in site-packages would scatter data
into the checkout, and a tool installed under a home-local directory that happens to sit under the
current directory would falsely look like dev. A production install always loads from a site-packages
directory whose parent is never the current directory, so it stays production wherever it is launched
from. Agent-scoped paths resolve through the agent-name environment variable rather than a parameter
default, so health tools do not report the default database for every agent.

---

## 10. Project structure

```
slife/
  agent/                # LLM interaction
    loop.py             #   the function-calling loop
    service.py          #   lifecycle manager: plugins, inbox, model switching, save_to_memory
    inbox.py            #   unified message queue + per-source history stores
    message_history.py  #   history, sanitization, turn consistency, the turn→messages builder
    system_prompt.py    #   prompt rendering + the per-turn status prompt
    roles.py            #   Caps — which role holds which harness capability
    llm_client.py       #   backend router + StreamChunk + TokenUsage
    llm_backends/       #   openai · anthropic · openai_responses
    templates/          #   agent.j2 · subagent.j2 · slife.j2 · turn_prompt.j2 · rebuild_messages.j2
    plugins.py          #   PluginLifecycle / PluginRegistry + watchdog
    heartbeat.py · schedules.py · timer.py    # the three timing mechanisms
    multimodal.py       #   image encoding for vision models
  tools/                # builtin tools — auto-discovered from this package
    base.py             #   Tool ABC + make_params / NO_PARAMS / require_params / validate_args
    registry.py         #   the execution pool
    factory.py          #   auto-discovery
    context.py          #   ToolContext — the runtime references every tool receives
    catalog.py + catalog_schema.sql   # CatalogStore — tools.db
    catalog_service.py  #   policy: seeding, snapshot, load/unload matrix, eviction
    semantic.py         #   the host-side catalog vector index
    whitelist.py        #   the always-loaded carve-outs
    meta_tools.py       #   tool_search · func_tool_load · _func_tool_unload
    models.py           #   model_* · attach_image · the two harness tools
    exec.py · skill.py · cli.py · rest_api.py · schedule.py · subagent.py
    system.py · config.py · credentials.py · embeddings.py · timer.py · user_prefs.py
    _config_io.py       #   atomic, comment-preserving, cross-process-locked config writes
  plugins/              # built-in plugins + the central spec
    spec.py             #   PLUGIN_SPECS — the single source of truth
    mcp_gateway/        #   server · connection · client · config · oauth
    memdb/              #   server · store · search · recall · semantic · embeddings · schema.sql
    memfiles/ · wechat/ · sharefile/ · a2a/ · media/ · job_coding/
  a2a/                  # the mesh's transport-agnostic core (mesh, broker, card, task store)
  mcp/                  # host-process MCP infrastructure
    host_server.py      #   slife-as-plugin — exposes the live ToolRegistry
    tool_adapter.py     #   MCPProxyTool — bridges MCP → Tool ABC
    era.py              #   protocol-era negotiation and the listen stream
  subagent/             # headless.py (the worker process) · process.py · identity.py
  ui/                   # Textual TUI: app · chat · handler · tool_display · restore ·
                        #   approval_prompt · model_picker · content · i18n · slife.tcss
  config.py · paths.py · platform.py · net.py · timeouts.py · logfmt.py · timeutil.py
  env.py · schedules.py · threads.py · fifoset.py · server_utils.py · health.py · bootstrap.py

credstore/              # standalone package — cross-platform credential store
cc-switch/              # standalone package — generates ~/.claude/settings.json
local-embed/            # standalone package — the OpenAI-compatible embeddings service
skills/ · jobs/         # seeded to the data dir at install
tests/                  # the AST gates (timeouts, subagent parity) live here
```

---

## Appendix A. Invariants

The rules that must not be broken, and what each prevents. They are collected here because each was
learned the hard way and each is silently violated by a plausible-looking change.

**Memory and context**

1. **Usage is measured or it is zero.** `context_tokens_for` never returns an estimate — a guess
   presented as occupancy is worse than an honest zero. Estimates appear in exactly one place:
   sizing what a recall may add to a context that has not been rebuilt yet.
2. **Every save path is a hard stop, not a skip.** A turn that cannot be persisted is not worth
   running. The flip side is deliberate too: subordinate dependencies never gate readiness, because
   they are uncontrollable and self-healing.
3. **Nothing a model says can empty the context by accident.** A decision keeps what it names and
   *adds* what it recalls, so a recall that answers nothing adds nothing; clearing is the explicit
   `"clear"`. A *failed* discriminator keeps the context too — defaulting it to a recency list would
   change the context on the strength of no decision at all.
4. **A store or tokenizer failure is fatal; no ids is the answer for everything else.** No ids is now
   safe by construction (see 3), so fatalness is not what protects the context — it is kept because a
   broken database and a missing vocabulary are real environment failures, and a plausible-looking
   empty list hides them behind a turn that ran without the history it asked for.
5. **One turn→messages builder, shared by restore and recall**, and the rebuilt list is chronological
   even though membership is by relevance. A rebuilt turn must render byte-identically to the same
   turn restored, or every rebuild costs a prompt-cache miss — which is also why a decision that asks
   for exactly what is in hand rebuilds nothing at all.
6. **The rebuild happens before the user message is added**, because it replaces the message list
   wholesale. A decision that changes nothing never gets there: the context stands as it is.

**Caching and the wire**

7. **Identity and world render once and never change; the per-turn status is a message-stream tool
   pair, never a second system message.** Both exist so the static prefix stays byte-identical and the
   prompt-cache breakpoint lands on it.
8. **A harness tool must be schema-declared**, because Anthropic and OpenAI-Responses reject a tool
   call in history whose name is not in the declared tool list.
9. **The tool list is computed once per request, outside the retry loop**, so every attempt sends
   byte-identical tools; and a mid-turn load **appends**, leaving the request's prefix untouched.
10. **Never emit an async notification from inside a request handler's cancel scope.** Interleaving a
    burst into that scope desyncs the SDK's cancel-scope stack and every later call dies.

**The tool system**

11. **Load state governs what a turn injects, never what a call may do.** The only refused call is one
    with no execution instance behind it, and a refusal names the state and nothing more — a refusal
    that guesses at a remedy tells the caller to do what it has already done.
12. **`load_status` has exactly four writers**: the autoload override, `func_tool_load`,
    `_func_tool_unload`, and eviction. No connectivity verdict is ever written into it.
13. **Removal is a row DELETE, never a status mark**, and the row's embedding chunks go with it
    explicitly rather than through a cascade that may be off.
14. **Off is not down.** `disabled` and `error` are different facts: the config arm may write
    `disabled` over either, but the runtime arm may never resurrect a tool the config switched off.
15. **The injected schema is the catalog's `schema` column** — the stored definition and the wire
    definition are one and the same.
16. **A schema is enforced, not advisory**: closed by default at class definition, validated at the
    single dispatch point. Exceptions are stated, not assumed — a schema declaring its own openness
    keeps it, and a remote server's schema is never rewritten.
17. **The agent's timeout overrides all defaults; a tool's own timeout is a generous backstop.** A
    native tool with an internal run-timeout must expose it as a parameter, or a hidden inner timer
    silently clamps the injection. `≤0` never means "no timeout".
18. **Background calls escape the tool budget** — with no injected timeout, a background call is
    scheduled bare, because the chain default must not govern work that exists to escape it.
19. **Timeout values are read at call time**, never import-captured, so they stay patchable — and a
    hardcoded timeout fails CI, which is what kills the fix-one-drift-another loop.

**Plugins and processes**

20. **The spec table is the only place a plugin is declared.** Adding a plugin is one row plus a
    `server.py` package; nothing else may hard-code a plugin's name.
21. **Readiness is the completed protocol negotiation.** There is no readiness probe, and a dependency
    not required to serve never gates readiness. Never signal the port early: the signal means "ready
    to serve MCP on this port".
22. **A capability must report "not yet" as "not yet", never as "no".** An initialization in flight
    must be awaited by every caller, not just the one that started it — a boolean that answers "no"
    while still loading is indistinguishable from a genuinely unavailable one.
23. **A hard-killed parent runs no cleanup**, so the kill-on-close job object is assigned at spawn,
    before the child can spawn anything of its own. On POSIX the process tree is read before anything
    is signalled, and a group kill is only safe when the child leads its own group.

**Subagents**

24. **A worker is the same loop with a declared, zeroed capability set.** A new capability is
    worker-denied by default and must be granted on purpose; an `is_subagent` branch outside the table
    fails CI.
25. **The harness pushes results; the worker never does**, and a late result is stored, never
    auto-pushed, because the caller was already told it timed out.
26. **A stuck task must be preempted in the child**, because a worker processes tasks serially and one
    stuck task would block every later one.
27. **Config is handed over by file, never by environment** — the resolved config carries plaintext
    keys and the process environment is readable through the process table.
28. **The config's round trip is a fixed point**, derived from the field list rather than written by
    hand — the hand-written version silently dropped nine fields, which is how a worker came to report
    embeddings disabled while its parent reported enabled.

**Process and platform**

29. **Unbounded blocking calls run on daemon threads, never the default executor.** Both shutdown
    paths join every executor worker, so a blocked worker hangs the whole interpreter.
30. **The stderr relay must never die**, and a discarded over-long line must be consumed through its
    newline.
31. **Blocking regexes must bound their repeats.** An unbounded repeat once froze the parent's event
    loop for minutes on a single relayed line.
32. **No global socket defaults**, which would silently change every third-party socket.

**The TUI**

33. **The app's `Esc` is not priority; the approval prompt's and picker's bindings are.** Textual's
    priority pass resolves the app before the focused widget, so a priority `Esc` on the app would
    steal the key from the approval prompt and cancel the loop instead of denying, leaving the prompt
    unresolved. The reverse is equally true: non-priority bindings on a prompt would type `y` into the
    input bar instead.
34. **A binding action must be sync.** Binding actions run inside the key-event handler, so awaiting
    there blocks the message pump and deadlocks the widget that needs the next key event.
35. **A dismissed widget must resolve its future**, or a re-entrancy flag stays stuck and the shortcut
    is dead. The status-bar scroll happens **after layout**, or it pins the view above the fold.
36. **All user data renders with markup disabled**, and **tool widgets are cleared only at the genuine
    turn-end event** — never where the turn is merely *enqueued*, which wiped an in-flight turn's
    widgets and left its rows stuck.

**Configuration**

37. **A config parse failure raises; it never returns an empty dict**, or a mutating caller writes
    that empty dict over the whole config.
38. **Config writes edit the document and are verified before use.** Losing comments is bad; writing a
    config that says something else is worse.
39. **Credstore is consulted before a `${VAR:-default}` literal**, or the default wins over a key that
    is actually held.

## License

MIT
