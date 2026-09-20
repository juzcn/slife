# Subagents — the agent-worker model

## Design principle

> On subagents, our design is deliberately opinionated. A subagent is a headless agent — a worker, no personality. It can run on an empty context, or fork the agent's context, and has all of the main agent's capabilities. A task is one turn of a subagent, synchronous or asynchronous, with no persistence, without running the main agent's harness, and with no error handling. So the task result is pushed by the harness — never pushed back by the subagent using a tool. — *DESIGNER_NOTES.md:157 (translated from the original)*

A subagent is an **agent worker**: a local child process that runs the same agent loop with the same config and tools as the main agent, but deliberately stripped of everything that makes the main agent a *harness* — no TUI, no turn persistence, no scheduler, no cut-in injection, no A2A inbound drain, no plugin spawn, no health/watchdog duties. It keeps no independent network identity: when it reaches the mesh it sends **as the main agent**. This document is the authoritative statement of the worker model (the condensed view lives in [DESIGN.md](../DESIGN.md) **Part 5 · Subagents**).

Every clause of the design line is a non-negotiable property, mirrored in code:

| Design clause | Where it lives |
|---|---|
| headless agent, a worker, no personality | `slife/subagent/headless.py` (no TUI); identity template `subagent.j2` — "no presence, no personality", acts as the parent |
| empty context **or** fork the main agent's context | `spawn_subagent(clone_context=…)` → a spawn-time snapshot of the parent's message history, or a clean history |
| all of the main agent's capabilities | same `Config` (losslessly inherited — `Config.to_dict`/`from_dict` are derived from one field list, so nothing can be dropped silently), same auto-discovered tool registry, every plugin the parent started shared by port, and the shared tool catalog queried as the parent queries it |
| a task is one turn | one `worker/send` → one inbox message → exactly one `agent_loop.run()` → one JSON-RPC result |
| synchronous or asynchronous | `subagent_send_task` (sync wait) / `subagent_send_task_async` (`mode=auto` push, `mode=poll`) |
| no persistence | `caps.turn_persistence=False` → `inbox._on_turn_complete = None`, and a one-shot history per task — nothing survives the process |
| does not run the main agent's harness | the capabilities the worker is not granted, declared in `slife/agent/roles.py` — the *service* layer, not the loop |
| no error handling | fail-fast: no retries (`caps.stream_retries=False`), errors surface **as the task result** |
| the result push is the harness's | the worker replies over stdout; the **parent** harness auto-pushes `[Subagent:…]` into its own inbox |

### How the difference is recorded

The two roles differ, and that difference is **declared once** — as capabilities
in `slife/agent/roles.py`:

```python
MAIN   = Caps()                    # the full harness
WORKER = Caps(**{…: False})        # granted none of it
```

A capability is a *grant*: the process either owns the resource (the shared
tool catalog's rows, its vector index, the plugin child processes, the host MCP
face, the heartbeat, the scheduler, the mesh inbox drain) or holds the policy
(turn persistence, the stream-retry ladder, the startup gate, mid-turn cut-in).
The main agent holds all of them; a worker holds none — it is the loop, the
tools and the shared infrastructure, with every harness singleton left to its
parent.

Why it is written down rather than spread around: it used to be ~two dozen
`if not self.is_subagent` branches, so a worker's capability set was an
*emergent* property of wherever a gate happened to be written. A capability
added to the main agent's path then simply never reached a worker, silently —
the tool catalog's semantic search was one: a worker held no drainer *and no
query surface*, so every subagent's `tool_search` was keyword-only for its whole
life, against an index its own parent was maintaining in the same database.

Two guards keep the table honest (`tests/test_subagent_parity.py`): an AST gate
that fails on any new `is_subagent` branch outside this table, and a parity test
that builds both roles from one config and asserts their observable difference
is exactly what the table declares. Reading the index is **not** the drainer's
grant — see [TOOL-SYSTEM.md](TOOL-SYSTEM.md) for who owns the catalog and who
may only query it.


## Architecture

```
 Main agent (the harness)
 ┌───────────────────────────────────────────────────────────────┐
 │ AgentService                                                  │
 │  Inbox ── one turn per message ──► AgentLoop (identical loop) │
 │   ▲  [Subagent:…] auto-push (source=subagent)                 │
 │   │                                                           │
 │  _on_subagent_done ◄── SubagentManager.on_task_complete       │
 │  SubagentProcess  (stdin/stdout pipes, bounded task records)  │
 └─────────┬─────────────────────────────────────────────────────┘
           │  JSON-RPC 2.0 (worker/*)          stdin ← worker/send, worker/cancel
           │                                  stdout → {ready}, result, worker/complete
 ┌─────────▼─────────────────────────────────────────────────────┐
 │ Worker child   python -m slife.subagent.headless              │
 │  AgentService(is_subagent=True)                               │
 │   inbox ── worker/send = ONE agent_loop.run() = ONE turn      │
 │   one-shot MessageHistory per task (clean or clone snapshot)  │
 │   no TUI · no persistence · no scheduler · no cut-in          │
 │   shared plugin clients (mcp-gateway/memdb/wechat/a2a/memfiles)│
 └───────────────────────────────────────────────────────────────┘
```

Two agents, **one loop machine**. Both run the identical `AgentLoop` (including the per-turn `_turn_prompt` harness tool-pair and context trim) driven by the identical unified inbox. What differs is the harness around it: the main agent wires the loop to a TUI, a diary, schedules, cut-in and the mesh; the worker wires it to a stdin/stdout JSON-RPC channel and nothing else.

## The process protocol (worker/* over stdin/stdout)

`slife/subagent/process.py` spawns the child; the wire is JSON-RPC 2.0 — deliberately **not** A2A. The worker is a *local* worker, not a mesh peer.

| Direction | Message | Purpose |
|---|---|---|
| child → parent | `{"result": {"ready": true}}` (`id: null`) | startup readiness — the spawn await blocks on it |
| parent → child | `worker/send` (`id` = `rpc_id`) | one task (one turn), correlation by id, never by text |
| parent → child | `worker/cancel` (notification) | drop-if-queued / preempt-if-running (Esc-equivalent) |
| parent → child | `worker/plugin_restart` (notification) | a shared plugin moved to a new port — reconnect the client |
| parent → child | `context` (notification) | the cloned parent history, sent on stdin at spawn time |
| parent → child | `shutdown` (notification) | graceful stop |
| child → parent | `{"result": "<final reply>"}` (`id` = `rpc_id`) | the task's one-turn result |
| child → parent | `worker/complete` (notification) | "the result above is final" (carries only the task id) |

Details worth keeping:

- **Pure UTF-8 on stdout.** `sys.stdout` on Windows defaults to the system code page (GBK) and cannot encode emoji — the worker writes raw UTF-8 bytes to `stdout.buffer` directly (`headless.py:_write`).
- **Piped stdin on Windows.** `connect_read_pipe` fails with `OSError [WinError 6]` for a parent-owned pipe (IOCP registration in the Proactor loop rejects the handle), so a dedicated thread reads with `os.read()` and feeds the event loop (`headless.py:_feed_stdin`). The reader stays live while a task runs so `worker/cancel` can preempt a running loop.
- **Config never rides the process env.** The resolved config (which carries plaintext `api_key`s) is passed via a `0600` temp file (`SLIFE_CONFIG_FILE`, preferred) or the `SLIFE_CONFIG` env fallback — never visible via `/proc/<pid>/environ`. The worker inherits the main agent's in-memory config, not the `slife.yaml` file; only a standalone run (no `SLIFE_SUBAGENT_NAME`) falls back to reading it.
- **Over-long protocol lines are discarded, never fatal.** One line may legitimately be the whole cloned history or a many-MB result; a line beyond even the raised cap is dropped (tail and all) so a pathological line cannot kill the reader or wedge the worker (`PROTOCOL_LINE_LIMIT` / `discard_overlong_line`).

## Spawn & context — one turn per task

`spawn_subagent(name, clone_context=False)` starts a named worker — **a worker's name is its identity**; it is never auto-generated (`spawn_subagent` requires it, `_SAFE_SUBAGENT_NAME` gates it, and reuse is explicit: spawning a running name returns the live worker). Spawning is idempotent for a running worker.

Context is chosen once at spawn:

- **clean** (default): each task runs in a bare `MessageHistory(system_prompt=…)`.
- **cloned**: `_serialize_cloned_context` copies the parent's message history **without the parent's system message** (the worker renders its own), shipped on stdin as the `context` message. No upfront trimming — the loop's internal trim handles overflow once real usage is known.

A clone is a **spawn-time snapshot**: `_WorkerMessageHistoryStore.get_or_create` re-seeds from `_inherited_context` on **every** task, so a cloned worker restarts each task from that fixed snapshot — it never accumulates context across tasks, and it never sees parent turns that happen after spawn.

A task is one turn by construction:

```
worker/send ──► inbox.post(AgentMessage(…, correlation_id=rpc_id, on_reply=_reply))
             ──► inbox._process_one ── get_or_create(source) = fresh history
                                                │
                                                ▼
                             ONE agent_loop.run(user_input=task_text)   ← the whole turn
                                                │
                     final reply text ──► on_reply(reply_text)
                                  ──► _write(result=…, id=rpc_id)  +  _notify(worker/complete)
```

Inside that single `run()` the loop may do many LLM calls and tool calls (bounded by `max_iterations`), but from the task's point of view there is exactly one turn, one reply, one result. The worker processes tasks **serially**; a busy worker's extra sends are queued by the **parent** (`SubagentProcess` FIFO bookkeeping), never refused and never re-sent.

## Identity & system prompt

The worker renders its own system prompt with `is_subagent=True`; `subagent.j2` is the identity template (the world spec `slife.j2` is included byte-identical in both roles):

- **You are `{name}`, an agent worker of `{agent_name}`** — a headless process with the same capabilities.
- **No identity of its own** — "no presence, no personality. In all external communication (A2A / WeChat) you act as `{agent_name}` — NEVER introduce yourself by name or persona."
- **Ephemeral** — "your turns are never saved to memory … nothing you do outlives this process."
- `context_source` (`clean` / `cloned`) is rendered, so the worker knows how it was seeded.
- Platform type is `"headless"` (`SLIFE_SUBAGENT_NAME` set or stdin not a tty) — the TUI/terminal affordances are absent.

The completion result posts back under the unified-inbox source sentinel **`SUBAGENT = AgentName("subagent")`** (`slife/subagent/identity.py`), distinguishable from human turns in memory search, yet routed into the human history.

## Result delivery — the harness pushes, the worker never does

The worker has **no result-push tool**. Its reply goes out as an ordinary JSON-RPC `result` on stdout (the `on_reply` closure fired by *its* inbox). The **parent harness** does all the pushing:

```
worker result ──► SubagentProcess._dispatch_message
      │  sync waiter → resolves the pending future (send_task returns it)
      │  async      → stored in the bounded FIFO  +  _notify_manager_task_done
      ▼
 SubagentManager.on_task_complete = _on_subagent_done   (AgentService)
      │  content = _scheduled_?  _schedule_completion_content
      │           : subagent_marker(name, task_id) + "Subagent **<name>** completed async task …"
      ▼
 inbox.post(AgentMessage(source=SUBAGENT, channel=Channel.subagent(name, task_id, scheduled)))
      └──► a normal turn in the parent's human history (one turn, like any other source)
```

- The pushed content carries the machine marker **`[Subagent:{"subagent_name":…, "task_id":…}] `** (`message_history.subagent_marker`) so the LLM can attribute it; the TUI drops the marker and shows the `Subagent(<name>)>` bubble instead.
- The channel is **`Channel.subagent(name, task_id, scheduled)`** — `scheduled=true` for schedule workers. Per [DESIGN.md §6.3](../DESIGN.md), the pushed `subagent` channel turn is persisted and appears on restore like any other.
- There is deliberately **no `subagent_subscribe_task`** — async results are auto-subscribed (`tools/subagent.py:17`). `mode="poll"` suppresses only the *push*; the result still lands in the retrievable FIFO.

## Failure & timeout semantics

The worker has no error-handling loop of its own; every failure ends in a result coming back:

| Failure | Surface | When |
|---|---|---|
| LLM/provider error inside the turn | the reply text is `"Error: …"` — the parent's future resolves successfully with that text | fail-fast: `stream_max_retries=0`, `stream_timeout = work.task_budget` (`service.py:237-245`) |
| Protocol error frame (`-32602` bad params, `-32601` method not found) | JSON-RPC `error` → parent raises `RuntimeError` → tool returns `"Error sending task …"` | an invalid request |
| Worker process exit / pipe close before the reply | the pending future raises "closed before task was resolved" → tool error branch | worker died |
| **Stall** — no reply at all (silent provider stall, deadlocked loop) | `asyncio.wait_for(future, work.task_budget)` → `TimeoutError` → tool returns "Timed out waiting …" | the task never resolves |

The stall case is the interesting one:

- The abandoned task is **preempted in the child** via `worker/cancel` — a worker is serial, so a genuinely stuck task must never block later tasks (`process.py:327-335`).
- Any **late** result is stored for `subagent_get_task_result` but **never auto-pushed** — the caller was already told it timed out.
- `subagent_cancel_task` (parent-side) does the same for a user-cancelled task, and the parent discards the late reply for a cancelled id.

This matches "no error handling" precisely: no retries, no recovery, no second attempt — errors surface as pushed-back results, and the `work.task_budget` timeout is the only bound that keeps a sync call or a serial worker from hanging forever. `stream_max_retries=0` is explicit (`service.py:263`): the worker is the one agent that does **not** participate in the stream-retry ladder — even a transient transport failure is a single attempt, surfaced immediately as an `Error:` result. The per-chunk **stall watchdog** (`work.stall`, an inactivity timer that applies to every agent) still cuts a silent provider, but in a worker a stall is surfaced, never retried. The retry contract (DESIGN.md **Agent Loop · LLM stream failure contract**) is a main-agent one.

## What the worker does not run

The worker is the same loop wired to nothing else. Excluded from the *service layer* — one row per capability, and the capability's name **is** the gate in `slife/agent/roles.py` (the code locations are deliberately not listed: a line number in a table is a second copy of the code, and this one rotted twice before it was removed):

| Capability (`Caps` field) | Main-agent harness feature | Worker behavior |
|---|---|---|
| — | TUI handler & streaming | absent — headless (`slife/subagent/headless.py`) |
| `turn_persistence` | Turn persistence (`save_to_memory`) | `inbox._on_turn_complete = None` — even with the shared memdb |
| `schedules` | Scheduler (`run_schedule_now`, wakeups) | hooks left `None`, no trigger loop, no startup sweep |
| `cutin` | Cut-in injection (`_check_new_input`) | `pending_input_has` never bound → **never auto-invoked**; `extract_injectable` unset → an explicit call is a no-op |
| `plugin_children` | A2A inbound drain & presence; plugin spawn / watchdog / health | thin client only — can send as the parent, never drains; connects to the **shared** parent plugin servers by inherited port (`SLIFE_{NAME}_PORT`) |
| `heartbeat` | Heartbeat loop | task-driven only — no idle turns |
| `host_server` | slife-as-plugin host server | absent for subagents |
| `startup_gate` | Startup plugin-convergence gate | inbox `ready=None` (shares the parent's plugins, spawns none) |
| `stream_retries` | LLM stream retry ladder | fail-fast: `stream_max_retries=0`, a capped stream timeout |
| `catalog_owner` | Tool-catalog rows: boot seed, skill/cli mirror, reconcile projections, config purge, status marks, LRU eviction | reads the shared `tools.db` and never writes it — the parent seeded the same rows |
| `catalog_drainer` | The catalog's embedding index | runs no drainer; **queries** the index its parent maintains (`SemanticReader`) |

Every excluded feature is a *plugin of the loop*, not the loop itself — which is why the worker still runs the identical `AgentLoop` including the `_turn_prompt` harness tool-pair and the internal context trim. "Does not run the main agent's harness" is a statement about the service/orchestration layer, not the loop internals.

### Coming up: a capability must be ready when work starts

The parent agent loads its capabilities at boot, so nothing it does can race its own startup. A worker that initializes a capability *on first use* has no such guarantee, and the failure is quiet: the first turn fans out several tool calls, one of them triggers the initialization, and the others read a half-built state and report the capability as unavailable. If that state is a boolean (``available``), "not yet" arrives as "no".

The rule this file's capabilities follow, and the one to apply to the next one:

* **Never report "not yet" as "no".** An initialization in flight must be awaited — by every caller, not just the one that started it — so the only answer a caller can get is the finished one. (`EmbeddingClient.load` shares an in-flight future for exactly this; the reader's failure flag is set *after* the await, never before.)
* **Warm what is cheap at boot.** The boot handshake already waits for the plugin clients (`connect_shared_plugins`) before it reports `ready`, because a worker's tools must work on its first turn. A capability that cannot be awaited at boot without coupling spawn to a remote endpoint (the semantic client probes the embedding endpoint) comes up on a background task instead — the window moves to spawn, where the handshake covers it, and the first query then waits rather than degrades.
* **A degradation is a transition, and transitions are announced.** One line per change, not one per query: a silently degraded capability looks like an empty result set, which for a Chinese keyword query is indistinguishable (the CJK path requires each space-delimited run to appear verbatim, so a natural-language Chinese query matches nothing once the semantic leg is gone).

## Shared plugins & recursion

- **Every plugin the parent started, shared by port.** The worker reads `SLIFE_<NAME>_PORT` for each discovered plugin and connects as an MCP client over Streamable HTTP — a manifest loop, not a hard-coded subset (`AgentService.connect_shared_plugins`). It never spawns its own plugin processes; a worker crash takes down only the worker, never shared infrastructure (the parent's watchdog is untouched).
- **No isolation.** Shared servers are exactly the parent's servers — same memdb, same cabinet, same MCP gateway. The worker is trusted, never fenced.
- **Mesh as the parent.** The a2a plugin registers the `a2a_*` tools in the worker so it can send on the parent's identity; the inbound queue stays with the parent (a worker that can push into the parent's inbox via A2A could confuse *its own* history, so only the send side is shared).
- **Recursion is allowed.** A subagent can `spawn_subagent` its own descendants — each level has its own `SubagentManager` (there is intentionally no subagent-specific gate; trust, not enforcement).

## LLM tool surface

Native, "Subagent" category (`slife/tools/subagent.py`):

| Tool | Behavior |
|---|---|
| `spawn_subagent(name, clone_context=False)` | spawn or reuse a named worker; `clone_context` seeds it with a snapshot of the parent's history |
| `list_subagents` | running workers: PID, readiness, busy/async counts, context source |
| `subagent_send_task(name, task, timeout)` | sync — waits for the one-turn result; `timeout` (positive int) overrides the registry default `work.task_budget`, 0/negative falls back |
| `subagent_send_task_async(name, task, mode=auto\|poll)` | async — `auto` pushes the result when done (and stays pollable); `poll` suppresses the push |
| `subagent_get_task_result(name, task_id)` | poll a stored result, or `pending` |
| `subagent_list_tasks(name, status)` | worker task records (bounded per worker) |
| `subagent_cancel_task(name, task_id)` | cancel queued/running — drop or preempt; late reply discarded |
| `stop_subagent(name)` | stop the worker process |

Busy-worker semantics: a sync `subagent_send_task` to a busy worker is **auto-converted to async and reported** (task_id given, auto-push enabled, still pollable) — never a silent timeout, never a "resend". The worker processes one task at a time.

## Scheduled tasks ride the same model

A scheduled task is a **named worker per task name** dispatched by `run_schedule_now` via `subagent_send_task_async` with deterministic task text (usually `clone_context=True`). Completion rides the exact auto-push above, but the pushed content is reworded by `_schedule_completion_content` to hide the worker detail — "report saved" is decided by the run record, never by the worker's narration (a worker whose report generation died is never announced as "report saved"). The channel is marked `scheduled=true`. (See DESIGN.md **Part 2 · Scheduled Tasks**.)

## Config & timeouts

- `subagent.max_subagents` in `slife.yaml` (default **5**) — no `enabled` toggle; subagents are always available.
- Timeout values are **developer-owned, in `slife/timeouts.py`**, never user-read from config: the worker task bound and stream total are `work.task_budget`; the spawn-ready wait is bounded by `ready.spawn`. Per-call `timeout` on `subagent_send_task` is the one LLM-facing override. (See [TIMEOUT.md](TIMEOUT.md) — *timers at the owner, no total*.)
- Environment handoff: `SLIFE_SUBAGENT_NAME`, `SLIFE_SUBAGENT_CREATED_AT`, `SLIFE_SUBAGENT_CONTEXT`, `SLIFE_CONFIG_FILE` (or `SLIFE_CONFIG`), plus the inherited per-plugin `SLIFE_{NAME}_PORT` set.

## Windows note

Two worker-specific platform guards (both live next to the code that needs them):

- **Piped stdin** — asyncio's `connect_read_pipe` rejects the parent's pipe handle on the Proactor loop (`OSError [WinError 6]`), so stdin is read on a dedicated `os.read()` thread feeding the loop (`headless.py:_feed_stdin`).
- **Raw UTF-8 stdout** — the default text codec is the system page (GBK on zh-CN) which cannot encode emoji; all protocol writes bypass the codec (`headless.py:_write`).

## Tests

- `tests/test_subagent_headless.py` — the worker process: protocol framing, context cloning, `worker/complete`, shutdown, over-long-line discard.
- `tests/test_subagent_process.py` — `SubagentProcess` / `SubagentManager`: spawn/ready, sync timeout + preempt, late results, cancel, bounded records, serial/queue behavior.
- `tests/test_subagent_tools.py` — the LLM tool surface: schema, busy-queue conversion, async mode delivery.

No e2e tag — the worker protocol is covered by the unit suite; a live round-trip is exercised by the ordinary integration run.

## Notes / accepted gaps

- **`worker/progress` is parsed but never emitted** — the parent accepts the notification (debug-level) but no worker sends it today; the result is the only completion signal.
- **The clone is a static snapshot.** Forking happens once at spawn; each task re-seeds from that snapshot. There is no live handoff of the parent's context, and no incremental context sharing between parent and worker.
- **A timed-out sync task's result is stored, never auto-pushed.** The caller is told to poll; a silent push after a reported timeout would double-announce a task the caller already believes failed.
- **Worker task records are parent-local and bounded** (500 records / 200 async / 500 cancelled, FIFO). They are *not* the A2A mesh task store, and nothing is persisted across restarts.
- **No error recovery is a feature.** A worker that fails mid-loop returns an `Error:` result; the parent decides what to do with it. There is no retry, no resume, no checkpoint.