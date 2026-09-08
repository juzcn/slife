# Slife code review — 2026-09-08

Scope: full app pass (business-logic correctness, robustness, bugs, duplicate
code, dead code, optimization), with focused deep-dives on today's MCP-gateway
work (`connection.py` onto the mcp SDK, gateway reuse of memdb search/vector
helpers) and the plugin architecture (`PluginSpec`/`PLUGIN_SPECS`).

Method: four parallel subsystem sweeps (agent core; tools; a2a/subagent/ui/mcp
host; plugins) + direct verification of the high-severity items against the
running environment (mcp 2.1.1, fastmcp 4.0.1). Severity is advisory. Items
marked **(verify)** are strongly supported by code reading but I did not set up
the full runtime scenario; everything else was confirmed.

Legend: 🔴 critical · 🟠 major · 🟡 minor · ⚪ nit/dead · ♻️ dedup

---

## 1. Confirmed critical / major correctness

### 🔴 1.1 Plugin tool errors are silently treated as success — `isError` vs `is_error`
`slife/plugins/mcp_gateway/client.py:481`

```python
if getattr(result, "isError", False):
```

The installed SDK is **mcp 2.1.1**, whose `CallToolResult` field is
**`is_error`** (snake_case). `isError` does not exist on the parsed object
(verified: `hasattr(CallToolResult(…, is_error=True), "isError")` → `False`).
So this branch **never fires** for real SDK results.

Impact: when a built-in plugin child (memdb/memfiles/wechat/…) returns an error
result, `client.py` does not prefix it with `"Error: "`. The agent loop relies
on that exact prefix to set `is_error` (`slife/agent/loop.py:718, 1184` —
`is_error = result.startswith("Error")`), so the failure is recorded as a
success: the model sees a green tool result, and the persisted
`tool_result.is_error` flag (which Anthropic maps to native failure semantics)
is wrong. Restore/TUI also render it as success.

Note the month-old sibling in `connection.py:775` **reads the correct**
`is_error` — the two clients disagree on the same SDK.

Ceiling: tests pass because they mock `isError` directly on a `MagicMock`
(`tests/test_mcp_client.py:158, 177`), which always has the attr — so the
tests encode the bug.

Fix: `if getattr(result, "is_error", False):` and fix the two tests.

### 🟠 1.2 Scheduled task can fire twice — pending-fire guard expires while the trigger is still queued
`slife/agent/schedules.py:468-482` (guard set at `:237`, dropped at `:468-472`)

`_pending_fires[name]` is the only thing stopping the 30 s poll from re-firing
a task whose trigger is already queued. It is dropped purely on **age**
(`_time.monotonic() - t > MISS_GRACE`, grace = **120 s**). If the
`[Schedule <name>]` trigger is still sitting in the inbox queue behind a long
turn (a multi-minute LLM turn is routine) and `run_schedule_now` hasn't
dispatched yet, the guard expires; the next poll's `_classify` sees
`now - latest <= 120 s` → returns `("fire", …)` → a **second** trigger is
queued → `fire_task_now` records a second fresh run and two workers execute
the same task (two reports, double side effects).

Fix: when the guard is dropped by age, first scan the inbox queue for an
unprocessed trigger whose text starts with `[Schedule <name>]` and skip
re-firing; or key the guard on "trigger still queued" rather than wall-clock
age.

### 🟠 1.3 Poisoned-history rollback is OpenAI-only; Anthropic/Bailian 400s never roll back
`slife/agent/inbox.py:16, 333-345`

The rollback branch matches only `openai.BadRequestError` /
`ContentFilterFinishReasonError`. With the anthropic-messages backend active
(Claude, or Bailian/Qwen via the Anthropic-compatible endpoint), a provider
400 — including Bailian's content-moderation/policy 400s, exactly the case
this branch exists for — raises `anthropic.BadRequestError` and is **not**
matched. The poisoned turn stays, the `finally` re-saves it, and every
subsequent turn re-sends the same bad request and fails the same way.

Also note: bundling the OpenAI SDK class names imports the whole `openai`
package into `inbox.py` just for this match — a provider-agnostic "any
non-retryable 4xx" check (or an anthropic-name check too) removes the
dependency and fixes the bug in one move.

### 🟠 1.4 Empty subagent turn result is corrupted to the string `"{}"` on the wire
`slife/subagent/headless.py:46`

```python
msg["result"] = result or {}
```

When a worker turn ends with empty/`None` text (silent success, cancelled-
preempted turn), the reply serializes `"result": {}`. The parent
(`subagent/process.py` `_dispatch_message`) does `str(msg.get("result", ""))`
→ the caller receives the literal `"{}"` as the worker's answer, and the
auto-push path postpends `"{}"` into the parent's inbox.

Fix: `if result is not None: msg["result"] = result` (only `None` omitted).

### 🟠 1.5 Unsanitized remote A2A peer name injected into the TUI
`slife/ui/app.py:869-877` (`task_received`), also `:904-907`

`source` comes straight off the MQTT wire (`a2a/client.py:692`) with no
control-char scrub, then is rendered as `A2A(<source>)` in the chat view. A
peer can name itself `"\x1b]0;…"` or otherwise embed ANSI/news-eval sequences
into the user's terminal. The codebase already solved this exact problem for
presence lines (`slife/a2a/card.py:20-32` `_safe_name`, strips
`[\x00-\x1f\x7f]`). Reuse `_safe_name` on the A2A source. (Also
`peer_message` at `:879-887` computes `source` then hardcodes
`"Wechat> "` — the variable is dead.)

### 🟠 1.6 WeChat QR login can hang forever — live poll loop ignores `redirect_base`
`slife/plugins/wechat/server.py:394-469` (`_qr_poll_loop`)

`_poll_login_status` (`client.py:188-192`) returns `{"redirect_base": …}` for
`scaned_but_redirect`, but `_qr_poll_loop` only checks `bot_token`/`expired`/
`scanned`/`verify_code_blocked` — it ignores `redirect_base`, so it keeps
polling the original host until the 10-minute deadline. The client's own
`_wait_login_confirmation` (client.py:230-233) **does** follow
`redirect_base`, but that flow is dead code (`login()` has no callers in
`slife/` — verified). A real login that needs a node switch stays
`waiting` forever while the phone shows confirmed.

Fix: handle `redirect_base` in `_qr_poll_loop` by restarting the poll against
the redirect host (or wire `_qr_poll_loop` through the client flow).

## 2. Configuration-write integrity

### 🟠 2.1 Read-modify-write race on slife.json5 — most config tools skip the cross-process lock
`tools/models.py:217-293, 335-369, 408-434` · `tools/embeddings.py:194-224, 258-268, 301-319, 348-354` · `tools/cli.py:128-144, 177-182, 251-259` · `tools/skill.py:598-608`

All of these do bare `read_config` → mutate → `write_config` with **no lock**.
The loop executes tool calls **in parallel** (`loop.py:1192`
`asyncio.gather(*(_run_one(tc) for tc in tool_calls))`), and the codebase
already established this exact window as a race to be closed with
`config_read_modify_write` (`_config_io.py:110-125`, a cross-process
`filelock`), used only by `config.py` and `mcp_gateway/config.py`. Two
concurrently-gathered mutations (e.g. `model_set` + another `model_set`, or
`model_set` + `embeddings_model_set`) both read, both mutate their copy, both
`os.replace` — the second clobbers the first's change.

Fix: wrap each mutation in `with config_read_modify_write(path):` — same as
`ConfigEnvSetTool`/`add_server_entry` already do.

### 🟡 2.2 `write_config` strips JSON5 comments from slife.json5 on every write
`_config_io.py:85`

`json5.dumps(raw, …)` emits plain JSON. Every tool write silently deletes all
`//` and `/* */` comments on first mutation. For a config that uses comments
as documentation (JSON5's purpose), that is silent, irreversible loss. Fix:
splice only the edited section with a comment-tolerant writer, or at minimum
document + warn once.

### 🟡 2.3 `config_env_get`/`config_env_set` surface raw resolved secrets
`tools/config.py:73-99, 143`

`_lookup_one` returns `os.environ[key]` verbatim; `config_env_set` echoes the
full value back. At startup `Config._inject_env_vars` resolves `${VAR}` refs —
including **credstore-backed secrets** — into `os.environ`, so
`config_env_get` returns the plaintext secret into the LLM/context/TUI while
`credential_check` deliberately masks the same values. Contradictory
behavior; mask env-get display and don't echo values in the set success line.

## 3. Tools-layer robustness

### 🟡 3.1 `skill_set` update can destroy the live skill before the atomic replace
`tools/skill.py:404-411` — the old dir is `rmtree`'d **before**
`os.replace(tmp_dir, skill_dir)` (Windows limitation). If the replace then
fails (AV lock, permission), the previously-working skill is gone.
Fix: attempt the replace and only remove the old dir after success.

### 🟡 3.2 `attach_image` reads local files with no size cap
`tools/models.py:525` · `agent/multimodal.py:58-59` — `path.read_bytes()`
then base64 for any path; a multi-hundred-MB file is buffered whole and
expanded ~33% into the message; a non-image is silently coerced to PNG.
Fix: cap size (≈15–20 MB) and reject non-image extensions before reading.

### 🟡 3.3 `notify_user` crashes when the message contains `{…}`
`tools/system.py:1311` · `ui/i18n.py:299-311` — `t("notify_sent", title=…,
message=…)` goes through `str.format`, which is always applied in `t`. An
LLM-authored message like `"Deploy failed — see {output}"` raises `KeyError`
inside the tool. Fix: never route the message body through `.format`.

### 🟡 3.4 Async tool results bypass the result pipeline until polled; errors not flagged
`tools/system.py:1117-1123, 1146-1158` · `loop.py:1064-1121` — the raw output
(up to ~120 KB) sits un-sanitized/un-truncated in `_tasks` until
`check_async`; failures come back wrapped in `"✓ Task completed"` so
`startswith("Error")` never fires. Fix: sanitize/truncate at storage time and
prefix failures.

### 🟡 3.5 `wait_minutes` has no upper bound
`tools/timer.py:43-57` — `minutes * 60` goes straight to `schedule_wakeup`
unvalidated; a stray `1000000` schedules a ~2-year timer. Cap at 24 h and
reject larger.

### 🟡 3.6 `spawn_subagent` reports "spawned successfully" when the worker already existed
`tools/subagent.py:163-177` · `subagent/process.py:663` — reuse is hidden.
Have `spawn` return created-vs-reused and reflect it.

### Vampire `(verify)` items
- 3.7 PowerShell 7 output decoded with the OEM codec on zh-CN Windows
  (`tools/exec.py:118-128`, `platform.py:100-116`) — `detect_current_shell`
  can't distinguish PS 5.1 (OEM) from PS 7 (UTF-8). Verify actual revision
  and pick the codec accordingly.
- 3.8 `model_set`/`embeddings_model_set` document `api_key` as required for
  new providers but only check `base_url`; an unkeyed provider is auto-
  activated and fails confusingly on first real call.

## 4. Agent-core robustness

### 🟡 4.1 A2A cancellation can be lost in the pre-busy window
`agent/inbox.py:114-138 vs 242-243` — `cancel_correlation` first scans the
queue, then checks `_current_corr`; but `_current_corr` is only assigned after
several awaits (`task_received`/`peer_message`/…). A task-then-cancel arriving
in that window: message already dequeued, `_current_corr` still `None` → no
cancel → task runs to completion. Stash the correlation id before the first
await, or re-check after assignment.

### 🟡 4.2 Subagent stdin/stderr startup mutual-block window
`subagent/process.py:177-198` — `start()` writes the (possibly large) cloned
context and `await proc.stdin.drain()` **before** the stdout/stderr readers
are launched. If the child boots with >~64 KB of unresolved stderr meanwhile
(plugin connects), child blocks writing stderr, parent blocks draining stdin,
both stuck until the 30 s kill. Low probability; same class as the prior
stderr pipe-wedge incident. Fix: start both readers immediately after spawn.

### 🟡 4.3 Unbounded inbox queue
`agent/inbox.py:81` — `asyncio.Queue()` with no `maxsize`, `post()` never
blocks; a WeChat/A2A burst during a long turn grows memory without limit.
The presence deque has a 1000-entry cap; the inbox has none.

### 🟡 4.4 Stale/duplicate worker responses treated as fresh async completions
`subagent/process.py:564-579` **(verify)** — a response for an unknown/un-
tracked rpc_id is handled as a brand-new async result: decrements `_inflight`,
flips the record to `completed` (resurrecting a cancelled record), fires
`_notify_manager_task_done`, double-pushes into the inbox. Track known
rpc_ids; log-and-ignore unknown ones.

### 🟡 4.5 Fire-and-forget sharefile tunnel probe task
`agent/service.py:805, 830` — `_watch_sharefile_tunnel` spawns
`_check_sharefile_tunnel` via `asyncio.create_task` and never stores/cancels
it, unlike the supervised `poll_task`/`restore_task` the module manages
elsewhere. Hang it off the same lifecycle slot.

### 🟡 4.6 `_context_time_start` reset to wall-clock now after a trim
`agent/loop.py:583-592` (**verify** — cosmetic) — after `_trim_after_save`
pops every tracked date it sets the footer's "Context covers since HH:MM"
anchor to `datetime.now()` instead of the current turn's start. Purely
display, contradicts the documented invariant.

### 🟢 (verify) Anthropic `prompt_tokens` undercounts with active caching
`agent/llm_backends/anthropic.py:329` — `usage.input_tokens` excludes
`cache_read_input_tokens`/`cache_creation_input_tokens`; the value is treated
as "context size at turn end" and is the trim gate. Impact small (system
prompt is a few K tokens, history uncached). Confirm SDK semantics.

## 5. MCP gateway (today's work) — targeted findings

### 🟡 5.1 `mcp_set` idempotency check compares resolved env vs raw `${VAR}` — every re-run reconnects
`slife/plugins/mcp_gateway/server.py:505-522` + `connection.py:246-248`

Auto-connect builds `ServerConfig` via `resolve_server_config` (env refs
resolved to secrets); `mcp_set` builds the config with the raw `${VAR}` refs
and only resolves at spawn. Verified: `env={'FOO': 'actual-secret'}` vs
`env={'FOO': '${FOO}'}` → `_server_config_equal` → **False**. So re-running
`mcp_set` with identical args tears down and reconnects instead of returning
`already_connected` — silently re-running OAuth/device flow. Fix: resolve env
in the `mcp_set` path before comparing (or compare the unresolved forms).

### 🟡 5.2 Duplicate `notifications/initialized` handshake
`slife/plugins/mcp_gateway/connection.py:458-460`

The mcp SDK's `ClientSession.initialize()` **already sends**
`InitializedNotification()` (verified in mcp 2.1.1 source). `connect()` sends
it a second time — a duplicate handshake message every connect. Harmless with
FastMCP currently, but a redundant protocol message and one more thing to
drift if the SDK changes. Drop the explicit `send_notification`.

### 🟡 5.3 `_connect_http` treats the outer connect timeout as "SSE not supported"
`slife/plugins/mcp_gateway/connection.py:343-378` + `:450`

The whole connect runs inside `asyncio.timeout(_CONNECT_STARTUP_TIMEOUT)`
(120 s). When `sse_client`'s enter hangs and the budget expires, `TimeoutError`
(an `Exception`) is caught by the `except Exception:` at `:349` as "SSE not
supported" — the code tears the stack down (another ≤2 s) and retries as
Streamable HTTP, but the outer `asyncio.timeout` has already fired, so every
subsequent await in the fallback immediately re-raises. Net effect: a server
whose SSE endpoint is slow/hung but which would serve streamable-HTTP burns
the whole budget and fails. Catch `TimeoutError` separately and re-raise.

### 🟡 5.4 Re-sharing an edited file returns the old, now-permanently-403 token
`slife/plugins/sharefile/server.py:156-171` (`_register_file`) — dedupes by
path and returns the existing token without re-checking the file's stat pin
(the serve path refuses any changed file). Edit → `share_file` again → same
URL → everyone gets 403. Fix: re-pin (new token) when the stat differs.

### ⚪ 5.5 `NOTIFY_TIMEOUT` in `connection.py:75` is an unused constant
The server has its own `_NOTIFY_TIMEOUT` (`server.py:214`); the connection one
is never read. Delete or wire up.

## 6. Duplicate code (consolidation targets)

### ♻️ 6.1 SemanticManager actor + drain loop is copy-pasted between memdb and mcp_gateway (~170 lines)
`memdb/semantic.py:65-342` vs `mcp_gateway/semantic.py:51-323` —
`_drain_loop`, `_process_batch`, `_stop_drainer`, `_status`,
`MAX_REINDEX_NO_PROGRESS`/`REINDEX_BATCH_LIMIT`, `on_saved`/`enable`/
`disable`/`close` near-identical. memfiles already imports the memdb one —
the gateway only diverges in the store contract (`replace_embedding` vs
`replace_embedding_chunks`). Consolidate onto one class parameterized by the
store (a callback or small protocol).

### ♻️ 6.2 Trigger-aware SQL splitter is verbatim in two places (~90 lines)
`memdb/store.py:1206-1311` vs `mcp_gateway/store.py:177-296` —
`_split_sql`/`_looks_like_trigger_start`/`_looks_like_trigger_end` are
byte-for-byte copies. The gateway already imports `_clamp_limit`/
`_contains_cjk`/`_serialize_f32`/`_to_fts5_query` from memdb; import these
three too.

### ♻️ 6.3 Secret sanitizer is copy-pasted (~85 lines)
`logfmt.py:369-465` vs `mcp_gateway/logging.py:279-364` — independently
maintained redaction tables (`_SECRET_PATTERNS`, `_URL_CREDENTIAL_PATTERN`).
A regex fixed in one can silently diverge in the other. Import from
`slife.logfmt`.

### ♻️ 6.4 OpenAI-compatible embedding client API path near-duplicated
`memdb/embeddings.py:481-624` (`_discover_model`/`_probe_api_dim`/`_call_api`/
`_get_client`) vs `mcp_gateway/embeddings.py:141-285`; plus
`_looks_like_placeholder` duplicated (`.py:31` and memdb copies). Local
GGUF/transformer branches are memdb-only; the API backend should be one class.

### ♻️ 6.5 FTS5-keyword + CJK-LIKE-fallback + grep trio re-implemented three times
`memdb/store.py:734-832, 933-969`, `memfiles/store.py:996-1046`, and
`mcp_gateway/store.py:517-602` — same `_contains_cjk` gate, `_to_fts5_query`,
`snippet()+rank` SELECT, the inline four-place `%_\` escaping, and the
`instr(...)`-anchored snippet. Only column lists differ. A column-parameterized
builder + shared escaping helper would collapse all three.

### ♻️ 6.6 Smaller duplicates
- `tools/user_prefs.py:28-48` `_MemfilesCallMixin` vs `tools/schedule.py:56-86`
  `_ScheduleMixin` (near-identical client plumbing + `_OFFLINE` constant).
- `a2a/task_store.py:78-83` `TaskStatus_dict` re-implements `wire.TaskStatus
  .to_dict`.
- `ui/model_picker.py:29-42` re-defines `_mc`/`_lit` that `ui/content.py`
  already exports.
- `config.py` `_resolve_secret` vs `env.py:resolve_env` — different contracts
  (`_resolve_secret` doesn't understand `${VAR:-default}`), unify.
- `agent/loop.py` + `system_prompt.py` + `restore.py` re-derive the same
  timestamp format string; `inbox.py`/`schedules.py`/`restore.py` each parse
  ISO offsets independently. One `format_turn_ts()`/`parse_iso_aware()`.
- Four near-identical config-aware tool bases (`_ConfigPathMixin`/
  `_SkillDirMixin`/`_ModelConfigTool`/`_EmbeddingsConfigTool`).
- JSON "parse-or-pass" duplicated (`schedule._parse`, `user_prefs`, `system`)
  and the read-stderr relay family (`logfmt.read_stderr_lines` vs
  `mcp_gateway/logging.read_stderr_lines` vs `process._log_stderr`).
- `bootstrap.py:29-81` vs `server_utils.py:226-284` logging setup.

## 7. Dead code

- `subagent/process.py:136-141` `pending_async_ids` — never used anywhere.
- `plugins/wechat/client.py:117-155, 204-244` `login()`/
  `_wait_login_confirmation()` — no callers in slife/ or tests/. Notably it's
  exactly this dead code that contains the `redirect_base` handling missing
  from the live path (finding 1.6) — the dead code masked the gap.
- `ui/app.py:404-406` `_recovery_info` `"skipped"`/`"budget"` — restore reads
  only `"turns"`; the i18n key `"restored_partial"` is never referenced.
- `a2a/card.py:61-63` `AgentCard.create` — used only by tests.
- `a2a/config.py:46-50` `http_host`/`http_port` — parsed but unreachable.
- `tools/rest_api.py:70-76` `get_rest_apis_summary` — production-dead.
- `tools/models.py:469` `_requires_vision` — never read in production.
- `agent/loop.py:270, 289` `memdb_enabled` — write-only.
- `agent/plugins.py:436-450` watchdog `module` fallback — unconditional
  `restart_cb` makes the branch unreachable.
- `tools/base.py:31-43` doc cites removed `os_info.py`.
- `agent/service.py:1808` `except (asyncio.TimeoutError, Exception)` —
  redundant tuple.
- `ui/app.py:879-887` `peer_message` dead `source` var (finding 1.5).

## 8. Checked-and-fine (not bugs)

- `restore.py`'s Phase-2 `history.messages = all_messages` re-assignment is
  safe (`_ensure_turn_consistent` mutates in place).
- `_process_stream` retry ladder, `save_to_memory`'s `isinstance(str)` guard,
  watchdog backoff/`stop()` ordering — all correct.
- memdb/memfiles/mcp_gateway `_write_lock` discipline on shared aiosqlite
  connections — consistent, no split-transaction bug.
- The semantic drainer's `MAX_REINDEX_NO_PROGRESS` bound trips correctly (the
  `complete=True`-hot-loop edge is unreachable); Connections in
  `connection.py` clean themselves on `CancelledError`, and `disconnect()`
  correctly serializes against in-flight connects via `_connect_lock`.

## 9. Suggested fix order

1. **1.1 (`is_error`)** — one-char fix, breaks failure semantics for every
   built-in plugin tool; fix tests to assert on a real SDK model.
2. **1.2 (schedule double-fire)** and **1.3 (antib-only rollback)** — both
   cause duplicated/terminal-confused user-visible behavior.
3. **1.4 / 1.5 / 1.6** — the cheap targeted correctness/robustness wins.
4. **2.1 config-race** — wrap config mutations with the existing lock.
5. **5.1 / 5.2 / 5.3** — today's gateway consolidation finish line.
6. **6.x dedup** — 6.2 and 6.3 are drop-in import swaps; 6.1 is the largest
   payoff, 6.5 the most risk to do safely.