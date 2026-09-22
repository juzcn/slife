# Context Harnessing

The single, authoritative description of how Slife curates the context the
model sees each turn — the sources that inject information on Slife's own
initiative, the taxonomy that distinguishes them (**channel** vs **marker**
vs **harness tool-pair**), and each concrete mechanism, including the per-turn
**context rebuild** that decides *which turns are in the context at all*
(§7).  **The code is `slife/agent/system_prompt.py` + `slife/agent/templates/`
(what is rendered), `slife/agent/loop.py` (when it is injected, and the
rebuild's discriminator), `slife/agent/message_history.py` (the persisted
record and the turn→messages builder), the selection itself in
`slife/plugins/memdb/recall.py` + `slife/plugins/memdb/server.py`, and the
trigger authors in `slife/agent/schedules.py`, `slife/agent/heartbeat.py`,
`slife/agent/timer.py`, `slife/a2a/identity.py`, `slife/tools/models.py`.**  If
a statement here and a design note in `DESIGN.md` ever disagree, this document
and the code win.

> This document was split out of `DESIGNER_NOTES.md` (§7 "Context Harnessing")
> when the mechanisms accumulated enough rules to deserve their own home.
> `DESIGN.md` keeps the condensed overview in its *Context Injection* and
> *Context Window Management* sections and links here.

---

## 1. The taxonomy — three orthogonal notions

1. **Channel** — the *sender identity* of a message entering the unified
   inbox.  Recoverable from the message alone (including its payload),
   persisted with the turn (`diary.channel`), and by default **not** part of
   the LLM context.  Kinds: `human`, `wechat`, `subagent`, `heartbeat`,
   `system`, `a2a` (`slife/a2a/identity.py`).
2. **Marker** — machine-generated notation carried *inside* a raw user or
   assistant message, telling the model (or the TUI) something about the
   message that the message text alone does not say.  A marker is what the
   model sees or what the TUI hides.
3. **Harness tool-pair** — a reserved, `_`-prefixed **harness tool** the loop
   auto-invokes once per turn, contributing an assistant `tool_call` plus its
   tool-result to the history.  This is a *mechanism* (invoke a tool to inject
   state), not a content form.

**A marker never determines a channel and a channel never forces a marker.**
A scheduled task is the canonical example: its trigger is a `[Schedule <name>]`
*marker* that rides the **system** channel, and its completion is a *subagent*
channel message that carries no `[Schedule …]` marker at all.

The three notions above are all about *content*: something is added to a
message, or a message is injected.  The **per-turn context rebuild** (§7) is
orthogonal to all of them — it injects nothing and decorates nothing; it
decides which turns exist in the context at all, before the turn's own user
message is added.

---

## 2. Channels — the unified-inbox senders

| Channel | Sender | Typical marker | TUI | Notes |
|---|---|---|---|---|
| `human` | keyboard operator in the TUI | — | `You> ` | The default, normal channel. |
| `wechat` | WeChat peer terminal | `[Wechat:json]` (peer, thread — what a reply needs) | `Wechat> ` | Marker filtered from the TUI display. |
| `subagent` | local worker async completion | `[Subagent:{"subagent_name", "task_id"}]` | `Subagent(<name>)> ` | Live bubble and restore agree on this prefix. |
| `heartbeat` | Slife itself — periodic autonomous window | `[Heartbeat]` | trigger hidden; real reply as `⚡ 自主`; status-bar beat (`●`/`·`) | Silent handler; `.` = silence. |
| `system` | Slife itself — schedule / timer triggers | `[Schedule <name>]`, `[Timer]` | trigger hidden; reply as `📅 定时` / `⏰ timer` | `display_prefix()` is `None` — filtered from live and restored view. |
| `a2a` | mesh peer | `[A2A:json]` (from / task_id? / type) | `A2A(<peer>)> ` | One envelope for every inbound A2A message; `type` = task_request / task_response / message / broadcast. |

The **system** channel is never user input: everything that rides it is a
synthetic trigger, and its turns are filtered from the TUI by both the channel
(read-only, `display_prefix() == None`) and the marker text
(`is_autonomous_trigger` covers the `[Heartbeat]`, `[Schedule …]` and
`[Timer]` prefixes — `slife/agent/schedules.py`).

**Silence contract:** a bare `.` assistant reply is silence from *any* turn
source — the TUI strips it live and restore never renders a lone-dot reply.

---

## 3. Markers

| Marker | Where it appears | Reader | Purpose |
|---|---|---|---|
| `[Heartbeat]` | heartbeat trigger (user-message side) | TUI + loop | Classify the autonomous turn; hide the trigger. |
| `[Schedule <name>]` | schedule trigger (user-message side) | agent + TUI | A cron fire or `run_schedule_now` backfill; the agent dispatches via `run_schedule_now(name=…)`. First line must keep this prefix. |
| `[Timer]` | `wait_minutes` wake (user-message side) | agent + TUI | Resume the agent after a delay. |
| `[Wechat:json]` | WeChat input | agent | Peer + thread the reply needs. |
| `[A2A:json]` | every inbound A2A message | agent | One envelope `{from, task_id?, type}` — `type` distinguishes a task to answer (`task_request`), an auto-delivered result (`task_response`), a conversation (`message`), or an event (`broadcast`). |
| `[Subagent:{"subagent_name", "task_id"}]` | subagent completion content | agent | Which worker, which task; unwrapped for display. |
| `[INFO: …]` | appended to an existing message | agent | Turn footnote / trim note — see §5. |

A marker is a *text* contract on the raw message; classification helpers
(`is_schedule_trigger`, `is_autonomous_trigger`, `is_timer_trigger`) match the
prefix so live rendering and session restore behave identically.

---

## 4. Harness tool-pair — `_turn_prompt`

`_turn_prompt` (`slife/tools/models.py`) is the per-turn status prompt: time,
context usage, changed model/CWD/shell, peer presence events, open
failed/missed scheduled runs, and the one-shot "system restarted" flag.  It is
rendered from `turn_prompt.j2` via `build_turn_prompt`
(`slife/agent/system_prompt.py`).

- **Injected by the loop**, not chosen by the LLM: the loop auto-invokes it
  once per turn before the LLM call (`slife/agent/loop.py`), computing context
  usage once and sharing it with the trim decision and the TUI status bar.
- **It is a tool-pair, deliberately.**  The assistant `tool_call` plus the
  tool-result enter the history as a normal part of the turn, so it persists
  and restores.  It must *not* live in the static system prompt: it changes
  every turn, which would evict the stable system-prompt prefix from the
  prompt cache.
- **Harness-scoped.**  The leading `_` marks it harness; it is excluded from
  the host server's exposed toolset and is context-only (never TUI widgets).

Context trimming is *not* such a tool: it runs internally after save and is
announced by the trim note (§5), not by a harness pair.

### `_check_new_input` — mid-turn message injection (cut-in mode)

`_check_new_input` (`slife/tools/models.py`) is the **zero-argument** counterpart
of `_turn_prompt`, auto-invoked at each *iteration boundary* (before the next
LLM call) when a queued message may cut into the running turn.  It is a mode:
`agent.cutin_enabled` (default **true**) — toggled at runtime with
`set_midturn_input(enabled)`; when false (the original `queue` behavior) the
boundary check is skipped entirely.

- **"Push all", one per boundary.**  The check asks `inbox.has_injectable()`
  (queue non-empty, no channel filter); `_check_new_input`'s execution pulls
  the FIRST queued message via the `extract_injectable` ToolContext hook
  (drain-rebuild, survivors stay FIFO) and returns its **bare text** — the
  content already carries its `[A2A:…]`/`[Wechat:…]` marker, so no wrapper or
  per-kind instructions are needed (the reply protocol lives in the system
  prompt).
- **Same tool-pair mechanics as `_turn_prompt`** — assistant `tool_call` (with
  EMPTY arguments, so the message exists once in context) + tool result,
  recorded and restored.  Extraction is gated behind the same cancel guard so
  a cancelled turn never drops the queued message.
- **Main agent only** — subagents never wire the hooks, and an inbox-absent
  tool just reports "no pending input".
- **LLM judgment.**  An injected message is a live input the model addresses in
  the same turn — or, for an `[A2A:…]` of type `task_response` (result FYI) or
  `broadcast` (event), acknowledges and ignores.  A task it chooses not to
  complete simply ends without a result; the sender's delivery profile retries
  the request a few times, then leaves the task pending.

---

## 5. Context markers on existing messages

Two `[INFO: …]` footnotes decorate messages that are already present rather
than injecting a standalone turn:

- **Turn footnote** — `[INFO: {"turn_id": N, "begin": …, "end": …}]`
  (`turn_header`, `slife/agent/message_history.py`).  Appended to a user
  message after the turn saves, so the next call can reference the turn by
  id; skipped for autonomous/synthetic triggers.  On restore, re-appended to
  the restored non-synthetic turns.
- **Trim note** — `[INFO: N oldest turns have been removed from context]`
  (`trim_note`, same module).  Announced when context trimming evicts turns:
  the trim triggers at the 80 % ceiling using the last API call's **real
  prompt + completion tokens** (the exact count the persisted history will
  re-send), and re-fills to the 20 % floor estimated with `count_tokens`.

---

## 6. A scheduled task's two surfaces

A recurring task produces **two** distinct context/TUI surfaces, one per note
in `DESIGNER_NOTES.md` §7.3:

1. **The fire (dispatch)** — the loop times the task and posts a `[Schedule
   <name>]` trigger aboard the `system` channel.  The user message is hidden
   from the TUI; the agent's dispatch reply surfaces as `📅 定时`
   (`surface_schedule`).  The agent decides the dispatch call itself —
   including whether `clone_context` is needed (the tool schema, not the
   trigger, documents it).
2. **The completion** — the worker's async result returns over the
   **subagent** channel (`source=SUBAGENT`, `Channel.subagent(..., scheduled=…)`),
   exactly like any other subagent completion.  The content is rewritten from
   the run record ("completed — report saved" or the honest failure), not the
   worker's narration; the `scheduled` flag rides the persisted payload (a
   downstream annotation, not the classification source — classification is
   the worker-name set in `slife/agent/schedules.py`).

---

## 7. The per-turn context rebuild — recall selects the context

`agent.rebuild_message` (default **true**) makes the context **selected**
rather than accumulated: before every turn the harness asks the memory store
which turns this turn needs, and replaces the context with the answer.  It is
the implementation of `DESIGNER_NOTES.md`'s *next major refactor* note —
*"before each agent-loop iteration, based on the current context and the user
input, recall the turns relevant to that context and use them to update the
message list the loop is called with"* — with the retrieval parameters chosen
per turn by a model call (§7.1) instead of fixed.

With the flag **false** the previous behaviour runs instead: the context grows
append-only and the trim bounds it.  The two modes are compatible by
construction — one persisted live-context list, one save-append path — so the
flag can be flipped between runs with no migration.  What the flag never
changes is the ceiling: the trim bounds the *window* in both modes (§7.5).

```
run()
  ├─ recall step — once per turn, BEFORE the user message is added
  │    ├─ discriminator: the system prompt + rebuild_messages.j2 → recall params
  │    ├─ turn_recall(query, since, until) → rows; the rebuild reads their ids
  │    ├─ {} / no reply / None / unfetchable → keep the context, continue
  │    ├─ __memory_context_turns_set(ids) | __memory_context_turns_clear()
  │    └─ MessageHistory.rebuild_messages(turns, images_by_turn=…)
  ├─ add_user_message · attach_image · _turn_prompt        (unchanged)
  └─ iteration loop
        └─ save_to_memory → the new rowid is appended to the persisted list
```

The step sits **before** `add_user_message` deliberately: it replaces
`messages` wholesale, so anything appended first — the user message, the
`attach_image` blocks (memory-only, unrecoverable) — would be destroyed.  It
also stays outside the iteration loop, whose per-iteration work
(`_check_new_input`, the tool-schema refresh) belongs to the turn in progress.

### 7.1 The discriminator — one call, and never a participant

`AgentLoop._discriminate_recall` makes exactly one model call per turn, to
decide which history the turn needs.  It is a *discriminator*: it is not in
the conversation, and nothing it says is ever shown.

- **What it is sent** — the **system prompt**, copied (never re-rendered), plus
  a single user message: `rebuild_messages.j2`, which quotes the current input
  and states `turn_recall`'s **own tool schema** — description, parameter
  descriptions and all — read out of the registry.  There is no second copy of
  the parameter surface, so the discriminator is asked for exactly the
  parameters the tool takes, in the tool's own words (the modes below are
  stated in that description).  Its log line carries `msgs` and
  `prompt_chars`, so the shape of the request is on the record per call.
  **No `turn_recall` in the registry, no call**: the schema *is* the
  instruction, so a process without the tool (memdb down or disabled) skips
  the discriminator entirely and keeps its context.
- **What it is not sent** — the history.  Nothing in the selection *needs* it
  (§7.2: the answer overrides, so a turn already in context is simply
  re-selected, never "excluded"), and sending it would make a pre-turn call
  the size of the context.  **This is narrower than the design note**, which
  has the discriminator run *on the current context* with the user message
  replaced; the note's coreference motivation ("再查一下", "那个呢") is
  therefore not served today.  Recorded here as a deliberate, revisitable
  choice rather than an oversight.
- **It never persists and never streams.**  It writes nothing to
  `MessageHistory`, the diary, or the TUI: no part of the call can be seen by
  the turn it selects for, by a later turn, or by a restarted process.
- **It degrades, it does not retry.**  A timeout or provider failure —
  `recall_discriminator_failed`, with the elapsed time — and a reply that is
  not the requested JSON object — `recall_discriminator_unparsed`, with the
  reply text — both return `None`.  Retrying would double the pre-turn latency
  of a call whose fallback (keep the existing context) is perfectly good.
  With no client to ask at all: `recall_discriminator_skipped
  reason=no_llm_client`.
- **Success is logged**: `recall_discriminated msgs=… prompt_chars=…
  took_ms=… query=… since=… until=…`.  These parameters are the entire input
  to the retrieval, so they are the first thing to look at when a selection
  looks wrong.

**What a reply means.**  The parameters select the retrieval, and the
instruction names every mode — a mode it does not name is unreachable, however
good the model is:

| reply | meaning |
|---|---|
| `{}` | **no recall needed** — the context already in hand is enough.  The store is not asked and the context is not touched (the ceiling still bounds it). |
| `since`/`until` alone | **time-only**: the turns inside that range, ranked by nothing but time — no similarity cap, because there is no query to measure against. |
| `query` alone | hybrid search over the whole diary, no time filter. |
| `query` + range | the same hybrid search, both legs windowed. |

An empty object is a *decision*, not a default: read as "give me the most
recent turns" it would silently *replace* the context the discriminator just
judged sufficient, and hand back up to `recall_limit` turns nobody asked for.
`turn_recall` called with no parameters at all likewise recalls nothing rather
than defaulting to a recent-turns list.

`since`/`until` take an ISO date or a relative phrase (`yesterday`,
`last week`).  The empty-query branch must run **before** the hybrid legs:
they cannot express "no query" — an empty query reaches FTS5 as `MATCH ''`
(an error) and embeds to noise.  Every retrieval ends at the same token budget.

### 7.2 The selection — one fusion, three caps, one order

The hybrid legs are FTS5 (the LIKE fallback for CJK, which FTS5's `unicode61`
cannot segment) and sqlite-vec KNN, fused by reciprocal-rank fusion.  The
**similarity cap gates the measured `similarity`, never `rrf_score`**: the
fused score is a function of rank position and carries no magnitude to
threshold.  Keyword-leg hits have no measured similarity and are **exempt** —
an exact match is a stronger signal than a cosine neighbourhood, and "no
number" is not evidence against it.

| cap | knob | bounds |
|---|---|---|
| count | `agent.recall_limit` (40) | turns in the selection |
| similarity | `agent.recall_min_similarity` (0.35) | a *measured* semantic hit; keyword hits exempt |
| tokens | `context_floor` (20 % of the window) | the selection's estimated size, via `estimate_turn_tokens` on the stored turns |

The caps are **recall's own configuration, not the discriminator's
arguments** — the discriminator chooses *what to look for*, never how much of
it to take, which is also why they are absent from the parameter schema it
fills in.

**Order is chronological.**  Membership comes from relevance, order comes from
time, because the list order is the restore contract — a rebuilt turn must
render byte-identically to the same turn restored, which is what the shared
builder (`messages_from_turns`, also used by session restore) guarantees; a
difference would cost a prompt-cache miss on every turn.

**The selection overrides; it does not merge.**  There is nothing to
reconcile: no incumbent to defend, and therefore no need to exclude turns
already in context — re-selecting one is the intended outcome, not a
duplicate.  That is what makes the two keep-paths in §7.4 the *only* two.

### 7.3 The store's answer — ids, or nothing, but never an error

`turn_recall` returns `{"turns": [{"turn_id", "created_at",
"user_message", "summary", "score"}], "degraded": "<reason>"}` — one row
per turn, ascending by turn id, each carrying what identifies the turn and
how well it matched (never the stored messages: `turn_read` is for those).
The rebuild reads the `turn_id`s straight out of these rows; the model reads
the same rows and calls `turn_read` when it wants one in full.  `degraded`
is non-empty when the semantic leg was unavailable (`recall_degraded`,
logged; a degraded leg widens the empty case, which clears the context).

**There is no error return.**  Everything that is not a fatal environment
failure — the store's or the tokenizer's, below — is answered as an empty
selection: a time bound the grammar rejects (the discriminator's phrase
matched no calendar period), a query the store cannot parse, an unexpected
failure in the selection pipeline.  The reason is the
caller's side of the contract — its only safe reading of "error" is *keep the
context you have*, and that would license exactly the wipe an empty selection
performs deliberately.  **A store failure is fatal instead**: `sqlite3.Error`
propagates, like the startup readiness check, because a plausible-looking
empty list from a broken database silently wipes the context.  **An unusable
tokenizer is fatal the same way** (`TokenizerUnavailable`): it is an
environment failure like the store's — every row's cost, and so the budget the
selection is fitted to, comes from it.  Both reach the harness as a tool
*error* (the MCP client turns a raise into an `Error: …` result), so the
context is kept and both sides log it.

### 7.4 What the selection does to the context

- **It replaces the context** — `MessageHistory.rebuild_messages(turns)`,
  built from the stored rows by the same builder session restore uses.
- **An empty selection is an empty context.**  No turn qualified for this
  turn, so the context is the system prompt and nothing else, and the
  persisted list is emptied with it (`__memory_context_turns_clear`;
  `..._set` refuses an empty list by design, because its guard protects a
  *partial* selection, not a deliberate one).  The tracked "Context covers"
  range and the measured occupancy are reset with it, so `_turn_prompt` and
  the status bar do not keep reporting the context that was just dropped
  (`AgentLoop.reset_context_time` — the one place a wipe is not merely a
  *smaller* context, where the previous round's real count remains the better
  read).  Keeping the turns the recall just judged irrelevant would mean the
  recall never took effect.
- **Four ways the context is kept.**  The discriminator answered `{}` (no
  recall needed); no reply came back at all; `recall_turns` returned `None`
  (memdb off, channel unreachable, store failure, an unusable payload); or the
  selected turns cannot be fetched.  Nothing was learned about what the turn
  needs, so a guess is not an improvement on what is already there — and a
  *failed* discriminator keeping the context is the point: defaulting it to a
  recency list would replace the context on the strength of no decision at all.
- **The persisted list is rewritten per turn** (`set` / `clear`), which is
  what keeps the restore contract exact: a restart replays the selection the
  agent exited on, not a re-slice of it.
- **Images** are re-attached from a session-scoped, bounded (100 turns)
  `turn_id → blocks` map, since image blocks are never persisted; a
  non-vision model gets none, so a rebuild after a model switch cannot smuggle
  them back.
- **Workers never rebuild.**  A subagent's history is one-shot per task, so
  there is nothing to select from and the discriminator would cost a model
  call per task.  The role decides, not the config.
- **TUI**: `↻ N turns recalled, context's messages rebuilt`, and `↻ no turn
  recalled — context's messages cleared` for the empty selection.  The context
  changed under the user; silence would make the agent look like it had
  forgotten things for no visible reason.

### 7.5 The ceiling is the window's, not the mode's

`AgentLoop._trim_after_save` bounds the context at `context_ceiling` (80 % of
the window) down to `context_floor`, **in both modes**.  It is a safety valve,
not a function of how the context was *chosen*: a turn whose tool results
ballooned past the ceiling has to be compacted whatever selected the turns
around them.  In rebuild mode the next turn's recall re-selects anyway, so the
eviction is a bound rather than a decision.  The trim's own mechanics —
real-usage detection, the runtime trim note, dropping the evicted ids from the
persisted list, the freshly-restored exemption — are in `DESIGN.md` →
*Context Window Management*.
