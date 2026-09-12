# Implementation Plan — One Timeout System

> **Status:** frozen plan for the next execution session. Saved at repo root as `timeout-plan.md` (working tree, uncommitted — commit as Step 0).
> **Spec source:** REVIEW.md §7 (5 moves) and §7.6 (fragility mechanisms) — approved framing, operationalized here.

## Context

The review (§6) proved timeouts are the fragility epicenter, not a tuning detail: ~50 scattered constants, `_NOTIFY_TIMEOUT` ×3, `_CLEANUP_TIMEOUT` ×2, `_MAX_TRACKED_SESSIONS` ×3, the digit `120` in 8 unrelated meanings, and 5 coexisting timer mechanisms with different cancellation semantics. Every bug found (B1/B2/B3) and every inconsistency (I1–I4) originated in timeout **values and semantics** — a wrong value turns a live-but-slow system into a crash, a hang, or a silent drop.

**Outcome:** one `timeouts` config section keyed by role; one `bounded()` timer primitive; one `RetryPolicy`; load-time invariant checks; expiry telemetry (`timeout_fired`); and a review gate that forbids new magic duration literals. The B1/B2/B3 fixes already landed are *contained* by this design, not reworked.

## End-state design (the spec to implement)

| Role | Knob (config `timeouts.<role>.<key>`) | Today's scattered proxies |
|---|---|---|
| work | `work.default/tool/task/stall/stream` 120/120/120/120/0 | `tool_timeout`, a2a+subagent `task_timeout`, `stream_timeout`, `_LLM_STREAM_STALL_TIMEOUT`, pip `120`, `_API_TIMEOUT`, `_EMBED_TIMEOUT` |
| turn | `turn.max` 0 (off interactive; on for heartbeat/scheduled/A2A-inbound) | none (fills the "no total" gap) |
| ready | `ready.connect/spawn/notify/probe/settle` 10/60/5/3/20 | `_CONNECT_ATTEMPT_TIMEOUT`, `PLUGIN_SPAWN_TIMEOUT`, `_NOTIFY_TIMEOUT`, probe 1 s, `PORT_SIGNAL_TIMEOUT`, `_TUNNEL_SETTLE_TIMEOUT` |
| grace | `grace.gentle/force/cleanup` 3/5/2 | `terminate` ladder, UI shutdown 3.0, `_CLEANUP_TIMEOUT` ×2 |
| transport | `transport.connect/read/total` 30/300/600 | httpx/aiohttp per-client Timeout() |
| retention | `retention.*` (counts) | `_MAX_QUEUED` ×2, `_MAX_TASK_RECORDS`, `_MAX_CANCELLED`, `_MAX_QUEUE_SIZE`, `_MAX_TRACKED_SESSIONS` ×3 |

**Named overrides stay** (documented per-name, outside the core section): media `deadline_s` 1200, oauth `_POLL_TIMEOUT` 300, wechat aiohttp totals, connection health ping 5 / interval 30.

**Invariants (written once in `timeouts.py`, asserted at config load, unit-tested):**
1. `heartbeat_timeout ≥ 3 × heartbeat_interval`
2. `subagent ready == ready.spawn` (kills I2: 30 s child under 60 s parent)
3. `ready.notify ≤ ready.connect ≤ ready.spawn` (nested budgets nest)
4. `0 < stall ≤ stream` when `stream > 0`
5. `grace.gentle ≤ grace.force`
6. every budget finite, non-negative; `0` semantics per role

## New module — `slife/timeouts.py` (Stage 0)

```python
@dataclass(frozen=True)
class Bounds:
    kind: str              # "work"|"stall"|"ready"|"grace"
    budget: float          # seconds; 0 = no bound (meaning per role)
    on_exceed: str         # "raise"|"degrade"|"silent"
    reset_on_activity: bool = False   # stall/watchdog semantics

class DeadlineError(Exception):
    def __init__(self, *, kind, owner, budget, ctx=""): ...   # structured, no string-parsing downstream

async def bounded(awaitable, bounds: Bounds, *, owner="", ctx=""): ...
    # engine: asyncio.timeout (deadline-raise, Windows/Proactor-safe — the one
    # mechanism the codebase validated in mcp_gateway/client.py:412-417).
    # NEVER asyncio.wait_for on cancellation-hostile transports.
    # On expiry: raise DeadlineError / invoke declared degrade hook / return silently.

@dataclass(frozen=True)
class RetryPolicy:
    attempts: int; base: float; cap: float; multiplier: float; jitter: bool
    retryable: Callable[[BaseException], bool]

def validate_timeouts(cfg) -> list[str]:   # invariant checks → named errors at load

RETENTION = {...}   # single source for all count caps
```

- Reuse the exponential-backoff constants already in `plugins/mcp_gateway/connection.py:53-55` as `RetryPolicy` defaults.
- Logging shape: `timeout_fired kind=<role.key> owner=<ctx> budget=<s>` via existing `slife/logfmt.py` conventions.

## Stage-by-stage execution (each stage = independent commit + green tests)

### Stage 0 — primitives (pure add, zero behavior delta)
Add `slife/timeouts.py` + `tests/test_timeouts.py`:
- `bounded` raise/degrade/silent; DeadlineError field access; `reset_on_activity` stall semantics
- `RetryPolicy`: retryable classification, backoff math, jitter bounds
- `validate_timeouts` invariants incl. negative cases

### Stage 1 — config surface + rehydration (`slife/config.py`)
**Facts that shape this stage (confirmed):** `Config` is a **flat** `@dataclass` (config.py:347) — `tool_timeout`/`heartbeat_interval`/`max_iterations` are flat fields (355-367), accessed as `config.tool_timeout` everywhere (service.py:250, 952, 968; plugins.py:506). Single parse site `Config.from_json5` (822); sub-sections plug in via `from_dict(raw.get(top_key))` inside it (a2a 905, embeddings 888, wechat 897, subagent dict 915). `from_dict` (430) / `to_dict` (399) carry subagent `SLIFE_CONFIG` inheritance. Tests build `Config(...)` directly (conftest `sample_config` 161-171) → new fields need dataclass defaults.

- Add `timeouts: TimeoutsConfig | None = None` field; `__post_init__` builds it from the explicit `timeouts` section merged with the legacy flat fields as defaults; `__post_init__` runs `validate_timeouts` → **fail fast with named errors**.
- **Rehydration, not aliases:** after merging, overwrite the legacy flat fields (`tool_timeout`, `heartbeat_interval`, `a2a.task_timeout`/`heartbeat_*`, `subagent.task_timeout`) from `timeouts.*` — so every existing `config.tool_timeout` reader picks up the consolidated value with **zero consumer changes**. Explicit `timeouts` section wins; absence = today's values, unchanged.
- `to_dict`/`from_dict` (399-428) carry `timeouts` so subagent `SLIFE_CONFIG` inheritance stays consistent.
- Tests: parse; precedence (explicit section beats legacy); every invariant violation raises a NAMED error; configs with only legacy keys load byte-identical behavior.

### Stage 2 — route hot paths through `bounded`/`RetryPolicy` (behavior-neutral)
- **loop.py**: tool wrap `wait_for(coro, tool_timeout)` (1154/1192) → `bounded(..., Bounds("work", budget=tool_timeout, on_exceed="raise"))` keeping the exact result-string shape (`tool_timeout name=%s timeout=%d`, loop.py:1205-1208); stall watchdog `asyncio.timeout(stall)` (762-774) → `bounded(Bounds("stall", ..., reset_on_activity=True))` — note `stream_stall_timeout` is **never passed** today (default 120 from loop.py:54), so `work.stall` becomes the ctor default; stream retry ladder (844-920) → `RetryPolicy` preserving the both-http-generations classification (`_is_retryable_stream_error`, 74-125).
- **a2a/client.py** `send_task` (273-335): `wait_for(future, timeout)` → `bounded(Bounds("work", task_timeout, "degrade"))`; the `on_abandoned` hook and auto-degrade text stay byte-identical.
- **mcp_gateway/client.py**: connect attempts (206-291) → `RetryPolicy` (absorbing the `_is_external_cancel` distinction) + `bounded(Bounds("ready", connect, "raise"))`; `list_tools` `min(self._tool_timeout, 20.0)` (410) → `bounded(Bounds("ready", min(work.tool, ready.refresh), ...))` — the 20 derives from `ready`, not a fresh literal. The ctor default `tool_timeout=60.0` (161) is dead in the service path (process.py:255-258 passes `self.config.tool_timeout`) but **live at `job_coding/runner.py:325` (`admin = MCPClient()`)** — align that construction to `timeouts.work.tool`.
- **service.py**: `PLUGIN_SPAWN_TIMEOUT` guard (610) → `bounded(Bounds("ready", spawn, "silent"))` (spawn continues in background — same as today, now declared); tunnel settle (874-894) → `ready.settle`; **both** memdb save bounds — `__memory_save_turn` (1668-1674) AND `advance_context_start` (1903-1907) — 10 s → named `work` override.
- **subagent/process.py:199**: hardcoded `30.0` ready → `ready.spawn` (I2 fixed by construction); `_WATCHDOG_STABLE_UPTIME` in plugins.py:74 stays derived from the same source (ready.spawn).
- Tests: full loop/a2a/gateway/subagent suites re-run unchanged; assert timeout message strings identical; zero-semantics regressions (B2/B3) keep passing.

### Stage 3 — collapse duplicates to single sources
- `_NOTIFY_TIMEOUT = 5.0` (mcp/host_server.py:89, job_coding/server.py:61, mcp_gateway/server.py:208 — all identical `wait_for(..., timeout=_NOTIFY_TIMEOUT)`) → `timeouts.ready.notify`.
- `_CLEANUP_TIMEOUT = 2.0` (mcp_gateway/client.py:85, connection.py:65) → `timeouts.grace.cleanup`.
- `_MAX_TRACKED_SESSIONS = 64` (×3) and all `_MAX_QUEUED`/`_MAX_TASK_RECORDS`/`_MAX_CANCELLED`/`_MAX_QUEUE_SIZE` → `RETENTION` module dict.
- Test: a grep-based unit test asserting the old names no longer exist; retention counts unchanged.

### Stage 4 — remainder + named overrides, transport stays delegated
- broker probe (a2a/broker.py `timeout=1.0` one-shot, service.py:750-751) → `ready.probe` (3 s **+ one retry** → I1 fixed; today it is the only retry-less, tightest bound in the mesh).
- Retry consolidation lands every remaining ladder on `RetryPolicy`: loop stream (already Stage 2), mcp connect (Stage 2), **mcp_gateway/connection.py health-reconnect** (617-700 — exponential 5→60 ×2, already the shape), **agent/plugins.py watchdog backoff** (46-49, 1→30 ×2, max 5 restarts — not in the original inventory), sharefile `_MAX_RETRIES/_RETRY_DELAY` (providers.py:330-345). Each keeps its own classification function (connection.py's `NeedsUserAuthError` pause is a per-policy retryable predicate).
- oauth `_POLL_TIMEOUT` 300 (oauth.py:88), media `deadline_s` 1200, wechat aiohttp totals, connection.py `_HEALTH_PING_TIMEOUT` 5 / `_HEALTH_CHECK_INTERVAL` 30 → **named overrides** (documented per-name in one `timeouts.named` block), not core knobs.
- httpx/aiohttp `Timeout(...)` defaults read from `timeouts.transport.*` (delegation retained — stacks own connect/read/write/pool).
- Retention stays count-bounded; interactive no-total default stays (now `turn.max`, off).

### Stage 5 — telemetry + review gate (§7.6)
- `bounded()` emits one `timeout_fired kind= owner= budget= ctx=` log line (via `slife/logfmt.py` conventions; hand-rolled `logger.warning("..._timeout ...")` lines like loop.py:1205-1208 are the pattern) on **every** expiry — including the `silent` class (publish-ack, tunnel settle) so non-events become queryable. Site-specific lines that predate `bounded` (memdb save 10 s, service.py:1681) stay until Stage 4 routes them, then collapse to the single emitter. Optionally surface the counter through `slife/health.py record()` so `system_health` can show "n timeouts" per component.
- High-frequency detector: if one `(kind, owner)` fires above a session threshold, emit a suggestion line (`timeouts.ready.spawn=60 fired 8× this session — consider raising`) — "inappropriate values" self-report.
- Review gate: **pytest AST scanner** (`tests/test_no_magic_timeouts.py`) — the repo has **no ruff/flake8/pre-commit today** (only pyright + pytest), so introducing ruff is heavier than a ~40-line `ast` walk that inspects `slife/**/*.py` for hardcoded duration literals (`timeout=`, `wait_for(..., N)`, bare `asyncio.sleep(N)`, module `_X = N.N` with duration names) and fails unless the site is in an explicit allowlist (the `timeouts` module, `RetryPolicy` defaults, named overrides, transport delegation, retention counts). Optionally mirrored as a `[tool.ruff.lint]` in a later pass — out of scope now.
- Tests: telemetry event asserted on raise/degrade/silent; planted violation trips the gate; allowlist additions require the reviewer's named-override entry.

## What is deliberately NOT changing
Count-bounded retention (memory safety ≠ time), HTTP-stack transport timeouts (delegation is correct), the interactive no-total default (now configurable rather than accidental), and the recently shipped B1/B2/B3 fixes (the mapping point, `on_abandoned`, the ≤0 rule — all *inside* Stage 1/Stage 2 surfaces, preserved byte-for-byte).

## Rollout / sequencing
- Each stage = its own commit + its own green test run; no cross-stage coupling except Stage 0 first (primitives) and the alias-drop/rehydration last.
- Stage 5's gate is the permanent container: once merged, every future timeout must enter through the `timeouts` module or a named override.
- Suggested commit sequence: `feat(timeouts): primitives+Bounds+RetryPolicy` → `feat(config): timeouts section+rehydration+invariants` → `refactor(loop): bounded tool/stall/retry` → `refactor(a2a/gateway/subagent): ready/work routes` → `refactor(dedup): notify/cleanup/retention single sources` → `feat(telemetry)+test(gate)`.

## Verification
- **Per stage:** its pytest modules + the full `tests/test_a2a_*.py`, `tests/test_loop.py`, `tests/test_tools_shell.py`, `tests/test_config.py`, `tests/test_agent_service.py` suites (the affected surface; pytest markers `unit`/`integration` with `--strict-markers`).
- **Config:** mutate `slife.json5` `timeouts` to a violating value → startup fails with the NAMED invariant error (e.g. `timeouts: heartbeat_timeout(10) < 3×heartbeat_interval(15)`).
- **End-to-end smoke:** boot the TUI (plugin spawn on a cold path, subagent spawn deriving from `ready.spawn`), an a2a sync send that degrades to async + late reply pushed as a message (B1 path), `execute_shell` with `timeout: 0` (B2 path) — all unchanged behaviors.
- **Gate:** run the magic-literal scan; plant one new `timeout=1.2` → it fails.
- **Exit criteria for the whole migration:** the §6 bug-hunt items (B1/B2/B3) still green, `grep -r "timeout=" slife` finds no hardcoded duration outside the allowlist, and the `timeouts` config section is the single answer to "what is the timeout here?"