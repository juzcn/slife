# Timeout Design Review — slife

**Scope:** how slife enforces time bounds on LLM streams, tool execution, subprocesses, MCP/plugin servers, the A2A mesh, and background workers. Reviewed the live tree at `main` (5671bef), with attention to `DESIGN.md` §Timeout Architecture and the A2A timeout/degrade work landed in the last few commits.

**Method:** full inventory of every timeout/deadline/wait in `slife/` (55 files), then close reads of the enforcement points: `agent/loop.py`, `a2a/client.py`, `a2a/mqtt.py`, `a2a/task_store.py`, `subagent/process.py`, `plugins/mcp_gateway/client.py`, `tools/exec.py`, `platform.py`, `agent/plugins.py`, `agent/service.py`, `config.py`.

---

## 1. Architecture summary

The codebase follows one consistent principle, stated explicitly in several places: **"timer at the owner, no total"** (DESIGN.md:928). Timeouts live where the resource is owned, are enforced per-step rather than as whole-operation walls, and — for the agent-mesh layers in particular — **degrade to a slower-but-surviving mode instead of failing**:

| Layer | Owner of the timer | Policy on expiry |
|---|---|---|
| LLM stream (inactivity) | `_consume_stream` per-`anext()` (`asyncio.timeout`, reset per chunk) | `StreamStallError` → retry ladder |
| LLM stream (total) | `_process_stream` (`stream_timeout`, subagents only) | `TimeoutError` → surfaced to caller |
| Tool call | `_execute_tools` `asyncio.wait_for` (loop `tool_timeout`, fallback) **or** the tool's own native `timeout` param | error string returned to the model; child process tree killed |
| A2A `send_task` wait | `a2a/client.py` `wait_for(future, task_timeout)` | **auto-degrade to async** — record stays pending, late result auto-pushed |
| A2A peer liveness | `_prune_stale_peers` (heartbeat 15 s / timeout 45 s) | peer marked offline (event `"timeout"`) |
| Subprocess lifecycle | `platform.terminate_process` | SIGTERM → 3 s → SIGKILL → 5 s |
| Subagent task wait | `send_task` `timeout=120` | error + `worker/cancel` preempt (serial-worker recovery), record marked failed |
| MCP plugin spawn | `PLUGIN_SPAWN_TIMEOUT = 60 s` outer hang-guard | convergence still fires; child left running in background |
| MCP connect / list_tools | gateway client (`10 s`/attempt, `min(tool_timeout, 20)` via `asyncio.timeout`) | retry ladder / recoverable `TimeoutError` |

Two deliberate design choices recur and are well documented:

- **Cancellation is distinguished from timeout everywhere.** `_process_stream` never retries `CancelledError`; `send_task` treats cancel as "true give-up" (drops the future, marks the record terminal) but a wait-timeout as "already delivered, keep it alive"; `MCPClient.connect` uses `_is_external_cancel()` to tell a real controller cancellation from the SDK's own cancel-scope teardown.
- **Every timer-backed data structure is bounded**: MQTT subscription queues (1000), merged inbox queue (1000), `_completed_tasks` (100), `TaskStore` (500), `_MAX_USAGE_CACHE` (1000), `_MAX_CONTEXT_DATES` (5000), `_poll_tasks`/`_presence_events` (`_MAX_QUEUED`). Timeout-retention can't grow memory.

---

## 2. Per-surface inventory

### 2.1 Agent loop — `agent/loop.py`
- `tool_timeout` (config `agent.tool_timeout`, default **120 s**, `0` = disabled) is the **fallback** `asyncio.wait_for` for tools without a native `timeout` param (loop.py:1125-1134, 1165-1187).
- **Native-timeout contract** (loop.py:1031-1042): tools whose schema has `timeout` (e.g. `execute_shell`) receive `_timeout` mapped onto the native arg and enforce their own deadline — no double wrap. DESIGN.md:354-358.
- **Bare `timeout` alias** (loop.py:1044-1057): an LLM-appended schema-less `timeout` arg is popped and enforced like `_timeout` instead of letting the server reject the whole call.
- **`_async: true`** background tasks still receive the loop bound when the tool has no native timeout (loop.py:1118-1134) — fixes the historical `run_python_script` "ran forever" leak.
- **Stream stall watchdog** `_LLM_STREAM_STALL_TIMEOUT = 120 s` (loop.py:54, 737-774): `asyncio.timeout` around each `anext()`, reset per chunk — a slow-but-live generation is never cut. Root cause: a Bailian provider stall answered `200 OK` then sent nothing ~7 min (DESIGN.md:928).
- **`stream_timeout`** — opt-in *total* wall-clock cap per stream call; `None` on the main agent, `task_timeout` for subagents (service.py:241-244, 261). Docstring explicitly separates "total cap" vs "inactivity" (loop.py:289-313).
- **Stream retry ladder** `_LLM_STREAM_MAX_RETRIES = 2` + linear backoff `0.5 · attempt` (loop.py:36-44, 844-920). Retry classification spans both http generations (httpx/httpcore and httpx2/httpcore2) plus the openai/anthropic SDK wrapper errors and `StreamStallError`. Subagents run `stream_max_retries = 0` (fail fast — no user to wait on).
- **No whole-turn deadline** — the loop is bounded per-step only (stall, tool timeout, `max_iterations = 30`). See finding F2.

### 2.2 Tool execution — `tools/exec.py`, `tools/system.py`
- `execute_shell`: default **30 s**, per-tool overridable via the `tools` config section (`factory.py:37`). Own process group + `kill_process_tree` on timeout so the whole tree dies (exec.py:187-208). Read side is head/tail bounded (100 KB / 20 KB) so a memory budget can't be exceeded.
- `run_python_script`: **no own timer** — relies entirely on the loop's `tool_timeout` wrapper; kills the tree on `CancelledError` (exec.py:294-299).
- `install_python_package`: hardcoded `wait_for(..., 120)` (exec.py:352-358).
- `tools/system.py:343`: `_PROBE_TIMEOUT = 5.0` on HTTP probes.

### 2.3 Subagents — `subagent/process.py`
- Spawn readiness: **hardcoded `30.0 s`** on `_ready.wait()` (process.py:199). See finding F1.
- Task wait: `send_task(timeout=120.0)` default, driven by `subagent_config.task_timeout` (process.py:293, 658, 698-701).
- On wait-timeout: drops the future, releases the in-flight slot, **preempts the task in the child** via `worker/cancel` (the worker is serial — without this, one stuck task blocks all later work forever), marks the record failed, keeps the late result out of the auto-push path (process.py:310-335). `_MAX_CANCELLED` bounds the late-result set.
- Stop: `terminate_process` (3 s graceful / 5 s force) after a JSON-RPC shutdown notice.

### 2.4 A2A — `a2a/`
- Config (`a2a/config.py`): `heartbeat_interval 15 s`, `heartbeat_timeout 45 s` (3×), `task_timeout 120 s`.
- **`send_task` auto-degrade** (`a2a/client.py:287-359`): the wait is `wait_for(future, task_timeout)`; on expiry the request was already published, so instead of failing the caller, the record is left **pending**, the future is dropped, and the late result flows through `_handle_result`'s async branch (store + `[A2A-PUSH]` to the inbox). The store was changed so a wait-timeout is no longer terminal (task_store.py:123-138). Explicitly: "never retry a timed-out sync call." Cancellation (loop `tool_timeout`, `a2a_cancel_task`) is the only true give-up.
- Stateless messages (`record=False`) degrade identically (client.py:340-346; `a2a_send_message` server.py:319-352).
- The plugin tools `a2a_send_task`/`a2a_send_message` take `timeout` as a **native schema param** so the sender's `_timeout` meta-param maps onto it and the wait is enforced plugin-side, not mid-flight by the harness (server.py:267-270, 334-336).
- MQTT publish ack: paho's blocking `wait_for_publish` is hoisted into a thread under `wait_for(..., 6.0)` so the event loop never parks; unacked → logged warning (mqtt.py:230-243).
- Broker connect: `_wait_for_connection(10.0)` (mqtt.py:148), paho keepalive 30. Duplicate-name detection listen window: `asyncio.timeout(1.5)` during connect (client.py:144-168).
- Peer pruning is **event-driven** (on every presence sighting, including our own heartbeat echo), not a monotonic clock — see finding F6.
- Watchdog stability: `_WATCHDOG_STABLE_UPTIME = PLUGIN_SPAWN_TIMEOUT` (plugins.py:74).

### 2.5 MCP gateway / plugin host
- `_CONNECT_ATTEMPT_TIMEOUT = 10.0` bounds the whole attempt incl. transport setup; `_CONNECT_RETRY_DELAY = 0.5`, `_CONNECT_RETRY_ATTEMPTS = 20` (gateway/client.py:81-93). Outer-bounded by `PLUGIN_SPAWN_TIMEOUT` at the service (service.py:610).
- `_CLEANUP_TIMEOUT = 2.0` on `aclose()` so a hung teardown can't block the retry loop.
- `list_tools` capped at `min(tool_timeout, 20.0)` using **`asyncio.timeout`, not `wait_for`** — a stuck SSE session on Windows/Proactor can defeat `wait_for`'s cancellation (the inner task never finishes cancelling); `asyncio.timeout` raises at the deadline without waiting (gateway/client.py:410-423). This is the same hazard class the DESIGN docs pick out; it's the one place the host learned it, others follow.
- Port-signal read: `PORT_SIGNAL_TIMEOUT = 60.0` with per-line 1 s bounds on stderr debug reads (gateway/process.py:33, 152-198).
- OAuth: `_POLL_TIMEOUT = 300.0`, poll bounded by `min(expires_in, 300)` (oauth.py:88, 278).
- Host server + job_coding plugin each declare their own `_NOTIFY_TIMEOUT = 5.0` for `tools/list_changed` (host_server.py:87-89, job_coding/server.py:60-61).

### 2.6 Plugins, services, workers
- `PLUGIN_SPAWN_TIMEOUT = 60 s` hang-guard (plugins.py:65) — deliberately generous; **a 30 s cap previously misfired and aborted a required plugin on a slow machine** (plugins.py:63-64, DESIGN.md:401, 936).
- Memory save: `wait_for(__memory_save_turn, 10.0)` — a timeout surfaces *uncertainty* ("the row may still be written"), not silent skip (service.py:1668-1682).
- memfiles tunnel: `aiohttp.ClientTimeout(total=30)` (server.py:437); sharefile tunnel settle `_TUNNEL_SETTLE_TIMEOUT = 20 s` at the service (service.py:72, 874-894).
- Sharefile: `_START_TIMEOUT = 30 s` URL-event deadline, `_TUNNEL_START_TIMEOUT = 45 s` supersede window (providers.py:72-76, 608-621).
- Media adapters: httpx `300 s` read / `30 s` connect on downloads; generation total deadline `deadline_s = 1200 s` with async polling (adapters/base.py:49, 94-95; dashscope_aigc.py:214-238).
- WeChat: aiohttp `total=120` (client.py:411), QR-poll deadline 600 s (server.py:397), typing keepalive deadline loop.
- Heartbeat: `heartbeat_interval = 1800 s` idle (config.py:367), `HEARTBEAT_INTERVAL = 1800` (heartbeat.py:31); non-positive config falls back to default (heartbeat.py:98-104).
- Health probes: `subprocess.run(timeout=5)` per toolchain version check (health.py:106-165).
- UI shutdown: `wait_for(coro, 3.0)` per service (ui/app.py:537).
- `platform.terminate_process`: SIGTERM → `graceful_timeout=3` s → SIGKILL → `force_timeout=5` s; crash-path sync variant polls `waitpid` on `WNOHANG` every 0.05 s to a 3 s deadline (platform.py:211-318).

---

## 3. Findings (severity-ordered)

### F1 — Subagent readiness budget (30 s) is smaller than its own plugin-enablement budget (60 s) — a known-failure class already fixed elsewhere
**Where:** `subagent/process.py:199` (`timeout=30.0` on `_ready.wait()`) vs `agent/plugins.py:65` (`PLUGIN_SPAWN_TIMEOUT = 60.0`).

**Issue:** The plugin spawn guard was deliberately raised 30→60 s because a 30 s cap "misfired on a slow machine and aborted a required plugin (memdb/memfiles) that was still making progress" (plugins.py:63-64). A subagent's `start()` goes through the *same class of work* — spawning a Python process, importing heavy deps (agent/llm_backends, plugins), connecting to the shared plugin servers (MCP/memdb/wechat via inherited ports, headless.py uses `SLIFE_*_PORT`), then reading the cloned context off stdin — yet its readiness wait is still the hardcoded 30 s that the plugin path proved too tight. On the same cold slow machine, the parent kills a merely-slow subagent at 30 s with "not ready within 30s" while the 8-plugin spawn concurrently takes ~35 s+.

**Impact:** Sporadic subagent spawn failures on cold/midly-loaded machines; the error text points at readiness when the real cause is budget, and there is no retry.

**Suggestion:** Derive the subagent readiness budget from the same knob the plugin paths use (at minimum `max(30, PLUGIN_SPAWN_TIMEOUT)`, ideally a shared `spawn_timeout` config), and make the child's readiness deadline ≥ its plugin spawn budget. The `30.0` is not configurable at all today.

### F2 — No wall-clock bound on a whole turn (deliberate, but un-configurable)
**Where:** `agent/loop.py:_execute_tools`/`run`; everything else caps a *step*, not the turn.

**Issue:** The main agent has no total-duration cap: a turn is bounded only by `max_iterations` (30) × per-step timer (`stream_stall` 120 s, tool timeouts 120 s each, retry ladder with backoff). Worst-case single turn ≈ 30 iterations × several minutes each a turn can run for a very long time with no way for configuration to cap it. DESIGN.md:928 blesses this ("timer at the owner, no total") and it is the right default for a *user-interactive* agent — the TUI shows progress and Esc-cancel exists — but there is no `agent.max_turn_seconds` escape hatch, which matters for (a) unattended/heartbeat modes (heartbeat turns, which by definition have no user watching, run with the same unbounded budget), and (b) scheduled/A2A-inbound turns where the peer's `task_timeout` (120 s) will fire long before a heavy turn finishes.

**Impact:** Low today — the per-step design is sound and cancellation works — but the gap compounds in unattended contexts. The task-timeout auto-degrade on the A2A side (dst 120 s) already papers over it; local subagent turns are cut at 120 s by `stream_timeout` while the main agent's equivalent runs unbounded.

**Suggestion:** Consider an optional `agent.turn_timeout` (default off) wired through `loop.run()` that races the whole turn and returns a cancelled-style result; keep the interactive default unlimited. At minimum, wire the heartbeat/scheduled paths to a bound.

### F3 — `_NOTIFY_TIMEOUT = 5.0` duplicated in two modules; retry-window comment stale ("30 s spawn guard")
**Where:** `mcp/host_server.py:89` and `plugins/job_coding/server.py:61` each declare their own `_NOTIFY_TIMEOUT = 5.0`; `plugins/mcp_gateway/client.py:89-93` says the connect retry window is "bounded above by the 30 s spawn guard" but the guard is now **60 s** (plugins.py:65).

**Issue:** (a) Two identical notification-deadline constants live in sibling modules — drift risk if one is tuned. (b) The gateway client's retry math (`20 attempts × 10 s attempt cap` ≈ up to 200 s worst case) and its rationale refer to the pre-bump 30 s guard; the true outer bound is 60 s via `service.py:610`. The per-attempt 10 s cap is also *smaller* than the server's own port-signal budget (60 s), so a plugin that legally takes 40 s to serve (ngrok tunnel settle is 20 s alone) burns its client retry window before the server is ready — the outer 60 s guard then only rescues it if the child emits the port signal late too.

**Impact:** Minor-to-moderate; comment drift plus a tighter inner window than the outer budget implies. In practice the outer `asyncio.timeout(PLUGIN_SPAWN_TIMEOUT)` bounds it, so it is latent.

**Suggestion:** Hoist `_NOTIFY_TIMEOUT` to a shared constant (e.g. `mcp/common.py`), and re-comment the gateway retry window against the live `PLUGIN_SPAWN_TIMEOUT` (or cap `_CONNECT_ATTEMPT_TIMEOUT` to track it explicitly).

### F4 — Subagent LLM-stream cap reuses the `task_timeout` knob for a different phase
**Where:** `agent/service.py:241-244` — `stream_timeout = subagent_config.task_timeout (120)`.

**Issue:** One budget bounds two unrelated durations: how long `send_task` waits for a result, and how long a *single* LLM stream may run. A legitimately slow-but-live worker generation (long thinking, big tool loop) gets cut at 120 s by `stream_timeout` even though the stall watchdog (120 s inactivity) already guards against truly dead streams — and the subagent-side cut flows back to the caller as a hard error, whereas the main agent's equivalent generation would be allowed to run. The `stream_stall_timeout` alone would protect against hangs; the total cap is the riskier half.

**Impact:** Low, but surprising for users who raise `task_timeout` for slow peers and find their local worker streams cut at the same budget.

**Suggestion:** Give subagents an independent `stream_timeout` config (or default it to `None` and rely on the stall watchdog + tool timeouts, matching main-agent behavior).

### F5 — `execute_shell`'s native default (30 s) disagrees with the system prompt's "≈120 s" harness default
**Where:** `tools/exec.py:154-169` (default 30 s) vs `agent/templates/slife.j2:52` ("omit for the harness default (≈120 s)").

**Issue:** For any tool *without* a native `timeout` param, omitting `_timeout` yields the harness default (~120 s), but for `execute_shell` omitting it yields **30 s** (the tool's own default). The prompt is a universal claim; the shell tool silently halves-quaters the actual bound for calls that pass neither arg.

**Impact:** Cosmetic-to-modest: an LLM writing long-running commands with no `timeout` gets cut at 30 s and may not understand why (the error message says the harness killed it, not the tool). Because it is a *native-timeout* tool, the loop explicitly does not double-wrap it.

**Suggestion:** Either pass the loop's effective `tool_timeout` as the shell tool's default when the LLM omits `timeout` (one line in the native-timeout mapping), or narrow the prompt's "(≈120 s)" wording for native-timeout tools.

### F6 — A2A peer timeouts are traffic-coupled, not clock-driven
**Where:** `a2a/client.py:513-539` — `_prune_stale_peers` runs only inside `async for msg in self._adapter.messages("Slife/+/presence")`, i.e. only when *some* presence message arrives.

**Issue:** Pruning depends on traffic: in practice our own heartbeat publishes every 15 s and echoes back to our own subscription (`Slife/+` matches our name), so the detector ticks even on an empty mesh — that coupling is real but silent. If the heartbeat loop stalls (it is a separate task; an exception inside `_publish_presence` is caught and just logged, so the loop itself survives — but it can lag behind backpressure on the publish ack threads), or the broker transport hiccups, peer "timeout" detection silently stops too. There is no independent monotonic clock for liveness; the `heartbeat_timeout` (45 s) is only as good as the heartbeat echo that drives the prune.

**Impact:** Low in practice (heartbeat echo is a solid driver), but the design is implicit — a `timeout` event for a dead peer can be delayed by however long the mesh stays quiet beyond the heartbeat, contradicting the 45 s contract.

**Suggestion:** Run `_prune_stale_peers` on a `asyncio` periodic task whose period is a fraction of `heartbeat_timeout`, decoupled from presence traffic (keep the per-sighting prune as a fast-path).

### F7 — Backoff ladders have no jitter
**Where:** `agent/loop.py:920` (`_LLM_STREAM_RETRY_BASE_DELAY * attempts`) and `plugins/mcp_gateway/client.py:92` (`_CONNECT_RETRY_DELAY = 0.5`).

**Issue:** All retry sleeps are fixed delays. For a local app this is close to harmless today (one loop, ≤8 plugin children), but it is the standard thundering-herd smell: 8 plugin children restarting together on a broker/memdb outage would re-sync their retries. The A2A `_wait_for_connection` and the loop retry ladder share the same pattern.

**Impact:** Negligible now; a one-line note if scale changes.

**Suggestion:** Add a small jitter (`sleep(base * attempts * random.uniform(0.8, 1.2))` or `base * (attempt + random.random())`) — zero cost.

### F8 — deltas worth logging (non-issues, documented for awareness)
- `_MEMORY_SAVE` 10 s timeout raises `MemorySaveError` with uncertainty semantics ("may still be written") — correct, but the inbox freeze on persistent DB failure (service.py:1704) means a two-timeout session can wedge the inbox; worth a log-only escape if it recurs. (Observation, not a defect.)
- `wait_for`-vs-`asyncio.timeout` on Windows/Proactor: the codebase picked `asyncio.timeout` for `list_tools` for exactly the right reason (gateway/client.py:412-417). Audit future `wait_for` use on SSE/transport-y futures — `_exist_stack.aclose()` and MQTT thread-offloads are already the two other spots that touch threads/transport and are both bounded.
- `send_task`'s timeout covers only the reply wait, not the publish (publish is awaited before `wait_for` starts, client.py:320-325); with the 6 s publish-ack bound, the effective budget is `timeout + publish`. Minor, self-consistent.

---

## 4. What is well designed (worth preserving)

1. **Single enforcement point + documented native-timeout contract** (DESIGN.md:352-358). `_timeout`/`_async`/`_approve` live in the system prompt, are popped before dispatch, and map to a native `timeout` arg when one exists — no double timers, no schema bloat (≈3 params × 60 tools saved per request).
2. **Degrade, don't fail, on "already-delivered" timeouts.** A2A `send_task`, busy-subagent `subagent_send_task`, and mid-turn cut-in all convert a wait timeout into continuation (`[A2A-PUSH]`, async queueing with auto-delivery). The task store's "wait-timeout is no longer terminal" rule makes the degradation honest end-to-end (task_store.py:123-138).
3. **Cancellation ≠ timeout as a first-class distinction**, enforced with dedicated exceptions (`StreamStallError`, `AgentCancelled`), `_is_external_cancel()` for SDK cancel-scopes, and cleanup of leaked futures on every cancel path.
4. **Process-tree hygiene**: `start_new_session` + `kill_process_tree` on *every* exec tool, in both the timeout and the `CancelledError` paths, so a timed-out `yt-dlp`/`uv`/shell never orphans. The terminated-process ladder in `platform.py:211-269` (SIGTERM → 3 s → SIGKILL → 5 s → transport close) is a good template.
5. **Bounded retention everywhere a timeout can accumulate state** (queues, task store, pending maps, poll sets) — the surest sign the codebase has seen the "ran forever" class of bug and engineered it out.
6. **Subagent serial-worker preemption**: on `send_task` timeout the child is told to cancel the abandoned task because the worker is serial — this is the difference between "one timeout" and "every later task now times out too".
7. **Startup convergence is event-driven, not polled** (`_startup_settled`), so the TUI opens exactly when plugin spawns settle, never racing.

---

## 5. Configuration surface (what an operator can tune)

| Key | Default | Meaning |
|---|---|---|
| `agent.tool_timeout` | 120 s (0 = off) | fallback bound for tools w/o native timeout |
| `agent.heartbeat_interval` | 1800 s | autonomous idle turn cadence |
| `agent.max_iterations` | 30 | per-turn tool-loop cap (0 = unlimited) |
| `tools[].timeout` | per tool (shell 30 s) | per-tool native default; `enabled` also ad hoc |
| `a2a.heartbeat_interval` / `a2a.heartbeat_timeout` | 15 s / 45 s | mesh liveness |
| `a2a.task_timeout` | 120 s | sync send_task reply wait → auto-degrade |
| `subagent.task_timeout` | 120 s | send_task wait AND subagent `stream_timeout` |
| `subagent.max_subagents` | 5 | caps concurrent workers (also caps in-flight timeouts) |
| `plugins.required` | `[]` | failures abort startup instead of warning |

Not tunable today: subagent spawn-readiness (`30.0` hardcoded), plugin `PLUGIN_SPAWN_TIMEOUT` (60 s constant), media generation deadline (1200 s constant), MCP connect attempt budget (10 s constant), stream-stall (120 s constant), memory-save bound (10 s constant, hard timeout by design).

---

---

## 6. Verified inconsistencies & bugs (bug-hunt follow-up)

These were traced through the real call paths, not inferred. Each is categorized as a **bug** (behavior contradicts a documented contract), an **inconsistency** (two layers of the same system disagree), or a **latent hazard** (reachable, low probability). All anchors are exact file:line references.

### BUG B1 — Stateless sync-message degrade auto-pushes a misclassified "task completion," not a message reply

**Trace:**

1. `a2a_send_message` (stateless, `record=False`) runs the sync wait (`plugins/a2a/server.py:326-352` → `a2a/client.py:325`). The client's `send_task` docstring promises: *"a stateless message (`record=False`) degrades the same way — its late reply auto-pushes through the plugin's `_message_sends` map"* (`a2a/client.py:339-346`).
2. But `_message_sends` is populated **only** by `a2a_send_message_async` (`server.py:384`). The **sync** path never registers the `corr_id`.
3. On wait-timeout (`client.py:330-346`) the future is popped and the degrade text returned — and since the exchange is stateless there is no store record at all.
4. Late reply arrives → `_handle_result` (`client.py:803-832`): no waiter, no store record → stores in `_completed_tasks` + fires `_notify_task_result`.
5. Plugin `_on_task_result` (`server.py:171-183`): `corr_id in _message_sends` is **False** → falls into the task branch; `get_store().get()` → `None` → `peer = ""`, `kind = "task"`.
6. Harness `_a2a_poll_loop` (`service.py:2307-2337`): `kind != "message"` → `peer = cev.get("peer","") or corr_id or "peer"` → **the corr_id is rendered as the peer's name**, framed as *"Peer `<corr_id>` completed async task (ID: `<corr_id>`)"*.

**Result:** a stateless message's late reply surfaces to the agent as a bogus task completion from a "peer" that is really an id — exactly the completion lifecycle the recent commit (5671bef) removed for the async path, and it contradicts both tool descriptions ("stateless messages have no result to retrieve", `server.py:413-415`; stateless messages have no completion). Secondary: the reply also becomes retrievable via `a2a_get_task_result`, which the tool says is tasks-only (it landed in `_completed_tasks`, `client.py:820`).

**Status: FIXED.** `send_task` gained an `on_abandoned(corr_id)` hook, fired when the sync wait ends without a resolved result (timeout auto-degrade **and** cancelled waiter for `record=False`); `a2a_send_message` registers the peer via that hook (`_message_sends[corr_id] = agent_name`), mirroring `a2a_send_message_async`. The late reply now auto-pushes as a `kind="message"` reply with the peer preserved — verified end-to-end by `tests/test_a2a_plugin.py::test_send_message_sync_degrade_tracks_late_reply_as_message` and the client-level `tests/test_a2a_client.py::test_send_task_stateless_timeout_fires_on_abandoned`. (Original one-line suggestion was not viable: the sync path's `corr_id` is client-internal, so registration must ride the degrade itself, not precede the call — otherwise a successful reply would leak the map entry.)

### BUG B2 — `timeout: 0` on `execute_shell` is an **instant** timeout, while `_timeout: 0` is the tool default — three meanings for "0"

**Trace:** the system prompt (`slife/agent/templates/slife.j2:52`) declares the universal contract *"`0` = no timeout."* In the loop (`loop.py:1038-1157`):

- `_timeout: 0` on a native-timeout tool → `timeout_val = int(float(0)) = 0` → the `if timeout_val > 0` guard skips mapping (loop.py:1039-1042) → tool default (30 s). So `_timeout: 0` = **30 s**, not unlimited.
- A bare `timeout: 0` on `execute_shell` is schema-valid, passes through untouched (loop.py:1151-1157) → `ShellTool.execute` does `kwargs.get("timeout", 30)` = `0` → `asyncio.wait_for(_read_stdout_stderr(process), timeout=0)` → `wait_for` with `timeout <= 0` cancels immediately → `"Error: Command timed out after 0s"`, child tree killed on arrival.
- On tools *without* a native timeout, `_timeout: 0` correctly means unlimited (`effective_timeout = 0.0` → no wrap, loop.py:1166-1177).

Same tool, three outcomes for "0" depending on the spelling — the LLM trying to give a long command no limit gets it killed instantly. **Status: FIXED (together with B3 — same root cause).** The loop's native mapping (`loop.py:1039-1062`) now normalizes ≤0 in BOTH spellings: `_timeout ≤ 0` (or sub-second, truncating to 0) and a bare `timeout ≤ 0` are omitted so the tool's own default governs — the zero never reaches a `wait_for(..., timeout=0)`. `ShellTool` also coerces `timeout ≤ 0` to its own default as defense-in-depth (`tools/exec.py`), and its schema description states "≤0 = default (never instant)". Covered by `tests/test_loop.py::test_native_timeout_zero_never_reaches_the_tool` and `tests/test_tools_shell.py::test_timeout_zero_uses_default_not_instant_kill`.

### BUG B3 — `_timeout: 0` ("no timeout") is unexpressible on *any* native-timeout tool

Same guard as B2: for `execute_shell`, `a2a_send_task`, `a2a_send_message` (all declare a schema `timeout`), a `_timeout <= 0` is dropped and the tool's **own default** governs (30 s / 120 s / 120 s). The prompt contract promises "0 = no timeout" uniformly; the native-timeout contract (DESIGN.md:354-358) silently downgrades it. (The a2a tool descriptions are self-consistent — "≤0 = default" — so the plugin layer is honest; the *system-prompt* line is what lies.) Also: `int(float(...))` truncation means `_timeout: 0.5` becomes `0` → default, whereas a non-native tool honors the sub-second bound. **Status: FIXED (with B2).** Code: the ≤0 normalization at the native mapping (B2's fix) makes `_timeout: 0` and bare `timeout: 0` behave identically (tool default) on every native-timeout tool, including the sub-second truncation case. Contract: the system-prompt meta-parameter line (`slife/agent/templates/slife.j2:52`) is scoped — tools with their own `timeout` parameter treat ≤0 as their default (never instant); "0 = no timeout" now accurately applies to harness-wrapped tools.

### INCONSISTENCY I1 — A2A enablement probe: a **1 s, no-retry** TCP probe versus a 10 s connect and a 60 s spawn guard

`agent/service.py:750-751` gates the whole A2A subsystem on `probe_broker(host, port)` with **default `timeout=1.0`** (`a2a/broker.py:15`, single `wait_for`, no retry). The same subsystem that, once enabled, tolerates a 10 s MQTT connect (`a2a/mqtt.py:148`) and lives under a 60 s plugin spawn guard (plugins.py:65 — itself raised from 30 s *because a slow machine misfired*). A transient second of load against mosquitto → `a2a_broker_not_found`, A2A silently off for the entire session, never re-probed. This is the tightest timeout in the whole mesh, on exactly the slow path the rest of the codebase defends to 60 s. **Fix:** `probe_broker(timeout=3-5)` plus one retry, or let enablement ride the 10 s connect semantics.

### INCONSISTENCY I2 — Subagent readiness budget (hardcoded 30 s) still disagrees with the 60 s plugin tolerance, and is not configurable

`subagent/process.py:199` (`timeout=30.0` on `_ready.wait()`). The child (`subagent/headless.py:119-136`) boots by connecting to **every** parent plugin over HTTP *sequentially*, each via `MCPClient.connect`'s retry ladder (up to 20 × 10.5 s per the gateway constants, `plugins/mcp_gateway/client.py:81-93`). On a cold machine (the "8 children spawning" contention the 60 s guard documents, plugins.py:58-64) one slow plugin connect inside the child can blow the parent's 30 s budget — the failure text says "not ready within 30s." Meanwhile the *main* agent's identical work is allowed 60 s. (Design review #F1; the headless-connect mechanism confirms it's a live mismatch, just reached via HTTP connects rather than child-side plugin *spawn*.)

### INCONSISTENCY I3 — Two phases share one budget: subagent `stream_timeout` == `task_timeout`

Design review #F4. `agent/service.py:241-244`: a slow-but-live worker stream is killed at the same 120 s that bounds the *send* wait — unrelated durations, no independent knob.

### INCONSISTENCY I4 — Stale comment + inner/outer budget mismatch in the MCP gateway retry window

Design review #F3. `plugins/mcp_gateway/client.py:89-93` still says "bounded above by the 30 s spawn guard" (the guard is 60 s, plugins.py:65), and the per-attempt cap (10 s) × retry count (20) allows up to ~200 s worst case — materially larger than the outer 60 s that actually bounds it (service.py:610). The comment misleads tuning; the effective bound is the 60 s guard, not the math on line 93.

### LATENT HAZARD L1 — A2A task records are never reconciled with peer liveness

`_prune_stale_peers` removes a dead peer from the roster (`client.py:628-639`) but nothing sweeps its in-flight records: a peer that dies mid-task leaves its records `"pending"` **forever** (task_store.py has no TTL sweep; `record_cancel`/`record_error` need an explicit call). `a2a_list_tasks` shows ghost pending rows; `_message_sends`/`_poll_tasks` entries for dead peers expire only via the cap eviction. Memory is bounded (500-record store cap), so this is cosmetic — but the timeout philosophy ("degrade, don't fail") has no "peer died mid-flight ⇒ mark failed" rule. A heartbeat-timeout-driven sweep reconciling pending records against the roster would close it.

### LATENT HAZARD L2 — Drop-newest MQTT queues can silently shed *inbound tasks*, not just presence

`a2a/mqtt.py:430-450` (`QueueFull` → log-only) drops the **newest** message. For the presence route this is the right policy; for `Slife/<me>/tasks/inbox` (same mechanism — client.py:660-664 backpressure chain: inbox listener → merged queue (1000) → forward tasks → subscription queues) a sender that outpaces a slow `_handle_incoming_task`/inbox consumer gets its **newest task dropped with no per-task signal** — the sender only discovers via its own `task_timeout` auto-degrade. Documented policy, but asymmetric in effect: presence drops harmlessly, a task drop is silent loss. **Suggestion:** keep drop-newest for presence; for the task-inbox route, block (backpressure) or drain-to-oldest instead.

### L3 (benign, noted) — `wait_for` may return success a tick past the deadline

`a2a/client.py:325`: if the peer's result resolves in the same event-loop iteration the `wait_for` timer fires, the call succeeds despite exceeding `task_timeout`. Zero corruption — both outcomes (result returned / degrade message) are internally consistent — but it makes ultra-tight `task_timeout` values soft. Not a defect.

---

### What to fix first (effort / payoff)

1. **B1** — one-line register in `a2a_send_message`; restores the documented stateless-degrade contract. *(5 min)*
2. **B2/B3** — normalize `timeout ≤ 0` at the native mapping point + tighten the prompt line. *(10 min, one placement)*
3. **I2** — make the subagent readiness deadline configurable and default it ≥ `PLUGIN_SPAWN_TIMEOUT`. *(15 min)*
4. **I1** — widen `probe_broker` (3–5 s, one retry). *(5 min)*
5. **L1** — heartbeat-driven sweep of pending records vs roster. *(30 min)*
6. **I3, I4, L2** — documentation + one registry tweak; no code change strictly required.

*Bug-hunt status: B1/B2/B3 are genuine contract violations (edge-triggered: each needs a timed-out stateless send, a zero-timeout spelling, or a native `_timeout: 0`). No memory-, crash-, or data-loss-level bug was found; the remaining items are cross-layer inconsistencies and backpressure/documentation hazards.*

---

## 7. Design proposal — one timeout system instead of fifty

This section re-reviews the inventory collected above and proposes a consolidation. The goal is not fewer *call sites* (every bounded operation genuinely needs a bound) but fewer **distinct knobs, duplicates, and mechanisms** — so that the policy is visible in one place and the bug classes found in §6 (zero-ambiguity, wait_for-vs-timeout, drift) become structurally impossible.

### 7.1 The inventory says the problem is real

A full sweep of `slife/` finds **~50 timeout/deadline/poll constants** across 55+ files, by role:

| Role (informal) | Constants found | Values |
|---|---|---|
| One-operation budget | `tool_timeout`, `task_timeout` (a2a+subagent), `stream_timeout`, `_API_TIMEOUT`, `_EMBED_TIMEOUT`, pip install `120`, media `deadline_s` | 10–1200 s, **the digit `120` recurs in 8 unrelated meanings** |
| Inactivity/stall | `_LLM_STREAM_STALL_TIMEOUT` | 120 s (per-chunk reset) |
| Readiness handshake | `PLUGIN_SPAWN_TIMEOUT`, subagent `_ready` 30, `_CONNECT_ATTEMPT_TIMEOUT`, `probe_broker` 1, `PORT_SIGNAL_TIMEOUT`, `_TUNNEL_SETTLE_TIMEOUT`, OAuth `_POLL_TIMEOUT` | 1–60 s — the same sub-subsystem is probed at 1 s and allowed 60 s |
| Liveness | `heartbeat_interval`/`heartbeat_timeout` (15/45), `HEARTBEAT_INTERVAL` 1800 | ratio 3× |
| Grace/escalation | `terminate` 3→5, shutdown 3.0, `_CLEANUP_TIMEOUT` 2, warmup delay 5 | 2–5 s |
| Transport | httpx 300/30, aiohttp 120/180, DashScope 180 | per HTTP stack |
| Retention → **count-bounded, not time-bounded** | `_MAX_QUEUED`, `_MAX_CANCELLED`, `_MAX_TASK_RECORDS`, `_MAX_QUEUE_SIZE`, `_MAX_TRACKED_SESSIONS`, ×store caps | 64–5000 |

Worse than the count is the duplication — the same constant is **re-declared in sibling modules**, which is definitionally drift:

- `_NOTIFY_TIMEOUT = 5.0` — **×3**: `mcp/host_server.py:89`, `plugins/job_coding/server.py:61`, `plugins/mcp_gateway/server.py:208`
- `_CLEANUP_TIMEOUT = 2.0` — **×2**: `plugins/mcp_gateway/client.py:85`, `plugins/mcp_gateway/connection.py:65`
- `_MAX_TRACKED_SESSIONS = 64` — **×3**: same three modules as `_NOTIFY_TIMEOUT`
- `120` as a bare literal or constant — 8+ unrelated meanings

### 7.2 Five failure modes (why it breeds bugs)

1. **No taxonomy.** Every module invents the timeout it needs, named and spelled however it likes. Two modules doing the *same role* end up with different names, different values, and different comments — and nothing ties them together, so they drift (§6 I1, I2, I4).
2. **Five coexisting timer mechanisms, each with distinct semantics**: `asyncio.wait_for` (cancels the inner task — hangs on Windows/Proactor for stuck SSE), `asyncio.timeout` (deadline raise, safe — the codebase learned *which* and applied it in exactly one place), manual `deadline = monotonic()+X; while` loops (re-implemented in ~5 places), `threading.Event.wait(t)` / `subprocess.run(timeout=)` (block the thread/process), HTTP-stack timeouts. The choice between them is ad hoc, so the subtle difference (does this timeout kill the thing or just raise past it?) is decided per caller.
3. **Zero/≤0 ambiguity** — the B2/B3 class. The loop's one mapping point is now normalized, but every *other* tool that accepts a `timeout` still picks its own convention (a2a: ≤0=default; shell: 30s default; none allow true "unlimited").
4. **Degrade-vs-fail-vs-silent is emergent, not declared.** Whether an expired wait auto-degrades (a2a send), fails loudly (shell, memdb save), or stays silent (tunnel settle, publish-ack) is whichever exception path the caller happened to write. There is no one place that says "this wait's contract is: degrade." The three bugs in §6 all came from this being implicit.
5. **The "no total" rule is irregular.** Per-step bounding is the philosophy, but its only exception (subagent `stream_timeout`) is chosen by *who is watching* (autonomous ≠ interactive) rather than by a role, so unattended turns (heartbeat, scheduled, A2A-inbound) run unbounded (§6 F2).

### 7.3 The proposal

**Move 1 — one timer primitive.** Replace all five mechanisms with one helper (keep `asyncio.timeout` as the engine — deadline-based, raises without waiting for inner cancellation, the Windows/Proactor-safe choice already validated in `mcp_gateway/client.py:412-417`):

```python
# slife/timeouts.py — the ONLY async timer in slife
@dataclass(frozen=True)
class Bounds:
    kind: str                    # "work" | "stall" | "ready" | "grace"
    budget: float                # seconds; 0 = no bound
    on_exceed: str               # "raise" | "degrade" | "silent"
    reset_on_activity: bool = False   # True = stall/watchdog semantics

class DeadlineError(Exception):
    def __init__(self, *, kind, owner, budget, ctx=""): ...

async def bounded(awaitable, bounds: Bounds, *, owner: str = ""): ...
```

`DeadlineError` carries `kind/owner/budget/ctx` as structured fields, so every error path classifies programmatically (retry? degrade? surface?) instead of string-matching; the log lines (`tool_timeout name=%s timeout=%d`) keep their exact current shape via `owner`/`ctx`.

**Move 2 — six roles, one config surface.** Every timeout maps to a role, and config owns **one `timeouts` section keyed by role**, with *primary* knobs only; everything else derives from them:

```json5
timeouts: {
  work: { default: 120, tool: 120, task: 120, stall: 120, stream: 0 },  // stream 0 = off
  turn: { max: 0 },            // NEW — whole-turn wall; off for interactive, on for autonomous
  ready: { connect: 10, spawn: 60, notify: 5, probe: 3 },
  grace: { gentle: 3, force: 5 },
  transport: { connect: 30, read: 300, total: 600 },   // delegated to httpx/aiohttp
}
```

- `work.task` replaces `a2a.task_timeout` **and** `subagent.task_timeout` (they are the same role; one knob ends §6 I3).
- `work.stall` replaces `_LLM_STREAM_STALL_TIMEOUT`; `work.stream` (`0` on the main agent, derived for workers) replaces the subagent-only special case.
- `ready.*` replaces spawn/connect/probe/notify/port-signal; `ready.probe` (3 s) retires the 1 s silent-disable probe (§6 I1).
- `turn.max` fills the "no total" gap (§6 F2) with the *same* mechanism instead of an exception.
- Retention stays **count-bounded** (queues, task store, `_MAX_*`) — that is memory safety, orthogonal to time, and the current caps are fine.

**Invariants enforced at config load (one `assert` block, not comments):**
- `heartbeat_timeout ≥ 3 × heartbeat_interval` (the documented intent becomes structural).
- `subagent_ready == ready.spawn` (kills §6 I2: no more 30 s child under a 60 s parent).
- `ready.notify ≤ ready.connect ≤ ready.spawn` — nested budgets must nest.
- `timeouts.work.stall ≤ timeouts.work.stream` when `stream > 0` (a total cap below the stall watchdog would be unreachable noise).

**Move 3 — one retry/backoff ladder.** A single `RetryPolicy(attempts, base, cap, multiplier, jitter, retryable)` drives the LLM-stream retries (loop.py), MCP connect retries, the broker probe, sharefile retries, and the existing `connection.py` 5→60 exponential reconnect (which already exists — lift and share it). Kills the bespoke `for n in range(1, _MAX_RETRIES+1): await sleep(_RETRY_DELAY*n)` shapes and adds the missing jitter (§6 F7) in one place.

**Move 4 — outcome class declared at the call site.** `Bounds.on_exceed` is the first-class answer to "what happens when this fires": a2a sync send → `degrade` (auto-async, the B1 path becomes a *declared* contract, not a docstring promise); shell/pip → `raise`; tunnel settle / publish-ack → `silent`. A reviewer sees each wait's contract without tracing the call stack, and the §6 bug classes lose their breeding ground.

**Move 5 — delete the duplicates.** `_NOTIFY_TIMEOUT` (×3), `_CLEANUP_TIMEOUT` (×2), `_MAX_TRACKED_SESSIONS` (×3), and the 8 unrelated `120`s collapse into the single-source `timeouts`/`retention` modules. The zero-semantics rule ("`0` = no bound for loop-wrapped tools; ≤0 = tool default for native-timeout tools") is written **once**, next to `ready/probe`-style defaults, as the documented contract §6 B2/B3 now implements in one spot.

### 7.4 What this buys (numbers)

| Metric | Today | After |
|---|---|---|
| Distinct time/poll constants | ~50 | ~10 primaries + ~6 derivation rules |
| Duplicated declarations (`_NOTIFY`, `_CLEANUP`, `_MAX_TRACKED`) | 3×, 2×, 3× | 0 |
| Async timer mechanisms | 5 (`wait_for`, `asyncio.timeout`, manual deadline loops, thread/`subprocess.run` blocking, HTTP stacks) | 2 (`bounded` + delegated HTTP-stack `transport.*`) |
| Places the "what happens on expiry" contract lives | 0 (emergent per caller) | 1 (`Bounds.on_exceed`) |
| Whole-turn bound | none (implicit exception for subagents) | `turn.max`, same mechanism |

**Deliberately not changed**: count-bounded retention; HTTP-stack transport timeouts (delegation is correct — the stacks do connect/read/write/pool better than a reimplementation); the interactive-agent no-total default (it stays, now *configurable* rather than accidental).

### 7.5 Migration (each stage test-visible)

1. Introduce `timeouts.py` with `Bounds`/`bounded`/`RetryPolicy`/`invariant check`, wired to config **alongside** the old constants — a pure refactor, zero behavior delta (the §6 fixes stay).
2. Route the five hot paths through `bounded`: loop tool execution + stream stall (loop.py), a2a `send_task` (+ the `on_abandoned` hook), gateway connect/list_tools, plugin spawn guard, subagent ready — asserting the invariants as each lands.
3. Fold the remaining modules one at a time (notify/cleanup/max-tracked → shared module; probe/oauth/settle → `ready.*`; media/wechat/sharefile keep their `transport.*`/named overrides).
4. Rename the config surface (`agent.tool_timeout` → `timeouts.work.tool`, `a2a.task_timeout` → `timeouts.work.task`, …) with a migration alias so existing json5 keeps working, then drop the aliases.

Step 3 is where the duplication literally disappears; steps 1–2 are where the bug classes retire. The proposal leaves the recent B1/B2/B3 fixes intact — it *contains* them (the mapping point, the degrade hook, the ≤0 rule) rather than reworking them.

### 7.6 This is a fragility fix, not a cleanup

The deeper framing: **every timeout in the codebase is a pre-declared point of failure.** It says "this subsystem may stop here" — and a wrong value (too tight on a slow machine, too loose to ever fire, zero meaning the opposite of intended) converts a live-but-slow system into a crash, a hang, or a silent drop. §6's bugs were exactly that: not logic errors in happy paths, but *values and semantics* (0 = instant kill; 1 s probe gating a 60 s subsystem; 120 s budget reused for two different phases). So the design must treat timeout values as **invariants with telemetry, not tuning parameters**. Three concrete mechanisms:

1. **Fail at load, not at runtime.** The `timeouts` section is validated once at startup: non-numeric → error; `0` where the role forbids it → error; nesting violated (`notify > connect`, `heartbeat_timeout < 3×heartbeat_interval`, stall > stream) → error. A wrong value in a config file becomes a loud, early, *named* failure — never a mystery hang an hour later. (Today the same wrongness lives in 50 scattered unlinked constants, so nothing can catch it.)
2. **Expiry telemetry, including the silent ones.** Every `DeadlineError` carries `kind/owner/budget` and feeds one counter/log stream (`timeout_fired kind=ready._spawn owner=subagent budget=60 ctx=...`). Three consequences: (a) "which timeouts actually fire in production" is a query, not folklore; (b) the `silent` class (tunnel settle, publish-ack) becomes visible — today a silent expiry is indistinguishable from a non-event; (c) a budget that fires often for one owner is a *config bug detector*: emit a suggestion line (`timeouts.ready.spawn=60 fired 8× this session — consider raising; a child's cold boot is trending past it`) so "不合适的值" self-reports instead of manifesting as a rare crash on someone else's machine. This directly kills the §6 F1 class (30 s under a 60 s reality) as a *repeatable diagnosis*, not one-off luck.
3. **A review gate for new timeouts.** The invariant block doubles as a lint/test: any new hardcoded duration literal in `slife/` fails review unless it is (a) one of the role knobs, (b) a documented per-name override (media 1200, oauth 300), or (c) a retention count. Proliferation stops being a drift you notice too late; it becomes a constraint enforced from day one — the same way the invariant asserts catch it for config.

Together these turn the system's historic fragility epicenter into its most observable surface: values are validated once, expiries are metered, and the "no new magic timeout" rule keeps the §7 taxonomy honest forever.

*Reviewed from `main` @ 5671bef · 2026-09-12. §1–5 design review (F1–F8, no high-severity design defects); §6 bug-hunt verified three contract violations (B1–B3, now fixed) and four inconsistencies (I1–I4); §7 proposes consolidating the ~50 scattered timeouts into one role-based `timeouts` system — 5 moves, 5-stage migration, and (7.6) treats every timeout as a pre-declared failure point: validated at load, metered at runtime, gated at review.*