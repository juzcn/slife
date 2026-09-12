# Context Harnessing

The single, authoritative description of how Slife curates the context the
model sees each turn — the sources that inject information on Slife's own
initiative, the taxonomy that distinguishes them (**channel** vs **marker**
vs **harness tool-pair**), and each concrete mechanism.  **The code is
`slife/agent/system_prompt.py` + `slife/agent/templates/` (what is rendered),
`slife/agent/loop.py` (when it is injected), `slife/agent/message_history.py`
(the persisted record), and the trigger authors in `slife/agent/schedules.py`,
`slife/agent/heartbeat.py`, `slife/agent/timer.py`, `slife/a2a/identity.py`,
`slife/tools/models.py`.**  If a statement here and a design note in
`DESIGN.md` ever disagree, this document and the code win.

> This document was split out of `DESIGNER_NOTES.md` (§7 "Context Harnessing")
> when the mechanisms accumulated enough rules to deserve their own home.
> `DESIGN.md` keeps the condensed overview in its *Context Injection* section
> and links here.

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

---

## 2. Channels — the unified-inbox senders

| Channel | Sender | Typical marker | TUI | Notes |
|---|---|---|---|---|
| `human` | keyboard operator in the TUI | — | `You> ` | The default, normal channel. |
| `wechat` | WeChat peer terminal | `[Wechat:json]` (peer, thread — what a reply needs) | `Wechat> ` | Marker filtered from the TUI display. |
| `subagent` | local worker async completion | `[Subagent:{"subagent_name", "task_id"}]` | `Subagent(<name>)> ` | Live bubble and restore agree on this prefix. |
| `heartbeat` | Slife itself — periodic autonomous window | `[Heartbeat]` | trigger hidden; real reply as `⚡ 自主`; status-bar beat (`●`/`·`) | Silent handler; `.` = silence. |
| `system` | Slife itself — schedule / timer triggers | `[Schedule <name>]`, `[Timer]` | trigger hidden; reply as `📅 定时` / `⏰ timer` | `display_prefix()` is `None` — filtered from live and restored view. |
| `a2a` | mesh peer | `[A2A:json]` (peer, +task), `[A2A-PUSH:json]` (result push) | `A2A(<peer>)> ` | Peer name may double as the persisted identity. |

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
| `[A2A:json]` | mesh message / task | agent | Peer (+ task) who sent it. |
| `[A2A-PUSH:json]` | mesh result push | agent | Same as `[A2A:json]`, for auto-pushed results. |
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
  content already carries its `[A2A:…]`/`[A2A-PUSH:…]`/`[Wechat:…]` marker, so
  no wrapper or per-kind instructions are needed (the reply protocol lives in
  the system prompt).
- **Same tool-pair mechanics as `_turn_prompt`** — assistant `tool_call` (with
  EMPTY arguments, so the message exists once in context) + tool result,
  recorded and restored.  Extraction is gated behind the same cancel guard so
  a cancelled turn never drops the queued message.
- **Main agent only** — subagents never wire the hooks, and an inbox-absent
  tool just reports "no pending input".
- **LLM judgment.**  An injected message is a live input the model addresses in
  the same turn — or, for an `[A2A-PUSH:…]` result FYI, acknowledges and
  ignores.  Tasks it chooses not to complete are covered by the sender's
  sync-timeout auto-degrade.

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