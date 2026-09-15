# Timeouts — the One Registry Model

> Developer-owned design doc.  Regular users never touch this — see how the
> values are owned below.  Companion section: DESIGN.md → *Timeout
> Architecture* (Part 4) and *Config & Credentials* (Part 8).

## Why this exists

Slife's timeout design was the fragility epicenter: ~50 scattered constants
and literals, the same semantic with different values (probe timeouts 5/5/15,
`_NOTIFY_TIMEOUT = 5.0` in three servers, kill ladders 3/5 vs 1/2 vs 2.0),
each knob's default duplicated in 3–4 places, dead config keys parsed by
nothing, and two genuinely unbounded waits (SQLite query execution, the config
file lock).  Every fix landed on one symptom, which produced drift.

This doc freezes the **model** and the **mechanism** together so a timeout is
never a local invention again.

## The model — five rules

1. **owner-of-await.**  Every await that can block has a bound, owned by the
   layer that awaits it.  A callee never sets a total for its caller.
2. **The ONLY sanctioned "total" is the tool-call budget.**  The timeout the
   agent injects through the prompt meta-parameters (`_timeout` /
   `tool_timeout` → `work.tool_budget`; `work.task_budget` for subagent
   tasks) is the one total deadline in the system.  **No other totals** — no
   turn deadline (`turn.max` was explicitly rejected), no chain-decreasing
   budgets, no endpoint-wide totals.  Long-running-but-live work is bounded
   by *inactivity* watchdogs (stall timers that reset on progress), never by
   a wall-clock cap.
3. **Slots are contracts.**  Some values must not be "tuned": the A2A
   requester retry profile mirrors the `a2a-over-mqtt` SDK's wire contract;
   the MCP gateway transport keeps `read`/`write` delegated (the loop's tool
   budget owns the read bound).  Faithfully replicate, don't "improve".
4. **One semantic → one value.**  Reuse the existing registry key for a
   semantic before adding a new one; a new key needs a comment explaining the
   semantic no existing key covers.
5. **No global defaults.**  `socket.setdefaulttimeout`-style process-wide
   poisons are banned — a global default would silently change every
   third-party socket (paho/aiomqtt, ngrok, httpx2, the SDKs).

## The registry — `slife/timeouts.py` (the module IS the registry)

All values ARE CODE: the typed dataclass defaults in **`slife/timeouts.py`**
are the single source of truth — there is no external JSON5/config file and
no second seat for the same value.  Developers own the values by editing the
module (default + `// why` comment).  The registry has **no configuration
consumers** (no runtime tool, user seed or installer reads it), so a data
file would only be ceremony over a commented, type-checked Python literal —
Python wins.  **A structurally invalid edit fails loudly at import** with a
named `TimeoutConfigError` (via `validate()`); the values themselves are
checked by pyright, not ad-hoc parsing.

| Role | Holds | Example keys |
|---|---|---|
| `work` | per-tool-call execution budgets (the tool chain) | `tool_budget`, `task_budget`, `stall`, `shell`, `pip_install`, `save_memory` |
| `ready` | startup / spawn / connect / liveness windows | `plugin_start`, `spawn`, `connect_attempt`, `connect_startup`, `signal`, `stderr_line`, `relisten`, `probe_broker`, `probe_endpoint`, `tunnel_*`, `list_tools`, `watchdog_backoff_*` |
| `grace` | teardown / kill escalation ladder | `gentle`, `force`, `cleanup`, `shutdown`, `tunnel_kill` |
| `transport` | HTTP / wire client phases | `connect`, `pool`, `read`(null=delegated), `write`(null), `oauth`, `poll_oauth`, `embed`, `embed_api`, `media_*`, `wechat_poll`, `url_download`, `qr_deadline` |
| `stream` | LLM stream retry ladder | `retries`, `retry_base_delay` |
| `storage` | bounded lock / busy waits (DB reads are unbounded by design) | `sqlite_busy`, `filelock` |
| `deliver` | A2A / mesh delivery windows | `mqtt_connect`, `reply_first`, `keepalive`, `retry_delay` |

**Load-time invariants** (`slife/timeouts.py:validate`) — violating any of
these fails startup with a named error:

1. every value is finite and ≥ 0 (`transport.read/write` may be `null`;
   `stream.retries` must be an int);
2. `grace.gentle ≤ grace.force` (the kill ladder escalates);
3. `ready.relisten ≤ ready.connect_attempt ≤ ready.spawn` (nested budgets nest);
4. `ready.connect_startup ≥ ready.spawn`;
5. `work.stall > 0`.

## The convention — how consumers read values

Consumers do **call-time lookups**:

```python
import slife.timeouts as _timeouts
...
async with asyncio.timeout(_timeouts.timeouts.ready.connect_attempt):
    ...
```

- NEVER import-capture into a module constant (`_FOO = timeouts.x.y`), NEVER
  use a registry value as a def-time default argument.  Call-time reads let
  tests `monkeypatch.setattr("slife.timeouts.timeouts.<role>.<key>", N)`.
- Constructor defaults are `None` sentinels resolved to the registry in the
  body (e.g. `AgentLoop.tool_timeout`, `send_task`), with one exception: the
  MCP child wrapper's `create_client` bare default is the registry's
  `ready.signal` (=== 60, deliberately different from `work.tool_budget`).
- `slife/timeouts.py` imports nothing from `slife.*` — `config.py` imports
  it; a cycle would break both.

## Tool-execution precedence — one value per tool call

Every tool call has exactly **one effective timeout `T`**, decided once:

1. **The agent injects a positive value** (the `_timeout` meta-parameter or the
   tool's `timeout` argument) → `T` = agent value.  **The agent's timeout
   overrides ALL system defaults** — this is the rule.  `0` / negative /
   missing are NOT overrides; they mean "use the default" and normalize to
   `None` on the tool side — and never "no timeout": there is no unbounded
   escape for in-turn tool calls; a call that must run long gets a large
   positive value instead.  (The one exception is a backgrounded `_async: true`
   call with no injected timeout — it runs bare, below.)
2. **The agent omits** → the tool-execution default:
   - a tool **with** a native `timeout` parameter keeps its own registry value
     (`execute_shell` → `work.shell`, `subagent_send_task` →
     `work.task_budget`) — the value stays in the parameter and the tool is
     the single enforcer;
   - a tool **without** a native `timeout` parameter gets the loop's
     `work.tool_budget` via `asyncio.wait_for(T)`.

Enforcement is exactly **one timer per call** (no double timer, no
divergent deadlines):
- native-`timeout` tools are never wrapped by the loop — `T` lands in the
  parameter and the tool enforces it (`subagent_send_task` → `send_task` →
  `wait_for` on the worker RPC);
- non-native tools get the loop's `wait_for(T)` and never see the value (they
  have no parameter to hold it).

**Backgrounded calls (`_async: true`) are the exception to rule 2's
fallback.**  A background call with a positive injected `_timeout` follows the
same mapping — native `timeout` → the arg (tool enforces), non-native → the
loop's `wait_for(T)`.  Without an injected timeout the call is scheduled BARE:
the chain default (`work.tool_budget`) is deliberately NOT applied to
background work — async exists to escape the in-turn budget, so its agent-loop
bound never governs background execution.  A tool offered for async therefore
carries its own bound when it needs one (a native `timeout` parameter or an
internal deadline — the media adapters' poll deadline).  `run_python_script`
currently has neither: its bare async usage is unbounded by design, and the
agent caps it with a positive `_timeout` or cancels via `cancel_async`.

`subagent_send_task` declares a native `timeout` parameter **because of this
rule**: an injected value flows to the worker and overrides `work.task_budget`
for that one call; omission resolves to `work.task_budget` at call time.

**A tool's own run-timeout is a BACKSTOP, never an operative bound for a call
that carries an effective `T`.**  Because the agent's value overrides the
tool's, the tool-side defaults must be designed GENEROUS — a tight native
default would preempt the injected value before it acts (the exact subagent
double-timer failure this model removes: an inner 120s timer clamped an
injected 300s).  When in doubt, prefer a generous backstop for the tool-chain
budgets and let the injected value (or a `_timeout`) be the precise bound.

**If a native tool has an internal run-timeout, it MUST expose it as a
``timeout`` parameter** (default = its registry value).  Only an exposed
parameter lets the agent's override reach the tool — a hidden inner timer
would silently clamp the injected value (the same double-timer).  Tools that
have no internal deadline have nothing to expose (`install_python_package`
and `generate_video` exist precisely because they DO).

**DB reads are never time-bounded.**  A read may take as long as it needs —
a slow query is not a failure.  There is no read timeout on SQLite reads
(`storage.query_cap` is gone); locking waits stay bounded by
`storage.sqlite_busy` / `storage.filelock`, and the *calling tool call* still
carries its own deadline at the loop.

## What intentionally stays local (not timeouts)

These are **not** timeout budgets and live on outside the registry (the AST
gate allowlists them — see below):

- **Cadences**: poll/keepalive/health *intervals* (`POLL_INTERVAL`,
  `MISS_GRACE`, `HEARTBEAT_INTERVAL`, `_HEALTH_CHECK_INTERVAL`,
  `_TYPING_MAX_LIFETIME`, `_TUNNEL_PROBE_INTERVAL`, `_QR_POLL_INTERVAL`, …).
- **Counts / profiles**: retry *attempts* (mcp connect `_CONNECT_RETRY_ATTEMPTS`,
  A2A `_MAX_ATTEMPTS`, watchdog `_WATCHDOG_MAX_RESTARTS`), backoff profile
  shapes (`_BACKOFF_BASE_S = [1, 2, 4]` is the SDK's list, kept verbatim),
  retention caps (`_MAX_TRACKED_SESSIONS`, `_MAX_CANCELLED`, …), sanity caps
  (`MAX_REINDEX_NO_PROGRESS`, `MAX_WAIT_MINUTES`).
- **`# noqa-timeout` sites**: deliberate sync subprocess probes (`health.py`
  version checks), desktop notifications (`platform.py` best-effort,
  never-fail), one-off dependency bring-up (mcp connection `npm`/`uvx`).
- **The LLM SDK clients carry no timeout of ours by design** — the loop's
  per-chunk stall watchdog (`work.stall`) and the tool budget own that bound;
  the vendor 600 s default is never reached.

## The gates (tests that keep this evergreen)

- **`tests/test_no_magic_timeouts.py`** — an AST scanner over `slife/**`
  that fails on any numeric `timeout=` literal, `wait_for`/`asyncio.timeout`
  numeric argument, module-level `_*_TIMEOUT`-style float constant, or
  `deadline = … + N` literal — unless the symbol is allowlisted or the site
  carries `# noqa-timeout`.  **A new hardcoded timeout fails CI the moment it
  lands**, which is what kills the "fix one, drift another" loop.
- **Consumed-keys companion** — every declared registry field must be
  referenced by ≥ 1 site (`timeouts.<role>.<key>`); a dead key (the old
  `a2a.task_timeout`/`heartbeat_*` pattern) fails CI.

## How to change a value

1. Edit the dataclass default in `slife/timeouts.py` (with a comment
   explaining the value where it is load-bearing).
2. Keep the invariants; run the gates.
3. If you need a timeout no key covers: it belongs to a role first — reuse an
   existing key unless the semantic is genuinely new, then add the field to
   the matching dataclass role.
4. Never reach for a total deadline to fix a hang: the fix is a stall
   watchdog (reset on progress) or a per-owner bound — not a wall clock.

## What was deliberately NOT done (rejected approaches)

Reference: the deleted project plan `timeout-plan.md` (git `5ec3264`)
proposed a deeper mechanism.  Rejected as too aggressive:

- `bounded()` primitive / `DeadlineError` replacing the existing
  `wait_for`-vs-`asyncio.timeout` split everywhere — the split is
  principled (kill-on-timeout vs handle-in-body); the fragility was the
  *values*, which the registry fixes.
- `RetryPolicy` unifying the 9 hand-rolled backoff ladders — each is
  domain-tuned (A2A mirrors the SDK contract; MCP connect distinguishes
  retryable classes; the plugin watchdog has its own counters).
- `timeout_fired` telemetry / high-frequency detector.
- `turn.max` (a non-interactive turn total) — users explicitly ruled out any
  total besides the tool-call budget.

## What this change fixed

Bugs: `storage.sqlite_busy` bounds the connect busy-wait; the config
`filelock` no longer blocks forever (`storage.filelock`, raises
`ConfigLockTimeout`).

Inconsistencies: probe trio 5/5/15 → one `ready.probe_endpoint` (5); the
triplicated `_NOTIFY_TIMEOUT = 5.0` → `ready.notify`, retired with the
session-based notifier and replaced by `ready.relisten` (the backoff before
a dropped `subscriptions/listen` stream is reopened); kill ladders
3/5 vs 1/2 vs 2.0 → one `grace.gentle/force`; oauth `30.0`×3, wechat
`120`×2, media `180/300` → named transport keys; subagent ready `30`
→ `ready.spawn` (60, aligned with the plugin hang-guard).

Dead weight: the dead `_PROBE_TIMEOUT = 15.0` in mcp_gateway/embeddings was
deleted; the `a2a.task_timeout` / `a2a.heartbeat_*` / `subagent.task_timeout`
user-config keys were removed (never parsed; `task_timeout` is
developer-owned via the registry).  `agent.tool_timeout` is no longer read
from user config — the tool window is a developer value now.