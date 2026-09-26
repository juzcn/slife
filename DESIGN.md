# Slife — Software Design Description (SDD)

> The design of Slife at the level a person who will change the code needs it: what each subsystem
> is, how it is decomposed, the interfaces between the parts, and the constraints that bind them.
> **The level is deliberate.** Mechanisms are described as rules and consequences, never as
> procedure: an algorithm, a default value, an identifier or a column list is stated in the code,
> which is the only place it cannot go stale. Where this document and the code disagree, **the code
> wins** — and this document should then be corrected to state the rule again rather than the fact.
>
> Two documents sit beside it and are deliberately **not** duplicated here:
> **[README.md](README.md)** is the user's manual (install, configuration, the tool inventory,
> keyboard shortcuts); **[DESIGNER_NOTES.md](DESIGNER_NOTES.md)** is the author's own notebook — the
> philosophy, the trade-offs, the next refactor. Reference-grade detail (column lists, protocol
> tables, per-tool inventories) lives in the code and is cited rather than copied, and the rules this
> design rests on are collected as [Appendix A](#appendix-a-invariants), stated as assertions.

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
│  UI — Textual TUI                                                    │
├──────────────────────────────────────────────────────────────────────┤
│  AgentService                                                        │
│  Unified inbox — human · wechat · heartbeat · system · a2a · subagent │
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
│  platform · config · health · logfmt · paths · timeouts              │
└──────────────────────────────────────────────────────────────────────┘

Outside the process tree, started and owned by the user (never gated on):
  the embeddings daemon · the A2A broker · external MCP servers
```

§10 maps each layer to its directory.

Two MCP directions, deliberately uniform. The main process is an **MCP client** to its own child
plugins; the **mcp-gateway** plugin is simultaneously an MCP client to external servers and an MCP
server to the main process. Slife can also collapse to a server: the host-process MCP server exposes
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
| **Turn** | One user→assistant exchange, persisted as one row. The unit of history, memory and trimming. |
| **Channel** | The sender identity of an inbox message: `human`, `wechat`, `subagent`, `heartbeat`, `system`, or an A2A peer name. Persisted with the turn; by default not part of the LLM context. |
| **Marker** | Machine-generated notation inside a raw message telling the model or the TUI what the text alone does not say. |
| **Recall** | The per-turn selection of history turns that becomes the context. A **system-level** arrangement, not an LLM tool — the harness calls it before each turn. What it does not select stays reachable through the model's own read tools (`turn_search` / `turn_list` / `turn_read`), whose results arrive as tool output. |
| **Harness tool** | An LLM-visible but reserved tool the loop **auto-invokes** rather than the model choosing it: the per-turn status pair, the cut-in check, and image attach. The `_` prefix marks the reserved pair; the image tool is unprefixed because the model also calls it on its own. |
| **Internal tool** | A `__`-prefixed plugin tool serving the main process, filtered out of the schema before registration. |
| **Plugin** | A child process declared by one row in the central plugin spec, speaking MCP over Streamable HTTP. |
| **Worker** | A subagent: a child process running the same loop with a declared, zeroed capability set. |
| **Silence contract** | A bare `.` assistant reply is silence — never rendered, from any turn source. |

The `_` and `__` prefixes are the whole mechanism by which the model can tell a tool it may drive
from one the harness drives, so they are part of the interface rather than naming style.

### Language policy

**Model input is uniformly English**: the system prompt, harness and plugin tool schemas (names,
descriptions, parameter docs, result strings), job schemas and result strings, and logs. External
tools — MCP servers, skills, third-party commands — keep the language of their source; they are
opaque and pass through untranslated.

**The TUI is bilingual (English / Chinese) by OS locale.** Detection happens once, from the OS
itself: the Windows UI-language API (the C locale does not carry the UI language — an English
Windows in Spain reports Spanish), else the POSIX locale variables. Chinese locales select Chinese,
anything else English, degrading to English on failure; a `--lang` flag overrides it. The layer is
one translation accessor over a two-table dictionary, no catalog files, and **a missing key raises
rather than rendering blank**.

Key caps in the status bar and notification *bodies* stay English regardless of locale — translating
a key label breaks the key→action scan, and a notification body is LLM- or system-supplied text
rather than Slife chrome. Tests pin the language to English.

---

## 2. The agent

### 2.1 The turn loop

One function-calling loop. Every tool is registered as an OpenAI function definition in a single
registry; the model decides what to call and when. The loop drives the cycle, and the **inbox**
drives the loop once per queued message.

```
message posted to the inbox
  → per-turn context rebuild (keep ∪ recall — §2.3)
  → add the user message                            (secrets sanitized at this gate)
  → iteration loop:
      cancel check · cut-in check · refresh the injected tool snapshot
      → LLM stream → thinking / text / tool deltas → handler callbacks
      → tool calls? → execute the batch concurrently → continue
      → no tool calls? → return the reply text
  → save the turn to the diary (unconditional — cancel, error, max-iterations alike)
  → trim the context if it is over the ceiling         (§2.2)
  → trim the injected tool set if it is over the threshold   (§4.4)
```

- **Streaming.** Thinking and text tokens are delivered in real time through handler callbacks.
  Tool-call deltas accumulate across chunks and execute as one batch concurrently; approval dialogs
  serialize behind a lock.
- **Iteration limit.** Configurable, checked **live** each iteration so a mid-turn change applies
  immediately. Hitting it returns a cancelled result and notifies the handler. The limit's
  sentinel for "unlimited" is **0**, not a very large number.
- **Cancellation.** The user's cancel key sets an event, checked before each iteration, after each
  stream, and before each tool batch.
- **Background execution.** A per-call flag schedules the tool as a background task and returns a
  task id; the caller polls or cancels by id. The runner sanitizes secrets **at storage time**, and a
  failed background task surfaces with the same failure prefix as a synchronous call. Results are
  pruned past a bound, so a very old poll may answer "Task not found".
- **Turn consistency.** Two idempotent invariants are enforced before a history is persisted and
  again on load: **no orphaned tool calls** (an interrupted turn's call gets a synthetic
  interrupted-result message) and **alternating roles** (a history ending on `user`/`tool` gets a
  closing assistant message). The save, the restore and a subagent's clone all pass through the same
  repair — the clone needs it *guaranteed* rather than accidental, because its snapshot ends on a
  tool call whose results do not exist yet (§6.2).
- **Why a turn stopped early** rides that closing assistant line, standardized as an
  interrupted-with-reason marker. Each layer labels what only it knows: the loop puts its own
  terminal state on the result (cancel, max-iterations), the inbox labels the failure it caught
  (HTTP status and the provider's own code when the SDK exposes them, else the exception's class name
  one hop down its cause chain), and the save point forwards whatever it received into the repair.
  That label is short by construction: the message passes the same secret mask as any user text, is
  collapsed to one line, and is bounded — the line stays in the model's context *and* in the diary
  for the rest of the session, so neither secrets nor a provider's whole JSON body may ride it.
  A repair on **load** has no reason to give — the process that knew it is gone — so it reads as a
  placeholder. A content-filter reject produces no closing line at all, because that turn is rolled
  back rather than saved.
- **The one rollback.** One operation removes the last user message and everything after it. It is
  called from exactly one place — the inbox, on a **content filter** reject — and suppresses the
  save. Everything else keeps the turn and saves it: a malformed *request* (a part the provider
  would not read, an image it could not fetch) is not a bad history, and transient failures are not
  even about the payload. Filtered content is recognised **by name rather than by status code**,
  because providers spell it differently and some signal it only in the message text.
- **A rejected request still costs its attachments.** Any bad-request rejection drops the injected
  image blocks — the one place in a turn where they are removed. Image blocks are session-only
  (there is no column), so they would otherwise ride every later request and be rejected there: one
  failed attach turned into a session that dropped every turn, from every source, until a restart. A
  rejected attachment is not kept, and the TUI says so, so the model is not blamed for not seeing an
  image that is gone.

### 2.2 Context window management

Active history is kept between a **floor** and a **ceiling**, both configured as fractions of the
model's context window.

**Usage is measured, never estimated.** One function is the single source for the current context
size, resolving the last API call's actual prompt plus completion tokens, else the restore-time
value primed from the latest restored turn, else zero. It drives the per-turn prompt, the trim
decision and the status bar. Estimates appear in exactly one place — sizing what a recall may add to
a context that has not been rebuilt yet — and are never presented as usage. Usage is tracked **per
history**, because the main agent has one shared context that every channel writes into while a
worker gets a fresh one-shot history per task.

- **Trim** happens *after* a turn is saved, by which point the last API call's real usage is known.
  At the ceiling, the oldest **complete** turns are removed down to the floor, always keeping the
  current turn. It is internal — no tool call, no LLM-visible pair. The cut is announced by a
  runtime-only note appended to the last assistant message and mirrored in the TUI; the evicted ids
  are dropped from the persisted live-context list (§7.4) and the tracked context range advances by
  the same count. A freshly restored history is exempt from the first-turn trim.
  **Where the check runs is the persistence grant, not the role** (§2.7): a process whose turns are
  not saved has no save point, so its loop runs the same check at the request boundary instead —
  once per iteration, with the token estimate standing in for a usage reading that will never exist.
  That is what bounds a worker's context (§6.2). The hook that maintains the persisted live-context
  list is withheld from it for the same reason: those ids are the parent's turns.
- **There is no summarization of evicted context.** Old turns leave the *context*; they stay in the
  diary forever. Recall is the only way to bring one back.
- **Tool result cap (a hard limit).** One tool result is truncated at a configured multiple of the
  context window, with an explicit marker inside the output. Generous enough that a large-but-real
  file read is never truncated; it caps only outputs that could not fit the window at all.
- **Permanent-memory compaction.** At save, a tool result over a configured size is persisted as a
  head+tail digest naming the original size and the tool to re-run. The live history keeps the full
  result — compaction affects only the persisted copy.
- **Truncation is announced inside the tool output**, never in the system prompt, so the model knows
  re-running retrieves the full version.

**Token counting is real BPE, not a heuristic.** One implementation backs the whole estimator family
— counting, the trim's stop condition, the recall budget — so the members cannot disagree. The
vocabulary is provisioned at install time and pointed at a local cache path, because the tokenizer
library otherwise fetches it over HTTP **with no timeout**: an unreachable fetch would hang the agent
rather than fail. Slife refuses to start on a missing or partial vocabulary rather than mis-count
silently.

### 2.3 Recall — the context is selected

A config flag (on by default) makes the context **decided** rather than accumulated: before every
turn the agent says what to keep of the turns in hand and what to **recall** from memory, and the
turn runs on the two together.

```
run()
  ├─ recall step — once per turn, BEFORE the user message is added
  │    ├─ discriminator → {context, recall}    (one model call; never persisted)
  │    ├─ the store's recall call → turn ids to add, [] or None
  │    ├─ None / unfetchable / nothing asked → keep the existing context, continue
  │    ├─ union = kept ∪ recalled  (chronological)
  │    ├─ publish the union as the live-context list (or clear it)
  │    └─ rebuild the message list from the union
  ├─ add the user message · attach images · per-turn prompt
  └─ iteration loop
        └─ save the turn → the new id is appended to the persisted list
```

The step sits **before** the user message is added because the rebuild replaces the message list
wholesale, so anything appended first — the user message — would be destroyed. The live image blocks
of the turns it replaces go the same way (§7.6). It also stays outside the iteration loop. With the
flag **off** the context grows append-only and the trim bounds it; one persisted list and one
save-append path serve both modes, so the flag flips with no migration. What the flag never changes
is the ceiling.

**The discriminator.** Exactly one model call per turn. It is not in the conversation and nothing it
says is ever shown.

- **Sent**: the agent's **current context** — the live messages, system prompt included, with a
  placeholder standing in for the user message. That shape is load-bearing twice over: the turn being
  recalled is usually a follow-up, and a follow-up names its subject only through the conversation in
  hand, so a query written from the input alone drops that subject and retrieves nothing. And the
  decision's *first* field is answered from that same context: the ids a keep-list names are the ones
  in the footnotes the model can read there, and seeing them is what tells the model what it already
  has, so it does not ask to recall it again.
- **What the instruction states**, in two parts. First the **decision**: what to keep of the turns in
  hand (all of them, some by turn_id, or none), and — with the turn running on what is kept *plus*
  what is recalled — recall's three conditions (a period, a query, a query within a period), together
  with the one thing a model cannot read off the store: the context is bounded, so a recall answers
  with a selection and never with every turn its condition matched, and which part survives follows
  from the condition — a period is read from the end `anchor` names, a query is ranked by relevance
  and so is narrowed by its time bound instead. Then the **cases**: one worked reply per combination
  of the two fields, which is where a value's wording is shown rather than described. Around the two
  sit the current input, one rule — *the query holds what turn recall needs — keywords, short phrases,
  or the full user input, in any combination — matched against the stored turns by a hybrid of
  full-text and semantic search*, the three forms offered as sources rather than as a template — the
  discriminator composes the query, so the rule names what the text may draw on (§7.2) and leaves the
  composition to it — and the reply surface itself, stated once as the reply's fields with their
  values and defaults and nothing beyond them. It cannot be read off a tool: the selector is an **internal** tool the model never sees, so there is no
  LLM-facing schema to quote. The loop is the only caller and its parser reads exactly these two
  fields, which is what keeps the two ends of this contract in step. When the store cannot be reached
  at all there is no call either — the availability check is the gate, so a turn is never spent asking
  a model to decide a recall that cannot run.
- **Cost**: one context-sized call per turn — the pre-turn call is about as expensive as the turn
  itself. That is what judging from the conversation costs.
- **Not sent**: anything beyond that context. The runtime-only turn id on the message that opens each
  turn is stripped.
- **It never persists and never streams** — nothing it sends or receives touches the history, the
  diary or the TUI.
- **It degrades, it does not retry.** A timeout, a provider failure, or a reply that is not the
  requested JSON object all return nothing. Retrying would double the pre-turn latency of a call
  whose fallback — keep the context — is perfectly good.

**What a reply means — six decisions from two independent fields.** `context` is what to keep of the
turns in hand (absent = all of them, empty = none, or the ids to keep); `recall` is what to add
(absent, or the search parameters). The two are decided separately, so the six decisions are their
six combinations and no mode has to be enumerated:

| keep | recall | new context |
|---|---|---|
| all | — | the turns in hand, untouched. The store is not asked. |
| some | — | those turns. |
| none | — | the system prompt alone. |
| all | ✓ | base ∪ recalled |
| some | ✓ | base ∪ recalled |
| none | ✓ | recalled |

Recall's own three shapes are the store's three branches: a **time-only** range (the newest turns in
it, ranked by nothing but time — no similarity cap, because there is no query to measure against); a
**query alone**, a hybrid search over the whole diary; and **query plus a range**, the same search
with both legs windowed. An empty-query branch must run **before** the hybrid legs — they cannot
express "no query": an empty query reaches the full-text index as a syntax error and embeds to
noise.

**The union is what makes "keep this and add that" expressible** — and it is why an empty recall is
now *harmless*. Under the older overriding selection every reply but the empty one discarded the
context it replaced, so a query that merely failed to match emptied it: the turn ran on the system
prompt alone and answered from nothing. A union with nothing is the base. Clearing is therefore only
ever the explicit clear, and nothing a model gets wrong can empty the context by accident. An
explicit clear plus a recall is exactly the old behaviour, so the union is a strict superset of it.

**The recalled set — one fusion, three caps, one order.** The hybrid legs are full-text search (with
a substring fallback for CJK, which the standard tokenizer cannot segment) and vector KNN, fused by
reciprocal rank fusion. The three caps are a turn count, a similarity floor, and a **token budget**:
the floor fraction of the window, narrowed to the headroom below the ceiling by whatever the decision
kept. The floor is the *selection's* size, so it stays the cap when nothing is kept — which is why
the **ceiling**, not the floor, is the bound a kept context is measured against: the trim compacts
*to* the floor, so a live context sits at or above it for most of a session, and subtracting the
floor would grant no headroom and quietly make "keep this and add that" unreachable. The similarity
floor gates the **measured** similarity, never the fused score — a fused score is a function of rank
position and carries no magnitude to threshold. Keyword-leg hits have no measured similarity and are
**exempt**: an exact match is a stronger signal than a cosine neighbourhood, and "no number" is not
evidence against it. The caps are recall's own configuration, never the discriminator's arguments —
it chooses *what to look for*, never how much of it to take, which is why they are absent from the
schema it fills in.

**The axis a condition provides decides the priority.** The caps cannot return everything a condition
matches, so the cut has to fall at one end — and which end is not a free choice: it follows from what
the condition's candidates are *ordered by*. With no query the axis is **time**: the candidates are the
window's turns and `anchor` names the end the caps spend from — `newest` (the default) or `oldest`,
which is the difference between reading a period from its end and reading it from its beginning. With
a query the axis is **relevance**: the caps spend from the relevance head, and time enters only as a
*bound* on the candidate set, so an `anchor` beside a query is not consulted (logged, not silently
dropped). Relevance wins over time, and the request it describes — "the earliest turn about X" — is a
*read* (`turn_search`, `turn_list`, `turn_read`), not a context selection. The two ends are one concept in either
branch, but not one mechanism, and the difference is worth stating plainly:
`{"anchor": "oldest"}` reaches the beginning with no date knowledge at all, while a query can only
reach back by naming a window it has to guess. Render order is chronological in every case, because
that is the restore contract and not a choice.

**A time-only recall reaches the end it names.** `anchor` also stands alone: with no range it is an
end of the *whole* history, which is how "look at our earliest records" is asked without naming a
window — a deliberate widening of the empty-call rule, since an anchor names a condition where
nothing did before. The anchored turn is taken **whatever it costs** (a turn larger than the whole
budget is recalled *alone*, which is the answer and not a failure, and one turn's overshoot is what
the ceiling absorbs — the same bound the trim enforces), and the run behind it is **contiguous**: the
scan stops at the first turn that does not fit rather than skipping it. Skipping is right under
relevance, where a candidate behind an unaffordable one is still a candidate; a time window is
adjacency instead, so a hole is a piece of the conversation missing with nothing in the result to say
so — and under time order the turn a skip drops first is the newest, the one the answer is most often
about.

**A keep-list is a statement about the context in hand**, read as an intersection: an id that is not
there names nothing, and is not a way to pull an arbitrary row into the context past every cap and
the budget. The ids are the ones in the footnote of the message that opens each turn — which is why
**every** in-context turn carries one, autonomous turns included. Suppressing the footnote on
heartbeat / schedule / timer turns (the old rule) made them unnameable, so a keep-list silently
dropped them — including scheduled turns that did real work — with nothing in the conversation to
say why.

**The floor is calibrated, not chosen.** A cosine scale belongs to the pair that produces it — the
embedding model *and* the text the index holds — so the similarity floor is a measured number,
re-measured when either changes; the shipped value came from a recorded session and is not a
plausible-looking default. Getting it wrong costs more than a mistuned knob would, because the
recalled set *joins* the context: a floor below the noise band does not degrade gracefully, it adds an
arbitrary turn as though it had been matched, and the union then carries it. The semantic leg's scale
depends on what the index holds, which is not the raw turn: see §7.2 for what the embedded text is,
and why tool *results* are absent from it.

**Order is chronological even though membership is by relevance**, because the list order is the
restore contract: a rebuilt turn must render byte-identically to the same turn restored. Both paths
share one builder — a difference would cost a prompt-cache miss every turn. Which is also why the
rebuild is **skipped when the decision asks for exactly what is in hand**: nothing was added and
nothing dropped, so re-rendering the same turns from the store would cost a round-trip, the turn's
live image blocks and the prompt-cache prefix to arrive at the identical list.

**The recalled set is joined, not reconciled** — there is no incumbent to defend and no need to
exclude turns already in context: a turn the recall names that the decision also kept is *the same
turn*, and the union is by id.

**The store's answer is ids, or nothing, but never an error.** The recall call returns the turn ids
and nothing else — a degraded semantic leg does not change the recalled set, so it is logged rather
than answered with. Everything that is not a fatal environment failure is answered as **no ids**: a
time bound the grammar rejects, a query the store cannot parse, an unexpected pipeline failure. That
answer is safe by construction — no ids *adds* nothing — so fatalness is not what protects the
context. Two things are **fatal** instead — a store failure and an unusable tokenizer, since every
turn's cost and so the budget come from it — because a broken database and a missing vocabulary are
real environment failures, and answering them with a plausible-looking empty list hides them behind a
turn that quietly ran without the history it asked for.

**The model's own way into the turns DB is the other half of the arrangement.** `turn_search`,
`turn_list` and `turn_read` mirror the cabinet's, and all three only ever *read* — none touches the
context. The selector that does feed the context is the harness's, and it is internal for exactly that
reason: changing the conversation under the model is not something the model asks for.

The division of labour is the point. The selection is a **system-level** arrangement: decided before
the turn and on every turn, so the context a turn runs on is rebuilt whether or not the model thinks
to do anything about it. The tools are the other direction — **the model's own** — and that is what
keeps the selection from being the only door: what it does not select is still reachable, because the
model can look for it and the answer arrives as tool output in the conversation it is already
reading. So a query the discriminator never wrote, or a turn the caps cut, is a round-trip and not a
dead end, and neither path has to be complete on its own.

And the two do not search alike. The selection has exactly one search — hybrid — while `turn_search`
also offers **grep**, a real regex (§7.2): a partial spelling, a path, a symbol, a word glued inside a
longer CJK run, none of which the tokenizer or the vector sees. It is the mode the model reaches for
increasingly often, and it is one the selection cannot ask for — so a missed selection costs a
round-trip and buys a search the discriminator was never given.

**What the decision does to the context.** The rebuilt set is the union above, built from the stored
rows by the same builder restore uses — so a context is reproducible from its id list, and a restart
renders what the live session rendered. Clearing is explicit, and the persisted list is emptied with
it. Four things leave the context untouched instead: nothing was asked for; no reply came back; the
store returned nothing; or the turns cannot be fetched. Nothing was learned about what the turn
needs, so a guess is not an improvement on what is already there.

**Workers never rebuild** — a worker's history is one-shot per task, so there is nothing to select
from. The role decides this, not the config.

### 2.4 The system prompt

The prompt splits **identity** from **world** so each role reads one coherent document:

- **Identity** — the role's own template (main agent / worker): who the agent is. Role framing only;
  the one part that carries persona.
- **World** — one shared template included by both: the runtime spec — context policy, host
  platform, workspace paths, marker expectations, the credential chain, tool naming, skills, jobs,
  subagents, and mesh info when configured. **Byte-identical in both roles.**
- **Dynamic** — the per-turn status prompt, rendered by the harness tool once per turn (§2.5).

Identity + world change only on a model switch or a user-preference write, and **always from the
role's own identity template** — the grant decides which — because re-rendering the main agent's
identity for a worker replaced its framing for every later task in that process. Between those two
events the static prefix of every request stays byte-identical and the prompt-cache breakpoint lands
on it. That is the whole reason the per-turn status is a **message-stream tool pair** rather than a
second system message.

Two derived rules: the world spec carries **project-specific facts only** — anything the model can
infer from tool schemas or training data does not belong; and the prompt **forbids nothing by list**.
The prefix conventions and the meta-parameters are each explained once, structurally, so the model
reads the mechanism rather than a denylist.

### 2.5 Channels, markers and harness tool-pairs

Three orthogonal notions describe how Slife introduces information on its own initiative: a
**channel** (the sender identity of an inbox message — recoverable from the message alone, persisted
with the turn, by default **not** part of the LLM context), a **marker** (machine-generated notation
inside a raw message, telling the model or the TUI what the text alone does not say), and a **harness
tool-pair** (a harness tool the loop auto-invokes — §1's Vocabulary — contributing an assistant tool
call plus its result to the history).

**A marker never determines a channel and a channel never forces a marker.** A scheduled task is the
canonical example: its trigger is a schedule marker riding the **system** channel, and its completion
arrives on the **subagent** channel carrying no schedule marker at all.

| Channel | Sender | TUI |
|---|---|---|
| `human` | the keyboard operator | `You> ` |
| `wechat` | WeChat peer | `Wechat> ` |
| `subagent` | local worker completion | `Subagent(<name>)> ` |
| `heartbeat` | Slife — the periodic autonomous window | trigger hidden; reply marked autonomous |
| `system` | Slife — schedule / timer triggers | trigger hidden |
| `a2a` | a mesh peer | `A2A(<peer>)> ` |

Each channel has a corresponding marker carrying the structured facts it needs; the marker's exact
spelling is an interface detail stated in the code. The **system** channel is never user input, and
its turns are filtered from the TUI by both the channel and the marker text. Classification helpers
match on the prefix so live rendering and session restore agree. A separate runtime-only footnote
decorates messages that already exist rather than injecting a turn: it is appended to a user message
after the turn saves so the next call can reference the turn by id.

**The per-turn status prompt** carries current time, context usage, changed model / working directory
/ shell, mesh peer presence events since the last turn, open failed or missed scheduled runs, and the
one-shot "system restarted" flag. It is a tool pair, deliberately, so it persists and restores as a
normal part of the turn — it must not live in the static system prompt, where changing every turn
would evict the cached prefix.

- **Injected by the loop, not chosen by the model**: the loop writes the pair into the history
  unconditionally at the top of every turn, computing context usage once and sharing it with the trim
  and the status bar.
- It executes the tool **directly**, not through the tool-execution path: no approval gate, no
  timeout wrap, no background wrapping.
- It must be a **schema-declared builtin tool**, not a history-layer fabrication — the Responses and
  Messages backends reject a tool call in history whose name is not in the declared tool list.
- Harness tools sit in the always-injected whitelist (§4.4) — never evicted, not unloadable — so the
  threshold squeeze cannot take away the mechanism the loop drives every turn.

**The cut-in check** is the zero-argument counterpart, auto-invoked at each *iteration boundary* when
a queued message may cut into the running turn. It is a mode — on by default, toggled at runtime —
and when off the boundary check is skipped entirely. The check asks the inbox whether the queue is
non-empty (no channel filter) and the tool's execution pulls the first queued message, returning its
bare text: the content already carries its marker, so no wrapper is needed. The extraction is gated
behind the same cancel guard so a cancelled turn never drops the queued message. It is **main agent
only**, and the injected message is a live input the model addresses in the same turn.

### 2.6 Timing — heartbeat, schedules, timers

The agent is otherwise purely user-driven; these three mechanisms give it time.

**Heartbeat.** While idle, every configured interval (disableable) the service posts a heartbeat
message, which runs as a normal turn with its own history and is saved like any other. The reply
contract: real content if the agent has something worth saying proactively, otherwise exactly `.`.
The loop skips a beat when the inbox is busy or has pending work, so it never competes with real
input. **Main agent only** — a worker is task-driven.

**Scheduled tasks** are three separated concerns — *timing*, *execution*, *record*:

- **Timing.** A poller recomputes each enabled task's next fire **from the DB, not from memory**: the
  anchor is the newest due time across the task's runs (a fire is never re-detected), falling back to
  the task's creation. The poller **fires only**: against a grace window, a fire due within it is
  fired; anything older means slife was down, which is the startup sweep's concern. An in-memory
  pending-fire guard keeps the poll from re-firing mid-turn.
- **Trigger → execution.** The poller injects a schedule trigger on the **system** channel; the run
  is recorded when the agent *dispatches*, not at fire time. The agent handles the trigger by
  delegating: one dispatch tool — also used to backfill — records a pending run, spawns or reuses the
  worker named after the task, and sends it deterministic task text instructing it to save a report
  and notify the user. Completion rides the ordinary subagent auto-push (§6.3).
- **Record.** Tasks, runs and reports live in the cabinet DB. A report bound to a task backfills the
  newest unlinked run at the store layer — pending → ran is the **only** success writeback;
  everything else is failed-by-default.
- **Failed and missed runs are settled at startup.** A one-shot sweep reaps every surviving pending
  run to failed (a run from a dead process can never complete) and marks fires due while slife was
  down as missed. Both surface through the per-turn prompt and can be backfilled or closed. Tasks
  fire only while slife is running.

**Timer.** A pause-and-resume tool pauses the current turn and resumes it later by scheduling an
in-memory wake that posts on the system channel. It dies with the process — anything that must
survive a restart is a scheduled task.

### 2.7 Roles — the main agent and the worker

Both roles run the **identical** loop. What differs is the harness around it, and that difference is
**declared once**, as a table of capabilities: one field per resource or policy, read through a
single accessor for the role. A capability is a *grant* — the process either owns the resource or
holds the policy — and the main agent holds every one of them while a worker holds none.

This is written down rather than spread around because it used to be ~two dozen scattered
role-condition branches, which made a worker's capability set an *emergent* property of wherever a
gate happened to be written — so a capability added to the main agent's path could silently never
reach a worker. Because the worker's set is derived by zeroing **every** field, a newly added
capability is worker-denied by default. Two guards keep it honest: a static-source gate that fails on
any role branch outside the table, and a parity test asserting the two roles' observable difference
is exactly what the table declares.

The config a worker inherits is lossless by construction for the same reason: its serialization and
deserialization are derived from one field list rather than hand-written, so a field cannot be
dropped silently.

---

## 3. LLM backends

### 3.1 The router and the unified stream

Three backends, equal citizens. The internal message format is OpenAI Chat Completions; each backend
owns its own wire conversion, and all three produce the same chunk type.

```
LLMClient (thin router)
  ├── OpenAIBackend           api: "openai-completions"   (the default branch)
  ├── AnthropicBackend        api: "anthropic-messages"
  └── OpenAIResponsesBackend  api: "openai-responses"

StreamChunk(thinking=…, content=…, tool_deltas=…, usage=…)
```

The whole contract the loop uses is two methods: a **batch** call (text + usage only — **no tool-call
support**) and a **streaming** call. Tool calling lives exclusively on the streaming path.
Tool-call deltas have one uniform shape across backends.

### 3.2 Per-backend notes

| Backend | Thinking | Notes |
|---|---|---|
| **OpenAI Completions** | a reasoning block in the request's extra body | The usage block is handled **before** the empty-choices guard — the final usage chunk carries no choices, so otherwise no usage would ever be emitted and context accounting would collapse to an estimate. |
| **Anthropic Messages** | a thinking block whose token budget is derived from the output limit | Sampling parameters travel in the request's extra body. |
| **OpenAI Responses** | a reasoning-effort field | Emits the Responses API's native function-call history items, not the Chat-Completions shape. |

**Thinking is requested per backend, and per-model compatibility overrides exist for the models that
cannot take the request.** A gateway may reject the enabled shape while reasoning natively, or accept
only one spelling of the field, so a per-model compatibility block overrides how thinking is asked
for — including asking for it not at all.

**Prompt caching (Anthropic).** Each system message becomes a system content block and the **last**
one is marked as the ephemeral cache breakpoint — the static base prompt becomes the cache breakpoint
(§2.4). On by default for the first-party endpoint, off for compatible providers that may reject the
field, overridable per model.

**Anthropic alternation is mandatory.** Tool results are **coalesced** into one user message per
batch, and a following user text message is merged into that same block — consecutive user messages
are rejected by several providers. An assistant with no text and no tool calls gets a single empty
text block rather than an empty content array.

**Two outbound normalisations are wire requirements, not tidying.** An empty assistant message must
be filled, and with thinking enabled every assistant message must carry a reasoning key — the
synthetic harness one included — or certain providers reject the request. Both act on the outbound
*copy*; storage is untouched.

### 3.3 The stream failure contract

One contract, every source. Transient transport failures — the HTTP stack's errors and the SDKs' own
connection, timeout and stall errors — are retried with bounded linear backoff. The main agent,
heartbeat, WeChat and the mesh share it; **workers deliberately do not** (§6.4). Bad-request,
content-filter and auth errors are not retried here. The history is kept intact on transient
failures; only the bad-request class rolls back.

**The stall watchdog is an inactivity timer, not a total one.** Every stream read is wrapped in a
timeout that **resets on each chunk**, so a provider that answers `200 OK` and then sends nothing is
cut after the idle bound while a slow-but-live generation is never cut. A separate opt-in total cap
exists and is set only for workers.

**Model switching** is config + runtime only, no API call: a switch validates, persists the active
model, and rebuilds the client, loop parameters and system prompt. Context-usage state is
deliberately **not** wiped — it self-corrects on the next API call. Setting a model is an **upsert
that merges, not replaces**, so a partial update keeps the model's other fields. Two model shapes are
accepted from config — an explicit provider list or a flat list — and a write keeps whichever shape
the file already has, **in that shape's own idiom, never converting**: under an explicit provider the
loader reads the id whole, so a converted flat-list id comes back double-prefixed, renaming every
model and stranding the active model. The inline model picker in the TUI is an **emergency escape**
for when the current model is unavailable and the model cannot switch itself — see Appendix A.

---

## 4. The tool system

### 4.1 The Tool ABC and schema authoring

A tool declares its name, description, JSON Schema parameters, category, and an async execute
returning a string. Required fields are validated at class-definition time. Per-tool construction
from the tool config is supported, carrying runtime references (registry, config, MCP client,
history).

**Auto-discovery.** Every module in the tools package is imported and every declared tool subclass is
walked recursively, so a new file is picked up automatically. Disabled tools are still registered and
refuse **at execute time** rather than being silently absent — a tool like image attach reports that
the active model has no vision instead of vanishing.

**A schema is enforced, not advisory.** Class definition adds `additionalProperties: false` to every
harness-authored schema that does not state its own answer, and each call is validated against it at
the single dispatch point before the tool runs. A required parameter that never arrived, or a name
the tool does not declare, returns an error naming the parameters that *do* exist. This closes the
failure where a guessed parameter name landed in the catch-all, was dropped without a trace, and the
required parameter silently fell back to its default while the call reported success. Closure is
applied at class definition because authoring style is not the contract — schemas are written two
ways, and closing only one style would leave the majority swallowing typos. Two deliberate
exceptions: a schema that states its own openness keeps that answer, and a **remote** schema is never
**closed** — a third-party server's schema is the server's contract to declare. The adapter does
normalise one thing on the way in: a remote input schema that is not an object schema is rewritten to
`type: object`, keeping every other key, so a definition reference cannot dangle.

**The schema is the model's only view of the tool**, so it is documentation rather than instructions:
the description and the per-parameter docs state what the tool does and how its arguments are used,
and never when to call it. The mechanical half of that contract is a parser's: a plugin tool's
parameter docs arrive as a docstring, which the MCP framework parses into the input schema.

### 4.2 Families and naming

Three families exist by **ownership** — indistinguishable to the model at the call site.

| Family | Owner | Categories | What it is |
|---|---|---|---|
| **system** | the developer | `builtin`, `plugin` | Slife ships it: a module in the tools package, or a built-in plugin's own tool |
| **job** | the user | `job` | code the user wrote: a public function in the jobs directory |
| **external** | a third party | `mcp`, `rest-api` | someone else's server, reached through the gateway |

`skill` and `cli` belong to none: nothing owns them, there is nothing to spawn and nothing to
register — the row *is* the thing (a playbook file, a config entry). They are tools all the same,
reached through search and then by using them. Because a skill's row *is* its directory, a skill name
is a path: setting or removing one whose path resolves to the skills **root** is refused, because a
containment check accepts a path relative to itself and a caller that only checked containment would
replace or remove the whole directory.

**Naming rules are fixed.** System tools are bare. A job is prefixed with its owning namespace. An
external tool is `{server}__{tool}`. Two source-fed families are namespaced in the catalog: a skill
row and a cli row each by their source key. A name is the row's identity — the primary key, the
embeddings' foreign key, the key every search result is merged by — so two families cannot share one,
and sharing is not a mistake to prevent: the same name may legitimately be both a CLI and the skill
documenting it.

**Semantic identity is not implementation.** Listing the MCP servers returns only that section and
listing REST APIs only theirs, even though a REST API is currently served by a process that shares
the transport, the pool and the config shape. That sharing is an implementation choice, not an
identity, and it is not allowed to show on the model's surface: returning a REST API under the MCP
listing would report servers the MCP management tools do not manage, indistinguishable from the ones
they do. All five MCP tools are gated the same way — naming a REST API is refused with the twin that
owns it. The gate reads a category the *caller* declares, so the implementation is written once with
two thin registrations each — and that category is never a schema parameter: a model that could
declare its own family would declare its way past the gate.

### 4.3 The catalog — the tool database

**The catalog is the load/unload model.** Every tool is a row in one shared database, read by the
main agent, subagents and the gateway child alike. One store class owns all SQL; policy lives in a
service above it. The schema file is authoritative for the columns.

**Every catalog mutator is write-owner only.** A worker's view of a source is partial, and an
upsert-then-purge from it would delete the rows it merely could not see, so the category sync and the
external mirror refuse a worker outright. The load/unload pair is the one write the roles share,
because it is the model's own decision either of them may make.

Two absences carry design weight. There is no derived `type` column, because the load-state question
is a membership test over the function categories rather than a second thing to write and keep in
sync. And the stored schema text is **both** the injected definition and the semantic index's
document — a row whose schema text is empty has nothing to embed and is invisible to the semantic
leg.

The two running-state columns answer deliberately separate questions: **status** is what the config
says (or what the runtime found), **load state** is what the *model* decided. **No column is
nullable** — "not applicable" is a value, never NULL, so every read is a plain comparison.

**Status is one column with three exclusive values**, because they answer one question and do not
coexist. *Enabled* is the ordinary state; *disabled* means the config switched it off (per **server**
for external servers — all of its tools move together, there is no per-tool enable — per entry
otherwise); *error* means its owner is unusable right now. Two writers move that value, each owning
one transition, and each is guarded: config writes disabled ↔ enabled, the runtime writes enabled →
error and back. So a server switched off while it was down is *disabled* — **off is not down** — and
coming back up does not resurrect a tool the config switched off.

Load state is the database's whole reason to exist, and **no verdict is ever written into it**:
writing the connectivity mark there would destroy the load state it landed on, so a blip would reset
every tool the model had loaded. It has exactly four writers (§4.4).

**Removal is a row delete, never a status mark** — one statement per set, so a whole server, a
category mirror or a single vanished tool drops cheaply, while a tool that is merely switched off
keeps its row with a disabled status. The families differ only in who reports the death: the tool
config for an external server leaving it, the server's own tool list for a tool it stopped
publishing, the source mirror for a skill/cli/job, and the boot seed for a builtin whose class left
the code.

**There is no server table.** Which servers to bring up is decided by the tool config, what is live
right now is answered by the gateway's pool, and the database records the RESULT on the tool rows. A
server table would be a third copy of facts that already have owners — and one that goes stale the
moment the gateway child dies.

**Effective status** is derived from the row alone, with status outranking load state: disabled →
disabled; error → error; a function row → its load state; a skill/cli row → enabled. One label per
fact. The first two never overwrite the third, which is what lets a loaded tool come back loaded.
**Injection takes the function rows that are enabled and loaded** — that single predicate is the
injection query, the effective-status rule, and the eviction budget and victim set alike, so a row
whose owner is down or which the config switched off can neither absorb a slot nor be evicted, and
the four move together.

**Configuration** carries one section per category plus the load threshold. Every entry carries the
same two policy flags:

- **enabled** mirrors onto the row's status. For a skill the switch is written to the config under
  that file's lock, with the in-memory disabled set moved alongside it, because a write that left the
  mirror stale made the switch a per-process no-op until the next restart.
- **autoload** means injected from session start and never evicted — per *tool* where a tool has its
  own name, per *server* for external servers, because an external tool's name is not knowable before
  its server connects. It is accepted and inert for skills and CLIs, which have no load state to
  seed. Unlike every other mirror decision, `autoload` also **overrides** an existing row's state —
  the one place config wins over the model.

A REST-API entry *is* a standard MCP server that lives in the other section. **The section is the
whole fact** — nothing is tagged for it. The recorded source is where a definition was *downloaded*
from, which is a different question.

### 4.4 Load, inject, evict

**Boot seeding.** Every registered tool gets a row. A **new** row is born loaded only from the two
autoload sources — the whitelist and autoload entries — and unloaded otherwise. An **existing** row
keeps whatever the model decided, with the autoload exception above. Every external row is then
marked error, because no server is up yet.

**Per-request injection.** Before **every** LLM request the loop refreshes a snapshot — the loaded
rows that are not config-disabled, plus the whitelist — and builds the request's function list from
the stored schema. The registry key always wins over the descriptor's bare name, so the injected name
is exactly what execution resolves. Per-request is what makes loading mean anything: a tool loaded in
one iteration is in the next iteration's request. Because the list is in registry insertion order, a
proxy materialized mid-turn **appends** — the request's prefix is untouched and the prompt cache
survives the load. One request's own retries reuse the list computed for it, so every attempt sends
byte-identical tools.

**Threshold eviction** runs at the turn boundary: if the loaded count exceeds the configured
threshold, the excess is dropped least-recently-used-first, protected by the same two autoload
sources that seed a row loaded. **The budget and the victim set are the one injectable predicate of
§4.3** — function rows that are enabled and loaded — so a row whose owner is down or which the config
switched off can neither absorb a slot nor be evicted. Two rules keep the LRU honest — seeding never
touches the last-used mark, and **every successful execute bumps it**, so a tool used this turn is
never the next victim. Eviction is main-owner only; a worker inherits the curator's budget and never
squeezes it.

**Evicted tools stay registered and stay callable.** Eviction takes them out of the injection
snapshot and nothing else. **Load state governs what a turn injects, never what a call may do.** What
an evicted tool loses is its schema, and the next load restores that.

**Load state has exactly four writers**: the autoload override, the load tool, the unload tool, and
eviction. Everything else about a row's state lives in status's two lanes, which is what keeps a
disconnect or a restart from costing the model its set.

**The whitelist** is the always-injected carve-out: the harness tools, the tool-system meta tools,
and a couple of calls every session reaches for. It never evicts and is not unloadable — a design
constant, not configurable.

### 4.5 Discovery — search and load

**Search spans every category.** Its filters *are* the catalog's columns — category, source, status,
load state — one parameter per column, so the surface cannot drift from the table. A filter the agent
does not supply contributes no clause at all, and every one is a real SQL predicate so filtering
happens before the limit.

Three retrieval routes, one row shape: **regex** (a real pattern, so an abbreviated spelling matches;
an invalid pattern is reported, never a silent no-match), **keyword** (ranked full-text search,
CJK-routed to a substring fallback), and **hybrid** (keyword + semantic KNN, merged by reciprocal
rank fusion). **An empty query browses**: with no text to match it returns the rows passing the
filters, which is also how a family gets enumerated. Results are scored on one 0–1 scale shared with
turn search and cabinet search so the numbers are comparable; **a keyword-only hit carries no score,
because nothing measured it and inventing a number would be a lie about the match.**

**Loading a function tool** is by full name. Refusals come from the effective status and name it —
unknown → "search for it"; disabled → "enable it first"; error → "its server is not up right now,
check it, then retry". On success the row flips to loaded and the tool is in the **very next**
request. For external rows it also materializes the execution proxy from the row's schema descriptor
— loading and materialization are the same step, driven by the row.

**What a load does not do is unlock anything.** It never gates a call. A tool with an execution
instance is callable whether or not the model loaded it, so load-and-call in one message is
legitimate — the load is what puts the tool's **schema** in front of the model, which is what makes
the arguments read rather than guessed.

**Unloading** is the spare ticket: it frees a slot in the tool list without making the tool
uncallable, and is refused for the whitelist. One family is the exception on the execution side — an
external proxy is unregistered along with its row, because that proxy holds a live client. That is a
resource decision, not a gate.

### 4.6 Results, errors and meta-parameters

Every tool returns a single string. The failure contract is one rule, one token: **a failed call
returns a string starting with `Error:`**. The harness derives the persisted error flag from exactly
that prefix at both dispatch sites, judged **before** the argument-truncation marker is prepended, so
a failed call still reads as an error even when the marker leads the text. The flag is stored on the
tool message and session restore reads the stored flag rather than re-deriving it. There is
deliberately no second failure token.

**`Error:` means the tool ran and failed, nothing else.** The last-used bookkeeping that follows a
successful execute sits **outside** the execution's error handling: the tool has already run — it may
have written a config, sent a message, deleted a file — so a bookkeeping failure on the shared
catalog database must not report a completed run as a failure, because the model's answer to that is
to retry a non-idempotent action.

**Meta-parameters.** Tool schemas sent to the model carry **business parameters only**. Three
meta-parameters — timeout, background, approve — are declared once in the system prompt and popped
before dispatch; re-describing them on each of ~60 schemas would be the single biggest per-request
context tax.

### 4.7 Timeouts and cadences

**One registry, and the values are code.** Every time value reads at call time from a typed set of
defaults, grouped by role — developer-owned, with no user-facing config section and no second seat. A
structurally invalid edit fails loudly at import, and consumers do **call-time lookups**, never
import-captured constants, so tests can patch a value. Two gates keep it evergreen: a static-source
scanner that fails CI on any hardcoded time value, and a companion that fails on a declared-but-
unconsumed key.

**Budgets and cadences share the registry, and the split is the role.** A *budget* bounds a single
await (owner-of-await); a *cadence* sets how often something runs — a poll period, a heartbeat, a
backoff step, a session lifetime. A cadence never bounds an await and a budget never sets a cadence.
Both belong here: a cadence left as a module constant is a second seat for a value the next reader
has to go find. The scanner draws no line between them: it fires on any literal time value — a
time-style name, folded arithmetic, a bare number, a call's time-style keyword, a literal sleep — in
the tests too, where a deliberate magnitude is marked as exempt. Two exclusions are stated rather
than inferred: a zero-length sleep is a scheduling yield rather than a duration, and a *count* of
retries is not a time value at all, so it is named as a count and the gate can tell.

**The model, in five rules.** (1) *Owner-of-await*: every await that can block has a bound, owned by
the layer that awaits it; a callee never sets a total for its caller. (2) **The only sanctioned
"total" is the tool-call budget** — there is no turn deadline and no chain-decreasing budgets.
Long-running-but-live work is bounded by *inactivity* watchdogs that reset on progress, never a wall
clock. (3) *Slots are contracts*: some values mirror an upstream wire contract and must be replicated
faithfully, not "improved". (4) *One semantic, one value*. (5) *No global defaults* — a process-wide
socket timeout would silently change every third-party socket.

**One bound outside that rule: the startup sync.** A single budget covers the boot tool sync — it
bounds each mirror's wait on the gateway, decides when the tool-set line reports what the set has
instead of waiting, and is the age at which a wedged reconcile pass may be abandoned by the next one.
Its invariant is that it must be no smaller than the gateway's own establishment and listing bounds
summed, which is what keeps the line honest: it may not claim "synced" before those have expired. The
corollary is why it had to be written down: **no blocking work may run on an event loop** — a
synchronous subprocess suspends every timer in that process, so one exempted blocking call that
freezes the loop invalidates every other deadline in it (measured: over two minutes of frozen gateway
loop, thirteen expired connect bounds firing at once). Blocking work goes to a daemon thread.

**Tool-execution precedence — one value per tool call.**

1. The agent injects a positive value (the timeout meta-parameter, or the tool's own timeout
   argument) → that value is the bound. **The agent's timeout overrides all system defaults.** Zero,
   negative or missing are not overrides: they mean "use the default", never "no timeout".
2. Otherwise the chain default applies: a tool **with** a native timeout parameter keeps its own
   registry value and is the single enforcer; a tool **without** one gets the per-call work budget.

Enforcement is exactly one timer per call — native-timeout tools are never wrapped by the loop. A
tool's own default is therefore a **generous backstop**, never the operative bound for a call
carrying an effective injected value; a tight native default would preempt it. If a native tool has
an internal run-timeout, it **must** expose it as a parameter — a hidden inner timer would silently
clamp the injected value.

**Backgrounded calls are the exception.** A background call with an injected timeout follows the same
mapping; **without** one it is scheduled bare. The chain default is deliberately not applied:
backgrounding exists to escape the in-turn budget, so its bound must not govern background execution.

### 4.8 The approval gate

Approval is **model-driven** — pure model judgment. There is no `requires_approval` flag on any tool
or MCP server; the model decides per call by setting the approve meta-parameter (§4.6) rather than
any schema carrying it. Execution then pauses and an inline prompt row is mounted in the chat stream
— a row, not a modal, so the transcript stays readable while the loop is blocked.

The gate's guarantee is that the loop is never left waiting on a prompt nothing can answer. Prompts
**serialize behind one lock**, so a batch's concurrent calls cannot stack two dialogs. The wait is a
race between the user's answer and the turn's cancel, and a cancel **denies** the prompt rather than
abandoning it — a prompt that has lost focus, to the model picker or to anything else, must not hold
the turn open, because every later message would queue behind it. A denied call returns an error
naming the denial, mounts no tool widget (the prompt row itself carries the rejection state), and the
rest of the batch proceeds. A process with no handler — a headless worker — **auto-approves**: the
decision belongs to whoever is watching, and nobody is.

---

## 5. Plugins

Nine built-in plugins run as independent child processes: `local-embed`, `mcp-gateway`, `memdb`,
`memfiles`, `wechat`, `sharefile`, `a2a`, `media`, `job-coding`. There is **no external-plugin
mechanism** — third-party capability enters only as a standard MCP server in the tool config,
connected by the internal gateway plugin.

### 5.1 The spec — one source of truth

Every child plugin is declared by one spec row in one ordered table. Nothing else in the harness
hard-codes a plugin's module, enablement or glue: every name-keyed table that used to exist — the
start chain, the connect-glue map, the health check list, the tool-adapter route set, the
reserved-name list — is now a lookup into this one table. A row describes one plugin: its name and
module, where its live client lands in the shared tool context, whether it is the gateway, the
service method names for its enable and after-ready hooks, whether it has a health check, and its
semantic-reload tool.

The spec module is **stdlib-only on purpose**, so the MCP child, the health tools and the tool
adapter can import it without pulling in the service. Per-plugin *behaviour* is declared as a method
**name string**, resolved once at service construction (which asserts a spec never names a missing
method). An external MCP server may not take a built-in plugin's name.

**Adding a plugin is one spec row plus a server package.** Auto-discovery returns every declared
plugin whose server module exists, in spec order, then appends any undeclared package that has one —
it runs through the same generic lifecycle with a default never-fails spec. Identity matching is by
**module path**, not leaf name: matching on the leaf would miss the hyphen/underscore pair and spawn
the same server twice.

### 5.2 The lifecycle

The service builds one **plugin registry** from the spec table, eagerly creating a lifecycle per
declared plugin before anything starts. A lifecycle owns the plugin's client, process, port,
supervised background tasks, watchdog state, readiness and the exact set of registered tool names.
Start, stop, watchdog, connect and health all iterate the registry — there is no name-conditioned
branch anywhere in the lifecycle engine.

**Readiness is protocol-defined, not probed.** A plugin is ready exactly when the harness's
connect-time **era negotiation** completes: a modern plugin answers a discovery request and is
adopted at the current revision; a legacy one gets the older handshake. A plugin server answers only
after its own server lifespan has finished, so the completed negotiation *is* the ready signal. There
is no readiness tool.

Each plugin's own serving requirement is encoded in its lifespan — memdb and memfiles require a
usable store, and a failure there means the port signal never fires, so the harness reports FAILED.
Dependencies that are **not** required to serve are deliberately outside the lifespan and never gate
readiness: the gateway's external servers, sharefile's tunnel, WeChat's login, media providers, the
mesh broker, embedding backends. They surface through their own status tools instead.

A **required** plugin (as named in the config) failing to become ready **aborts startup** rather than
limping on. The spawn hang-guard is bounded, and the service opens for input only once every plugin
spawn has converged, so input can never race ahead of plugin startup.

**Start is one path for every plugin**: idempotent if already running → the spec's enable hook (first
start only; a watchdog restart skips it — a hook reporting "no-op" is *expected*, and it also purges
the plugin's catalog rows) → the uniform start, which brings the child up and makes its tools real in
the shared catalog → re-point the shared tool context at the live client → the after-ready hook → arm
the watchdog. The order is what lets each step assume the last: the enable hook is a first-start
concern, and the after-ready hook is only ever reached with a live client. Spawn or hook failure →
FAILED.

**The watchdog** supervises every started plugin identically. On an unexpected child exit it
unregisters the plugin's exact registered tools (plus any registry tool bound to the dead client),
disconnects the dead client — deliberately tearing it down rather than dropping it, so the SDK's
background tasks cannot hammer a dead port — and restarts through the full uniform start with
exponential backoff, up to a bounded number of consecutive failures. The restart counter resets
**only when the crashed child had stayed up past a liveness bound**: a fast boot-loop is deliberately
not reset, so a crashing plugin accumulates toward the cap instead of restarting forever. The
plugin's supervised tasks are reaped before each respawn so a restart never stacks a second poll
loop. A restart tells every live worker sharing the plugin its new port. Subagents have no watchdog
of their own.

**Stop** is uniform: set the stopping flag *before* touching the process (otherwise the watchdog's
wait races and triggers a spurious restart), cancel the watchdog, cancel the supervised tasks,
disconnect the client, stop the child.

**A plugin may spawn children of its own** — a tunnel's connector, every external MCP server the
gateway runs — and those are reachable only through it. So both stop ladders kill the whole tree
rather than the child. On POSIX the descendants are read from the process table **before** anything
is signalled, because a dead parent's children reparent to init and become unfindable. The stop path,
however, only runs while slife is alive to run it — a hard-killed parent unwinds no Python at all —
so on Windows each spawned child is additionally assigned to a **kill-on-close job object** at spawn,
before it can spawn anything of its own, and the kernel then terminates whatever is still inside when
slife dies for any reason. The assignment lives in the uniform spawn, so no plugin carries cleanup
code for it.

### 5.3 The child contract

A plugin's server module must: bind a free port; signal the parent **once ready** — the shared
server-runner wraps the lifespan and emits the port on stdout only *after* the app is ready to serve
MCP, and a plugin must never signal early; start the MCP framework on Streamable HTTP with the
pre-bound socket; expose tools, where bare names are public and `__`-prefixed ones are internal; and
be importable as a module. There is no base class and no SDK — the contract is that shape plus the
spec row. Heavy post-readiness work goes through an after-ready hook rather than the lifespan. A
public tool becomes a catalog row the moment the child is ready, so it is findable by search (born
**unloaded**: searchable, not injected, until loaded).

Two mechanics worth knowing. **stdout is the port channel only** and is closed immediately after the
signal, which is why interactive-auth instructions go to stderr — and one specific marked line is
what the parent turns into a desktop notification. And **plugin servers run in SSE mode**: a listen
stream *is* a response stream, and a single JSON body per POST has nowhere to carry a change
notification.

**The spawn starts draining stderr before it reads the port signal.** A child whose first log write
fills the stderr pipe blocks in that write and never signals, so the port read then times out and
kills it as a failed start with the true cause — a full pipe — invisible. The drain has to be reading
before the child logs, and it is the pipe's only reader while it runs (§9.4).

The parent hands the child its identity and its serving ports through the **process environment** —
there is no other in-band channel before the first request: the session id, the agent name, the
directory overrides, the plugin name, and one port variable per plugin. Workers read the port
variables to share the parent's plugins, so the env var is also the sharing mechanism.

**Everything the child logs is a diagnostic pipe, not a terminal.** The child runs stderr at DEBUG
with its own session file; the parent relays stderr at DEBUG, masking secrets and filtering banner
art and the child's own already-formatted lines. An uncaught exception writes the full traceback to
the file and prints exactly one line to stderr — that line is what the host relays as the
load-failure reason.

### 5.4 The built-in plugins

| Plugin | Role |
|---|---|
| **mcp-gateway** | Gateway for external MCP servers (stdio / SSE / Streamable HTTP). Owns the transports, the per-server tool snapshot and interactive auth; the *catalog* is the host's. |
| **memdb** | Turns database, hybrid search, turn persistence, session restore, embedding configuration. |
| **memfiles** | Private notes / diary / files / reports cabinet (§7.5). Also owns the scheduled-task *data* tables; the schedule *tools* are builtin. |
| **wechat** | Bidirectional WeChat messaging. A long-poll loop feeds incoming messages into the inbox as `wechat`-channel turns; the model replies itself through a tool — no harness auto-dispatch. |
| **sharefile** | Public file sharing. Serves file bytes over a plain-HTTP route on the **same port** as its MCP endpoint, stat-pinned so a share never silently serves replaced content. Owns the pluggable tunnel. |
| **a2a** | The mesh (§8). Starts only when the broker is reachable. |
| **media** | Non-chat generation — image, video, TTS, ASR — behind a provider-agnostic adapter layer. Artifacts are work products in the working directory, never cabinet files. |
| **job-coding** | Deterministic jobs as MCP tools (§5.6). |
| **local-embed** | OpenAI-compatible embeddings endpoint, from its own package because it is also runnable standalone. Its config pins the port a static embeddings base URL points at, so it pre-binds its socket. |

**The sharefile tunnel is pluggable** — a named active provider per config. Every provider presents
one surface and shares one lifecycle: a single-flight start guard, retries with backoff, an
active/starting/failed/idle state machine, and a background health monitor. Three providers are
supported, two of which need no account. A **missing dependency is terminal** and never retried; a
**transport failure is retried**, and free-tier sessions recycle, so the monitor keeps restarting the
tunnel in the background. The plugin always loads — the tunnel never gates readiness.

Two rules shape the tunnel's liveness, and both generalise:

- **A published URL is not a working one.** A provider declares whether a printed URL is itself proof
  of readiness; for one that prints its banner *before* the edge connection, the hostname exists for
  that window and answers with an edge error. Readiness is deliberately the *edge* signal, not a
  local fetch of the published URL: on a fresh tunnel a public resolver answers as soon as the
  hostname is printable while the local resolver is still negative-caching it, so a self-probe would
  measure this machine's lag and refuse URLs that work.
- **Liveness cannot be read off the child's stdout.** A connector that outlives its edge connection
  keeps running while every published link answers the edge error, and a lost connection is logged as
  a retryable error with **no** unregister line following it — so a connector set scraped from stdout
  looks complete straight through the outage. Liveness is therefore asked of the transport. An
  unanswered probe is "no answer", never "down": a probe that cannot answer must not be the thing
  that declares an outage. And the monitor does not respawn a tunnel the moment it goes unreachable —
  a child that lost the edge re-registers on its own and **keeps its hostname**, while a respawn
  mints a new one and strands every link already handed out — so an unreachable transport gets a
  grace window to heal first.

`is_reachable` is deliberately **narrower than is-active**: "a URL exists" and "that URL would be
served" are different facts, and sharing refuses on the second rather than hand out a link that
errors. The health report says the same thing for the same reason.

### 5.5 The MCP gateway

Three wire transports, one connection class built on the **official MCP SDK's client session** — the
same mechanism the host uses to reach Slife's own plugin children. The class supplies the lifecycle
the SDK does not: device-flow authorization, transport establishment and re-establishment, stdio
stderr relay, per-server connect locking, and the needs-user-auth pause. For URL-configured servers
the gateway tries SSE first and falls back to Streamable HTTP.

**Protocol era decides how a change reaches us.** The modern revision removed the connection-scoped
channel: a tool-list-change notification arrives **only** on a subscription stream the client asked
for. So servers we own publish on a subscription bus and the consuming side keeps one stream open per
link, re-listening after a drop; a modern external server gets a per-server listen stream inside the
gateway; a legacy peer keeps the session channel. All three funnel into the same notification
handler, so the harness-side trigger is unchanged. The subscription-bus helper also closes a
framework gap — the server framework never registers the listen method, so a modern client's stream
would otherwise get "Method not found".

**Notifications are coalesced and sent from a detached task**, never inside a request handler's
cancel scope — an interleaved burst desyncs the SDK's cancel-scope stack and every later call dies.
That is implemented once and shared, not per-plugin.

**Health is a tool list, not a connection.** The modern protocol removed `ping` outright, so a
compliant peer answers "method not found" — which a probe can only read as death or as life. What
actually answers the question is the tool list, which is the same call the reconcile already makes,
so the connection keeps a per-server **tool snapshot** (the tools, its age, the peer's TTL, the last
error) instead of a connection state machine. The snapshot is re-read on the peer's own signals — a
change event, a dead transport, a failed call, which repairs on the failing request — never on a
timer. The one background job is acquiring a list for a server that has none; it retries with backoff
and **stops the moment a list succeeds**, so a healthy server is never polled.

**The catalog is shared, not gateway-local.** The gateway owns transports and the live tool surface;
every external tool's row lives in the host's catalog, fed by the host's reconcile whenever a
server's tool surface may have changed. Auto-load servers get their proxies and rows wholesale;
on-demand servers (the default) get **row-only** mirrors so search and load can reach individual
tools one at a time. A server whose last tool list failed has its rows marked error — the runtime
lane of the status column — so they leave the injected set while the load state the model chose stays
on the row and comes back with the server.

**The reconcile** is driven by connect events, config mutations, change notifications and the
gateway-ready glue, and runs in the **background** so a plugin start never waits on external servers.
It projects the connectivity verdict onto the row status column, mirrors each enabled server's rows
**concurrently** (each awaits a real tool list) and in one batched pass, skips disabled servers
entirely (asking for a tool list *is* connecting), unregisters proxies whose server left the config,
and purges the rows of removed servers by comparing against the tool config — the authority — rather
than the live pool, so a gateway restart cannot wipe a still-configured server's rows.

**The boot window.** Because the reconcile runs in the background, there is a window after startup
where the registry holds the builtins but not yet the external tools; a call then fails with
"unknown tool" before the row exists and the "is not loaded" refusal after it. Nothing is broken —
the pass has not finished. One line marks the moment the set is usable, emitted **once per process**
on the first pass that has converged (and always on failure): silence therefore means *still
syncing*. Its total counts every enabled catalog row, so skills and CLIs — which the registry holds
no *instance* of — are included, and its delta counts what the startup **wrote to the catalog**,
never a registry before-and-after, which would announce the whole external tool set as new on every
restart. Convergence is "no enabled server is still starting": a bare "no list yet" means three
different things, which the gateway's spawn-settled flag and a reachability verdict separate — a
failed spawn, a slow one still answering, or one whose spawn is simply still in flight and must not
be judged. The spawn-settled flag is published on **every** exit of the gateway's boot pass, a
config-load failure included, so a pass that has finished is never left reading "pending" until a
budget runs out.

**Crash survival.** When the gateway child dies, every external row is marked error so none of them
keeps injecting a dead transport; the restarted gateway connects every enabled server again and the
reconcile clears each mark as its server comes up. A crash costs one reconnect, not the session's
toolset.

**Interactive authorization** uses the device-code flow with tokens in the credential store, and the
token is held **beside** the configured headers, never written into them: the live config is what a
re-add compares for idempotency, so injecting the token there made an authorized server compare
unequal forever — every re-add reported a change, tore the transport down and re-ran the
authorization pre-check instead of answering "already connected". A metadata-only edit is now
**persisted** rather than answered and dropped: the transport comparison deliberately excludes those
fields, so without the write the config kept the old text while the caller was told nothing had
changed. The needs-user-auth state is real: while it is set, a background retry **refuses to re-run
the device flow** — an authorization prompt must never be raised by a retry — and the list refresh
and call paths raise immediately.

**External connections are proxy-free on loopback.** The SDK's HTTP client builds a default transport
that reads the OS proxy configuration and applies it to every request, loopback included, so on any
machine with a proxy configured the gateway's connect and tool list got routed through it, the
connect retry burned up, and plugins never appeared ready. The host client therefore supplies its own
transport with environment trust disabled. External servers are nuanced: the SSE path keeps the
proxy-reading default, and the Streamable fallback goes proxy-free only when the server is
URL-routed. A remote server that genuinely needs the proxy should be configured deliberately.

### 5.6 Jobs

A **Job** is a plain public function in a source file under the data directory. Its docstring and
typed signature become the tool's description and parameter schema, following ordinary MCP tool
norms; its name is the function's with a namespace prefix, which it shares with the plugin's own
management tools — and which a job may never take.

**The files are the source of truth — there is no job-config file.** A restart (or the watchdog)
re-scans the directory and re-registers the tools. Creating or editing a job is *coding*: a skill in
the skills directory is the authoring guide.

**Execution is deterministic.** The tool calls the job function with exactly its declared arguments,
and the only LLM access is an explicit one-shot call on a configured job model — a top-level
`"provider/model"` reference that reuses the model provider list and is independent of the active
model. It should name a *different*, usually smaller model: a nested one-shot job call then neither
churns the agent loop's prompt-cache prefix nor competes for its quota. No system prompt, no
conversation history and no agent loop ever reaches a job's model — a structural guarantee.

Jobs that call the LLM are async; pure-computation jobs stay synchronous, and the runner runs those
on a **daemon thread** (never the default executor, whose workers are joined at exit and would wedge
shutdown on a hung job), capturing the context so a sync job's LLM client stays visible.

A job that needs an **external capability** reaches it through a handle that is a lazy proxy to the
gateway, forwarding to the gateway's internal call tool — the same shape the host's proxies use.
Nothing external is ever spawned a second time, so a job can use **any tool on any connected server,
loaded or not**. The handle holds **no client between calls**: a connection is built, used and torn
down per call, so nothing can go stale and the plugin needs no reconnect bookkeeping. Port discovery
is layered — the host pushes the gateway's port at **both edges of the handshake**, so spawn order
never decides it, with the spawn-time env var as the fallback.

After any tool-set mutation the plugin pushes a change notification and the host's generic rescan
re-lists and diff-registers, so per-job tools appear and disappear live. The plugin is watched by the
uniform watchdog and covered by the health tool.

---

## 6. Subagents

A subagent is an **agent worker**: a local child process running the same agent loop with the same
config and tools as the main agent, deliberately stripped of everything that makes the main agent a
*harness* — no TUI, no turn persistence, no scheduler, no cut-in injection, no mesh inbound drain, no
plugin spawn, no health or watchdog duties. It keeps no independent network identity: when it reaches
the mesh it sends **as the main agent**.

```
 Main agent (the harness)                     Worker child
 ┌──────────────────────────────┐             ┌──────────────────────────────────┐
 │ Inbox ─ one turn per message │             │ a headless service, worker role  │
 │   ▲  subagent auto-push      │             │  inbox ─ worker/send = ONE turn  │
 │   │                          │  JSON-RPC   │  one-shot history per task       │
 │ done-hook ◄──────────────────┼─────────────┤  no TUI · no persistence         │
 │ SubagentProcess (pipes)      │  stdin/out  │  shared plugin clients           │
 └──────────────────────────────┘             └──────────────────────────────────┘
```

Two agents, **one loop machine**. Both run the identical loop, including the per-turn harness pair
and the internal trim, driven by the identical inbox. What differs is the harness around it — the
declared capability table of §2.7, not scattered branches. "Does not run the main agent's harness" is
a statement about the *service layer*, not the loop.

**A subagent is not a plugin, and not a mesh peer.** A plugin is spawned and owned by the parent,
speaks MCP over Streamable HTTP on a signalled port, and is watched. A subagent speaks JSON-RPC over
stdin/stdout, owns nothing, and has no watchdog — it dies alone.

### 6.1 The process protocol

The wire is JSON-RPC 2.0, deliberately not the mesh protocol: the worker is *local*, not a peer.

| Direction | Purpose |
|---|---|
| child → parent | startup readiness — the spawn await wakes on it, **or on child exit** |
| parent → child | one task (one turn), correlated by request id, never by text |
| parent → child | cancel — drop if queued, preempt if running |
| parent → child | a shared plugin moved to a new port — reconnect |
| parent → child | the cloned parent history, sent on stdin at spawn |
| parent → child | graceful shutdown |
| child → parent | the task's one-turn result |
| child → parent | "the result above is final" |
| child → parent | a progress notification — parsed but never emitted, reserved |

Four implementation details carry real weight:

- **Pure UTF-8 on stdout.** The Windows default stdout codec is the system code page and cannot
  encode emoji, so protocol writes go to the raw buffer, bypassing the codec.
- **Piped stdin is read on a dedicated thread.** The async pipe registration fails for a parent-owned
  pipe on the Windows Proactor loop, so a thread does blocking reads and feeds the event loop. The
  reader stays live while a task runs, so a cancel can preempt.
- **Config never rides the process env.** The resolved config carries plaintext API keys, so it is
  passed via a restricted-permission temp file — never the environment, which is readable through the
  process table.
- **Over-long protocol lines are discarded, never fatal.** One line may legitimately be the whole
  cloned history or a many-megabyte result; a line past even the raised cap is dropped, tail and all,
  so a pathological line cannot kill the reader or wedge the worker.

### 6.2 One turn per task

A spawn call starts a named worker. **A worker's name is its identity** — explicit, never
auto-generated, and validated, because the name lands in the child's system prompt *and* its log
filename. Reuse is explicit: spawning a running name returns the live worker, **keeping the context
that worker was started with** — a spawn request is not applied to an existing process, so what a
caller reports back is the worker's live context source, never the one it asked for.

Context is chosen once, at spawn: **clean** (the default) runs each task in a bare history;
**cloned** copies the parent's message history without the parent's system message (the worker
renders its own) and ships it on stdin. A clone is a **spawn-time snapshot** — a cloned worker
re-seeds from that fixed snapshot on *every* task, so it never accumulates context across tasks and
never sees parent turns that happen after spawn. The snapshot is taken *inside* the tool call that
spawns the worker, so it ends on an assistant tool call whose results do not exist yet; the worker's
history is repaired on arrival (§2.1's turn-consistency invariant) rather than sent as-is, which
every provider would reject.

A task is one turn by construction: one send becomes one inbox message, which becomes exactly one
loop run. Inside that run the loop may make many LLM and tool calls, bounded by the iteration limit
**and by the window ceiling** — a worker has no save point (§2.2's other trim site), so the loop
enforces it at the request boundary. **The worker processes tasks serially**; extra sends to a busy
worker are queued by the *parent*, never refused and never re-sent.

### 6.3 Identity and result delivery

The worker renders its own system prompt from the worker identity template, which frames it as a
headless process of the parent with the same capabilities, carrying **no identity of its own** — no
presence, no personality, and in all external communication it acts as the main agent, never
introducing itself. It is told it is ephemeral, and how it was seeded. Its completion posts back
under a dedicated inbox source, so it is distinguishable from human turns in memory search yet
routed into the human history.

**The worker has no result-push tool.** Its reply goes out as an ordinary JSON-RPC result on stdout.
The **parent harness** does all the pushing: a synchronous caller's future resolves; an async result
is stored and the manager is notified; the manager posts an inbox message carrying the subagent
marker of §2.5 so the model can attribute it, and the channel records whether it was a scheduled
task. The TUI drops the marker and shows the `Subagent(<name>)>` bubble. There is deliberately no
subscribe call — async results are auto-subscribed, and a poll mode suppresses only the *push*, never
the retrievability. Retrieval is **non-consuming and states what it is**: a task answers pending,
completed, failed or cancelled from its own record (only an id that was never sent reads unknown), so
a completed task cannot report "pending" the second time it is polled. A cancelled task's reply is
marked as partial **and names the reason** (the short terminal-state tokens of §2.1), because for a
*timed-out* task that text is what the late-result store hands back, and a task that hit its own
ceiling would otherwise read exactly like one its caller withdrew.

### 6.4 Failure semantics

The worker has no error-handling loop of its own; every failure ends in a result coming back.

| Failure | Surface |
|---|---|
| LLM/provider error inside the turn | the reply text is the failure prefix — the caller's future resolves *successfully* with that text |
| protocol error frame | the parent raises → the tool returns an error string |
| worker died at boot | the spawn await wakes on **child exit**, not only on the ready line, so the spawn fails at once instead of burning the whole spawn budget while the manager's registry lock is held — the waiter re-checks liveness, so a death still fails, promptly |
| worker died before replying | the pending future fails with "closed before task was resolved"; every async task it still owed is announced as a failure rather than left pending |
| **stall** — no reply at all (a caller is waiting) | the task budget expires → a timeout carrying the task id → the tool reports it and names what to poll |
| **stall** — no reply at all (nobody is waiting) | the async task's lifetime backstop fires → the task is marked failed, preempted, and its failure **pushed** to the caller |

The stall case is the interesting one. The abandoned task is **preempted in the child** — a worker is
serial, so a genuinely stuck task must never block later tasks. A **late** result is stored (and
reconciles the record it timed out on) but never auto-pushed, because the caller was already told it
timed out; a push after a reported timeout would double-announce a task the caller believes failed.
Parent-side cancel does the same, discards the late reply, and preempts the child too — a cancelled
turn is the same situation as a timeout.

An async task is the one case where the parent *is* the one to tell: nobody awaits it, so no caller
owns its bound, and silence would look exactly like work in progress. Its lifetime is therefore a
wedge backstop rather than a budget — deliberately far above the task budget (a validation enforces
the ordering), because honest work is never meant to reach it.

This is what "no error handling" means precisely: no retries, no recovery, no second attempt. The
worker is the one agent that does **not** participate in the stream-retry ladder (§3.3) — even a
transient transport failure is a single attempt, surfaced immediately, because there is no user to
wait on. The per-chunk stall watchdog still applies, but in a worker a stall is surfaced, never
retried. The concurrent-worker cap is configured; a waited task's bound is the registry's task
budget, and a per-call timeout on the send tool is the one model-facing override.

### 6.5 Sharing and recursion

Every plugin the parent started is shared **by port** — a manifest loop over the discovered plugins,
never a hard-coded subset. A plugin the parent skipped published no port and is skipped here too, so
the two processes agree on which plugins exist without either enumerating them. A worker never spawns
its own plugin processes, so a worker crash takes down only the worker.

**There is no isolation.** Shared servers are exactly the parent's servers — same turns DB, same
cabinet, same gateway. The worker is trusted, never fenced. The one deliberate asymmetry: the mesh
plugin registers the send-side tools in the worker so it can act as the parent, but the **inbound**
queue stays with the parent — a worker that could push into its own parent's inbox would confuse its
own history.

**Recursion is allowed**: a subagent can spawn its own descendants. There is intentionally no
subagent-specific gate — trust, not enforcement.

---

## 7. Memory

Every turn is permanently recorded as an independent row — there is **no session concept**, just a
continuous time-ordered log in a per-agent database file. Agent isolation is at the database-file
level: naming an agent gives it its own diary, indexes, file cabinet and mesh name.

**Memory is core — the agent never runs silently without it.** A fatal turn-save failure is a hard
stop, not a skip: the memory-broken state is set, the **inbox freezes** (queued turns are dropped — a
turn that cannot be persisted is not worth running), and the TUI shows a persistent banner until the
database is fixed and the agent restarted. Transient transport timeouts are warned, not fatal.
Restore-side failure is likewise fatal: a present-but-broken database **aborts startup**.

### 7.1 The turns database

One table is the diary, and the schema file is authoritative for its columns. The row's user-message
column holds the **masked** text, the same form the live history holds — so restore rebuilds the user
turn from that column verbatim, and a pasted key cannot return to the model's context through a
restart. Beside it sit the assistant side as a message array, the summarize pass's summary and tags,
the two timestamps, the channel and agent identities, the billed token count, and the context size at
the last API call, which restore primes the first turn prompt with.

There is **no image column**: image blocks live only in the in-memory user message and are never
persisted, so restore is text-only — and so is a turn rebuilt by the per-turn decision, which comes
back as its text plus the image-attach call and result (§7.6). The keyword index is
external-content, and its UPDATE trigger exists because the summarize tool rewrites columns that such
an index does not otherwise track — without it the summary stays invisible to keyword search.

**There is no migration layer.** Backward compatibility is not supported: schema changes land in the
schema file for fresh databases, and an old database is deleted and rebuilt rather than upgraded. The
data is derived, and a migration path is a permanent maintenance cost.

### 7.2 Search

Three indexes back the search modes: a full-text index, a vector KNN index, and a B-tree on the
creation timestamp. The modes are **grep** (a real regex over the user message and the message
column), ranked keyword search with snippets, hybrid (both legs fused by reciprocal rank fusion) and
time (browse by date).

**grep is the mode no index serves**, so it is the one that behaves differently. SQLite has no regexp
engine, so the text predicate runs in Python over a **scan** — newest first, capped at 20 000 rows
examined — and its results are unranked: newest-first, each carrying a window of text around the
match instead of a score. That is what the scan buys: a partial spelling (`translat(e|or)`), a path, a
symbol, a word glued inside a longer CJK run — text neither the tokenizer nor the vector describes.

Its column set is as deliberate as the rest: the user message and the message column, the conversation
itself. The **generated summary is not in it** — grep reads what was said, not what the summarize pass
later made of it, and that is not what it is for. A term surviving only in a summary is therefore the
keyword leg's to find, which searches `summary` and `tags` along with the two text columns.

It is also the mode the model reaches for increasingly often, and it has **no counterpart in the
per-turn selection** (§2.3), whose only search is hybrid.

The cosine metric is **declared in the vector table's DDL**, because that is what makes the raw
distance readable as a 0–1 similarity: one-minus-distance is a cosine only when the metric is one,
and the backends do not all normalize — a local GGUF backend's raw output is not unit-norm, so an L2
table could not yield a cosine at all.

All time bounds share one grammar: an ISO datetime or date, the day words, the calendar periods, or
"n days ago". A period word anchors to the period's **edge**, not to today's day-of-month. A bound in
no known grammar **raises** rather than passing through — SQLite would compare the text, match
nothing, and report a bound nobody understood as "no results".

**What a turn's vector is a vector *of* is the conversation, not the turn.** The embedded text is the
user message, the tool calls the turn made (names and arguments, bounded) and the assistant's prose —
**not tool results**. Measured on a live session, results were 56–99% of a turn's text, so an index
built on them describes "an agent ran tools" rather than what the turn was about: every turn lands in
one narrow cosine band and a similarity floor has nothing to separate. Nothing is lost to search —
results live in the message column, which is what the keyword leg and the turn reader read. Because
that text contract is half of what makes two vectors comparable, it is **versioned** (recorded in the
database's metadata) and a version change drops the stale vectors for the drainer to rebuild, exactly
as a model or dimension change does.

Two hardening rules are load-bearing: **CJK queries route keyword search to a substring fallback**;
and full-text MATCH operator words are quoted and stray symbols stripped, so a user query cannot
crash the MATCH parser. Without an embedding backend, hybrid degrades to keyword-only and reports its
degraded mode and reason.

The routing's real reason is narrower than "the tokenizer cannot do Chinese, substring matching
can". The standard tokenizer makes a **contiguous CJK run one token**, so a Chinese query is an
*exact-token* lookup: measured on a live diary, a two-character word matched only the turns where it
sits next to punctuation or a digit, while the substring route matched three times as many — the
others hold the word glued inside a longer run, which an exact-token lookup cannot see. Substring
matching is a true per-word matcher, which is what Chinese prose needs, so it is the leg CJK routes
to — but it inherits the AND below, which is what limits it in practice.

Both keyword legs **AND** their terms, and the counting route and the searching route share that one
rule so they cannot disagree. The consequence is worth knowing before trusting the leg: it fires only
on a turn containing *every* word, and for CJK that means every word literally, as a substring. A
query of one or two words the turn would actually contain is what it rewards; a synonym-stuffed one
matches nothing at all, which is what the discriminator's queries tend to be — so on Chinese turns
the semantic leg is often the only one contributing, and the floor above is the only thing deciding
the selection. Steering the query tighter was measured as a wash and not adopted: the keyword leg
starts firing, but relevant hits slip on the semantic leg, which the floor cannot afford. The query's
shape belongs to the discriminator; the schema states the parameters, not a search strategy.

### 7.3 Embeddings and the semantic gate

Embeddings are a first-class top-level config section, shared by the turns database, the cabinet and
the host's tool catalog. A **provider** is one OpenAI-compatible endpoint with a model, and one
provider is active at a time. One rule surprises people: **the vector dimension is deliberately not
configured.** It is resolved from a known-model table, the endpoint's model listing, or — whenever
the width is still unknown — a probe embed. A load that cannot read the endpoint reports **not
loaded**: the gate stays shut and hybrid degrades to keyword, instead of opening on an embedder that
fails every batch.

**One manager is the lifecycle actor** — one object owning the binary gate, the embedder instance and
an event-driven index drainer. It is document-generic, so each of the three stores drives its own
instance and the three gates are independent. There is **one implementation and one subclass**: the
turns database's is the base class, and the host's catalog subclasses it, overriding only the four
hooks where the catalog genuinely differs.

The gate opens exactly when the embedder is ready **and** nothing is left unembedded — there are no
intermediate states, and **partial semantic results are never served**; while the gate is off, hybrid
degrades to keyword-only with a hint naming the reason. A persistently failing embedder is bounded by
a per-session no-progress limit, after which the drainer parks as stalled rather than exiting:
enabled stays true, so the next save wakes it for a fresh bounded round. Idle therefore costs nothing
and a transient failure self-heals. The state machine and a human-readable reason are reported
separately from the binary gate.

The **write path is insert-only**: saving a turn never embeds on the save path (a slow embed of a
large turn once tripped the save timeout and raised a false alarm about a row that had in fact been
saved). Each insert wakes the idle drainer with a non-blocking event set. **A model or dimension
change** stops the drainer, migrates the vector table in place by comparing the declared width and
the model identity against what the database was built with, and restarts it; a mismatch drops and
recreates the table, because old vectors live in a different vector space.

The catalog's instance publishes its state into the catalog database's metadata table rather than
keeping it to itself. That is not redundancy: the index is *shared*, so a process running no drainer
— a worker — must be able to report it rather than guess. A reader that finds no published row
reports "unknown", which is a fact, where "disabled" would be a claim about a drainer that is not
there.

### 7.4 Session restore, and the live-context list

On startup, recent turns are read **directly from SQLite** — no MCP transport, no plugin dependency.
The UI rebuilds the last session from the diary, and only then does the plugin spawn begin. Restored
messages carry their stored timestamps, so the rebuilt chat matches what was seen live.

**The id list replays the exit-time context.** One metadata entry is an **ordered array of row ids**
naming the live context. Three things maintain it: the save appends the new row id inside the diary
row's own transaction; the internal trim drops the turns it evicts, passing the **actual** ids; and
the per-turn rebuild replaces the list with what the turn kept plus what it recalled, with an
explicit clear for a context that is to be empty.

- **The list's order is authoritative.** Reads replay it as written and never re-sort by row id — and
  the slice need not be contiguous.
- **Turns on the list are returned verbatim, with no ceiling re-slicing.** The list already encodes
  the trimmed state and is its own bound.
- **A set refuses an empty list by design** — its guard protects a *partial* selection, not a
  deliberate one; clearing is a separate, explicit operation.
- A database predating the list restores an empty context, and its turns stay searchable.

The restored turn prompt is primed with the latest restored turn's persisted context size — the exact
context size at exit — so the first prompt and status bar show real occupancy. The just-restored
history is also exempt from the first-turn trim.

### 7.5 The file cabinet

A standard plugin, self-contained and replaceable. **Four** typed knowledge stores, each
**dual-written** to a human-browsable markdown file and a SQLite index: **note** (keyed by subject),
**diary** (keyed by date), **file** (saved attachments under a per-category directory, auto-filed by
extension with bytes staying on the filesystem, and semantically searchable from an LLM summary given
at save time — one pass, no separate summarize tool) and **report** (scheduled-task reports).

Each kind owns its own full-text and vector tables and declares its own time axis, so a range query
on a kind uses the same column its list tool orders by. The index mirrors the turns database's design
and **reuses its code**: the shared manager drives the drainer over all kinds, and each plugin
reports its own gate.

URL saving guards against SSRF **before fetching and again on every redirect hop**: every resolved
address must be globally routable, so loopback, private, link-local and cloud-metadata targets are
refused. One deliberate exception — the documented **fake-IP pools** used by common proxy tools are
accepted, because those resolvers answer real public hostnames with addresses from them. (The same
pools are what sharefile's tunnel health *flags*, for the opposite reason: intercepted traffic breaks
the tunnel's control connection.)

All save tools return the saved local path and never auto-publish, so nothing is registered in any
token registry as a side effect of saving. Publishing is always the model's explicit choice, through
the separate sharefile plugin.

### 7.6 Images and the `@` syntax

Users attach images with `@` directives — **one `@` is one source**, any number per input, parsed
independently. The user message stays verbatim; the extracted sources are handed to the loop, which
**auto-invokes** the image tool once with the whole list through the harness-call machinery — a
single history shape, no LLM iteration spent deciding to attach.

A source is a bare path, a URL, a data URI, or any of those quoted or bracketed (so spaces work).
Multiple directives may sit adjacent without spaces. A bare path must end in an image extension, so a
bare `@name` is skipped as plain text; **URLs and data URIs are self-identifying by scheme** and need
no extension gate. A token ends at whitespace, a quote, the next `@`, a CJK character (a natural word
boundary when typing) and — for URLs only — a comma; data URIs keep their commas because base64 is
comma-heavy. There is no filesystem check at parse time: existence is validated downstream, where a
failure becomes a reported error rather than a silent drop.

Parsing is deliberately **two-phase** — locate every `@`, then match a source pattern on the slice
after each — rather than one regex, which is more robust against the special characters above and
keeps the existence check in one place downstream. Blocks are **live-session only**: never persisted,
so restore is text-only, and a rebuilt turn keeps the image-attach call and its result — which name
every source — but not the pixels. That is deliberate: the model re-attaches from its own history
when the turn needs the picture again, so nothing keeps a second copy of it. Images are never
rendered in the terminal — the model reads them, and the user opens the file with the OS or a share
link.

---

## 8. The A2A mesh

The mesh runs over the official **A2A-over-MQTT** profile — the published SDK — *not* a self-built
binding. The wire protocol, the topics, the QoS rules and the retry ladder are the SDK's; Slife keeps
only harness glue: the plugin, the inbox drain, the channel, task bookkeeping, presence display and
the LLM tool surface. Interop with the rest of the ecosystem follows, and standard A2A is
async-native — a send returns a task id immediately and the result is pushed back later, which is
machinery the harness already had.

**Layout.** A versioned topic tree with four categories — discovery (a retained agent card, with
presence carried by an MQTT user property and a last-will), request, reply, and event
(fire-and-forget, QoS 0). Envelopes are JSON-RPC 2.0; the MQTT response-topic and correlation-data
properties carry the requester's reply routing, and a request lacking them is rejected. A task flows
acknowledgement → optional working updates → artifact → terminal, with per-task dedup.

**Two connections, distinct client ids.** The SDK responder owns all presence publishing — the
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
model as an inbound message (carrying the peer and the task id) and the model — the only party that
knows whether it is still working on that task — decides what to do with it.

The harness keeps only the unambiguous half: a task whose message has not started yet is dropped from
the inbox outright, since nothing was done and there is nothing to judge. A withdrawal is surfaced
only on a **live** link: the SDK cancels every inflight handler when its own session ends, so a
dropped connection must not be reported as a peer cancel — the requester re-sends the task instead,
and the retry arrives as a fresh request.

A turn that fails is a **harness** failure, not a mesh one: the channel delivered the message and was
done, so the TUI draws no mesh failure line for it.

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

**Presence** is online/offline only — the profile has no heartbeat, and the older timeout sweep is
gone. Transitions reach the model: the plugin queues them and the per-turn prompt carries only
*changes*, read once, while the current roster stays queryable — so a missed event never leaves the
model with stale state. A **cold retained offline card** (a peer already gone before we subscribed)
is cached for the roster but not announced, so a dead card from a past session never fires a fake
offline event.

**The tool surface is the standard operations**, one prefix, nothing waits: send message, cancel
task, list agents, broadcast. There are no async or poll variants and no timeout parameter, because
nothing waits. **One envelope for every inbound message**, with the declared type distinguishing a
task to answer, an auto-delivered result, a withdrawal, a conversation, or an event; the TUI strips
it and shows the peer prefix instead.

**Config and gating.** Only MQTT is implemented; another transport value disables the mesh with a
warning at config load rather than crashing startup. Slife only **probes** the broker — the broker is
started by the user — and a failed probe means the plugin is not started, which is reported through
health. The mesh connects eagerly when the plugin starts so presence is announced at launch, and a
failed eager connect is tolerated: the tools connect lazily on demand.

**Subagents are not part of the mesh.** A worker is a local process with no network identity; it can
send as the parent, but the inbound queue stays with the parent.

On Windows the plugin switches its own event loop to the selector policy, because the MQTT library
uses reader/writer callbacks the default Proactor loop does not support — and deliberately inside the
process entry point, never at import, so importing the module in a test process cannot change the
suite-wide policy.

---

## 9. Surroundings

### 9.1 The UI

A Textual app with no screens and no modals: a chat view, an input, and a status bar. Streaming
thinking and text render into a message widget rebuilt per chunk; tool calls mount collapsible
widgets with a status icon and a primary-argument preview.

**Every piece of user data is rendered with markup disabled.** Strings Slife constructs may use
markup; tool output, arguments, results, file contents and tool names never do — the wrong path
raises a markup error on ordinary characters in URLs and JSON.

Four interaction rules are load-bearing, and Appendix A carries the canonical statement of each: the
app's cancel binding must **not** be priority, the approval prompt's and model picker's **must** be, a
binding action must be **sync**, and tool widgets are cleared only at the genuine turn-end event.

A bare `.` reply is silence: the widget is discarded rather than rendered.

**Restore is quiet for two reasons.** The restored widgets go back in **one batched mount** with
autoscroll suppressed and a single scroll at the end — rebuilding widget by widget is the live
behaviour, and it made restore jitter. And **following the tail is sticky**: the transcript scrolls
to the end only while the reader is already there, so a reader who paged up mid-turn keeps that
position instead of being undone by the next streamed token, and coming back down to the tail resumes
following.

**The console belongs to the app only while the app runs.** Textual takes raw input and the alternate
screen, and the teardown is what gives them back. A session that is *killed* rather than stopped —
`taskkill`, End Task, a closed window, all of them `TerminateProcess` — runs no Python at all, so the
terminal is left in raw mode (keystrokes echo as mojibake, `Ctrl+C` is no longer a signal) and even
the session log stops mid-sentence. Nothing in-process can undo that, and **no supervisor is added to
do it instead**: a third always-on process that breaks away from the job object and attaches to
someone else's console buys less than the accident costs. What a killed session *can* still do is
leave evidence — a **per-pid session marker** written at startup and removed by the teardown, which
the next start reads to report that the previous session was killed from outside and where its log
is. A killed process reports nothing itself; the marker is read by the only process that can.

### 9.2 Config and credentials

**Two layers.** The credential store holds secrets encrypted at the OS level; the main config file
holds *references* to them, not secrets.

Resolution order is **shell env → credential store → literal default**, for both the bare and the
defaulted variable form. The subtlety is that the defaulted form still consults the store *before*
the literal default — otherwise it would resolve to the default even when the key is held in the
store. Resolution is recursive over strings, lists and dicts. Slife never prompts and never reads the
store's backup file. The backend is chosen **deterministically by platform**, not by a keyring
priority search: Windows Credential Manager, macOS keychain, WSL's PowerShell bridge into the Windows
store, and the kernel keyring on Linux. An unsupported platform raises clearly.

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
rather than using an unbounded quantifier, because an unbounded repeat backtracked quadratically and
froze the parent's event loop for minutes on a single large relayed line.

**The gate is the model boundary, not the tool boundary.** A tool receives its arguments exactly as
the model emitted them, and what it writes — a cabinet file, an index row — keeps them in plaintext;
the mask applies on the way *back*, to the argument copy that rides into the history and to the
result the tool returns. So a secret is storable and searchable while staying unreadable through the
model: the cabinet search and its index are built from what the tool received, yet listing or reading
the same text through a tool comes back masked. The disk therefore cannot be audited through a tool
result — a masked listing and masked disk look identical from inside a turn. There is **no off
switch**: no config key reaches the engine, and a turn cannot opt out.

The recognized shapes are five kinds: well-known provider prefixes; header credentials; key/value
names; connection-string passwords; and the **JSON pair**, matched **first** because the key/value
value class excludes the quote character — a JSON body otherwise slips past it and half-matches the
URL pattern, which masks only the value's prefix and leaves most of the secret in the line.
Everything else passes through unchanged — a bare password, an AWS key id standing alone, an email
address.

**Config sections.** Two files carry the configuration, the second being the unified tool config of
§4.3 — the old inline tool array is retired — and the media, job-model and sharefile sections are
read by their own plugins rather than by the main config parser. **There is no user-facing timeout
section** — every timeout is a developer-owned constant (§4.7).

**Config writes preserve the file's comments.** Every writer mutates a dict and calls one write
helper, which then **edits the existing YAML document** rather than re-serializing the dict: the
current text is loaded as a round-trip document, the delta is applied to it, and the document is
dumped. Comments, indentation, key order and quote style survive because they never round-trip
through a dict. This matters because these files are hand-edited documentation — one of them is
roughly three quarters comments — and a single config write used to erase all of it. Lists are
replaced wholesale rather than merged element-wise, because a wrong guess about element identity
would move a comment onto the wrong element. The write is then **verified**: it re-parses the edited
document and falls back to a plain render if it does not read back as the intended dict. Losing
comments is bad; writing a config that says something else is worse. The write itself is atomic (temp
file, fsync, rename, preserving the existing file's mode) and the whole read→mutate→write window is
held under a **cross-process** file lock, because the main process and a plugin child can both be
editing the same file. That lock is taken by **non-blocking polling from the event loop**, never a
blocking acquire: a blocking acquire inside an async timeout froze the loop for the whole timeout,
and that freeze is a deadlock rather than a stall — tool calls run concurrently on one loop, so while
the first edit holds the lock across its own await, the second's blocking acquire stops the loop and
the first can never resume to release it. Acquire and release stay on one thread, because the lock's
reentrancy counter is per-thread: taking it on a helper thread and releasing it on the caller would
leave the OS lock held. A synchronous twin of the helper serves synchronous callers only, and an
async caller with an await inside the block must use the polling path — the only one that keeps
blocking work off the loop (§4.7). One rule about reading: a config parse failure **raises**, because
returning an empty dict would let a mutating caller write that empty dict back over the whole config.

### 9.3 Health

One health tool is **the only** health surface registered for the model; the per-subsystem checks are
internal functions, so nothing re-calls them after the aggregate.

There are two kinds of input. **Static records** are pushed during startup — the active model, the
config's provenance and counts, and a daemon-thread probe of the external toolchain — and the host
facts come from **one recorder** shared by the TUI and a headless worker, so both views list the same
components rather than differing by counts no reader could account for. The toolchain probe runs on a
daemon thread, so a report read in that window states its own **scope** — which facts are not in yet
— rather than letting a smaller count read as a smaller system. **Dynamic checks** are the
per-subsystem check functions, enumerated **from the plugin registry** rather than hand-listed, with
three non-plugin checks appended for the catalog, the active embedding endpoint, and the watchdog.

**Health is layered on purpose.** Every plugin-backed check probes the plugin's internal check, which
reports only raw technical state — facts and measurements, like a physical-examination report — and
**never triggers a connect**. The harness interprets those facts into levels and remediation hints; a
plugin's own check has no levels of its own. The watchdog only monitors processes; it never
introspects application state.

One rule shapes every entry: **the value is the fact, the hint is what to do about it**. A healthy
entry carries no hint at all, and a remedy must name a tool that exists — never an internal check
function, which is not callable. A startup record is dropped when a live entry covers the same
component and key, so a producer names its component after the live check that re-reports it; the
live entry wins in both directions.

**The report is plain text, not JSON**, and that is a consequence of the result budgets rather than a
style choice: a line-oriented report degrades to *fewer whole lines* when truncated, while a JSON
document degrades to an unparseable fragment. It is built to fit the save-side budget in the first
place, and it puts the verdict first, then problems, then one line per healthy component. Problems
lead; identical entries collapse into one line with a key list; the static environment records come
last.

### 9.4 Logging

Structured log lines: an event name followed by space-separated key/value pairs. Event names are
snake_case — past tense for completions, present tense for state. Every line that could carry user
input, tool arguments, tool output or subprocess stderr passes the secret sanitizer first. Nothing is
written to stdout, which is reserved for the TUI and the plugin port signal.

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

One module decides where session data lives, in two modes only: **production** (everything under the
user's home directory) and **dev** (the project root). Dev detection requires **both** conditions to
hold: the current directory's project file declares this project's name, *and* the loaded package's
parent directory **is** that directory. Either check alone misfires — a checkout whose project file
is present while the loaded package lives in site-packages would scatter data into the checkout, and
a tool installed under a home-local directory that happens to sit under the current directory would
falsely look like dev. A production install always loads from a site-packages directory whose parent
is never the current directory, so it stays production wherever it is launched from. Agent-scoped
paths resolve through the agent-name environment variable rather than a parameter default, so health
tools do not report the default database for every agent.

---

## 10. Project structure

```
slife/
  agent/       # the loop, the service, the inbox, history, prompts, roles, backends, timing
  tools/       # the Tool ABC, the registry, the catalog, discovery, the builtin tools
  plugins/     # the plugin children and the spec table — the one source of truth (§5.1)
  mcp/         # host-process MCP: slife-as-plugin, the MCP→Tool adapter, era negotiation
  a2a/         # the mesh's transport-agnostic core — mesh, broker, card, task store
  subagent/    # the worker process: its spawn and pipe protocol, its identity
  ui/          # the Textual TUI
  *.py         # platform · config · paths · health · logfmt · timeouts · threads · timeutil · …

credstore/     # standalone package — cross-platform credential store
cc-switch/     # standalone package — generates the Claude Code settings file
local-embed/   # standalone package — the OpenAI-compatible embeddings service
skills/ · jobs/  # seeded to the data dir at install
tests/         # the static-source gates (timeouts, subagent parity) live here
```

---

## Appendix A. Invariants

The rules that must not be broken, and what each prevents. They are collected here because each was
learned the hard way and each is silently violated by a plausible-looking change — so each is stated
as an **assertion**, not as a fact about the current code. If the code does not satisfy one, the code
is wrong. The numbers are stable and may be cited.

**Memory and context**

1. **Usage is measured or it is zero.** The context-size accessor never returns an estimate — a guess
   presented as occupancy is worse than an honest zero. Estimates appear in exactly one place: sizing
   what a recall may add to a context that has not been rebuilt yet.
2. **Every save path is a hard stop, not a skip.** A turn that cannot be persisted is not worth
   running. The flip side is deliberate too: subordinate dependencies never gate readiness, because
   they are uncontrollable and self-healing.
3. **Nothing a model says can empty the context by accident.** A decision keeps what it names and
   *adds* what it recalls, so a recall that answers nothing adds nothing; clearing is the explicit
   clear. A *failed* decision keeps the context too — defaulting it to a recency list would change
   the context on the strength of no decision at all.
4. **A store or tokenizer failure is fatal; no ids is the answer for everything else.** No ids is
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

7. **Identity and world change only on a model switch or a user-preference write, and always from the
   role's own template; the per-turn status is a message-stream tool pair, never a second system
   message.** Both rules exist so the static prefix stays byte-identical between those events and the
   prompt-cache breakpoint lands on it — and so that a worker never renders the main agent's identity.
8. **A harness tool must be schema-declared**, because the Messages and Responses backends reject a
   tool call in history whose name is not in the declared tool list.
9. **The tool list is computed once per request, outside the retry loop**, so every attempt sends
   byte-identical tools; and a mid-turn load **appends**, leaving the request's prefix untouched.
10. **Never emit an async notification from inside a request handler's cancel scope.** Interleaving a
    burst into that scope desyncs the SDK's cancel-scope stack and every later call dies.

**The tool system**

11. **Load state governs what a turn injects, never what a call may do.** The only refused call is one
    with no execution instance behind it, and a refusal names the state and nothing more — a refusal
    that guesses at a remedy tells the caller to do what it has already done.
12. **Load state has exactly four writers**: the autoload override, the load tool, the unload tool,
    and eviction. No connectivity verdict is ever written into it.
13. **Removal is a row delete, never a status mark**, and the row's embedding chunks go with it
    explicitly rather than through a cascade that may be off.
14. **Off is not down.** Disabled and error are different facts: the config arm may write disabled
    over either, but the runtime arm may never resurrect a tool the config switched off.
15. **The injected schema is the catalog's stored schema column** — the stored definition and the wire
    definition are one and the same.
16. **A schema is enforced, not advisory**: closed by default at class definition, validated at the
    single dispatch point. Exceptions are stated, not assumed — a schema declaring its own openness
    keeps it, and a remote server's schema is never **closed**. The adapter's one edit to a remote
    schema is the opposite direction and keeps every other key: a non-object input schema becomes
    `type: object`, so a definition reference cannot dangle.
17. **The agent's timeout overrides all defaults; a tool's own timeout is a generous backstop.** A
    native tool with an internal run-timeout must expose it as a parameter, or a hidden inner timer
    silently clamps the injection. Zero or negative never means "no timeout".
18. **Background calls escape the tool budget** — with no injected timeout, a background call is
    scheduled bare, because the chain default must not govern work that exists to escape it.
19. **Timeout values are read at call time**, never import-captured, so they stay patchable — and a
    hardcoded timeout fails CI, which is what kills the fix-one-drift-another loop.
20. **The run's outcome and the harness's bookkeeping are separate facts**, so the last-used update
    sits outside the execution's error handling. A tool that has already run — a config written, a
    message sent, a file deleted — must never be reported as failed because a shared-database write
    failed, since the model's answer to that is to retry a non-idempotent action.

**Plugins and processes**

21. **The spec table is the only place a plugin is declared.** Adding a plugin is one row plus a
    server package; nothing else may hard-code a plugin's name.
22. **Readiness is the completed protocol negotiation.** There is no readiness probe, and a dependency
    not required to serve never gates readiness. Never signal the port early: the signal means "ready
    to serve MCP on this port".
23. **A capability must report "not yet" as "not yet", never as "no".** An initialization in flight
    must be awaited by every caller, not just the one that started it — a boolean that answers "no"
    while still loading is indistinguishable from a genuinely unavailable one.
24. **A hard-killed parent runs no cleanup**, so the kill-on-close job object is assigned at spawn,
    before the child can spawn anything of its own. On POSIX the process tree is read before anything
    is signalled, and a group kill is only safe when the child leads its own group. Its death is
    therefore knowable only *afterwards*: the per-pid session marker a session writes at startup and
    its teardown removes is what the next start reads to report a kill from outside, and the console
    stays in raw mode, because the process that would have restored it never ran again.

**Subagents**

25. **A worker is the same loop with a declared, zeroed capability set.** A new capability is
    worker-denied by default and must be granted on purpose; a role branch outside the table fails CI.
26. **The harness pushes results; the worker never does**, and a late result is stored, never
    auto-pushed, because the caller was already told it timed out. The exception is the task nobody
    awaits: an async task's failure *is* pushed, because silence is otherwise indistinguishable from
    work in progress.
27. **A stuck task must be preempted in the child**, because a worker processes tasks serially and one
    stuck task would block every later one. A caller's cancel does it too — the same situation as a
    timeout.
28. **Config is handed over by file, never by environment** — the resolved config carries plaintext
    keys and the process environment is readable through the process table.
29. **The config's round trip is a fixed point**, derived from the field list rather than written by
    hand — the hand-written version silently dropped nine fields, which is how a worker came to report
    embeddings disabled while its parent reported enabled.

**Process and platform**

30. **Unbounded blocking calls run on daemon threads, never the default executor.** Both shutdown
    paths join every executor worker, so a blocked worker hangs the whole interpreter.
31. **The stderr relay must never die**, and a discarded over-long line must be consumed through its
    newline.
32. **Blocking regexes must bound their repeats.** An unbounded repeat once froze the parent's event
    loop for minutes on a single relayed line.
33. **No global socket defaults**, which would silently change every third-party socket.

**The TUI**

34. **The app's cancel binding is not priority; the approval prompt's and picker's bindings are.**
    Textual's priority pass resolves the app before the focused widget, so a priority cancel on the
    app would steal the key from the approval prompt and cancel the loop instead of denying, leaving
    the prompt unresolved. The reverse is equally true: non-priority bindings on a prompt would type
    the answer into the input bar instead.
35. **The approval prompt to deny or refocus is the *pending* one** — matched by type and undecided
    state, never by its class alone, because the model picker wears that same class and a decided
    prompt stays in the transcript as its status line. Denying the first match left the real prompt
    mounted and unfocusable behind the picker, with only the turn-cancelling key able to resolve it.
    The mirror direction is the same rule: a dismissed picker must not take focus back from a prompt
    that is already mounted.
36. **A binding action must be sync.** Binding actions run inside the key-event handler, so awaiting
    there blocks the message pump and deadlocks the widget that needs the next key event.
37. **A dismissed widget must resolve its future**, or a re-entrancy flag stays stuck and the shortcut
    is dead. The status-bar scroll happens **after layout**, or it pins the view above the fold.
38. **All user data renders with markup disabled**, and **tool widgets are cleared only at the genuine
    turn-end event** — never where the turn is merely *enqueued*, which wiped an in-flight turn's
    widgets and left its rows stuck.

**Configuration**

39. **A config parse failure raises; it never returns an empty dict**, or a mutating caller writes
    that empty dict over the whole config.
40. **Config writes edit the document and are verified before use.** Losing comments is bad; writing a
    config that says something else is worse.
41. **Credstore is consulted before a `${VAR:-default}` literal**, or the default wins over a key that
    is actually held.

## License

MIT
