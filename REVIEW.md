# Slife Code Review

**Review date:** 2026-08-11 · **This pass:** fresh full re-review after today's refactor round (a2a plugin rename + subagent-as-worker decoupling, memfiles-as-plugin, tool-naming unification, subagent-no-persona) plus the C1–C9 / W1–W2 / M2–M7 fix batch · **Prior passes:** 2026-08-06, 08-08, 08-09, earlier on 08-11 (the fix batch). · **Scope:** full codebase (~25.1k lines `slife/`, `credstore/`, 73 test files ~22k lines, install scripts).

**Method:** six parallel subsystem audits (agent core, MCP infra, built-in plugins, A2A/subagents, native tools, UI/infra) + every High finding re-verified against source + full suite re-run. Findings below are source-verified unless marked *(speculative)*.

**Current state:** full suite **1761 passed in ~25s** (re-run on the current tree). This pass surfaced **5 new High findings** — two of which **overturn prior "fixed" claims** (C7 approval-Esc, C2 MCP reconnect) — plus a set of Med findings and one confirmed doc-count error. **Follow-up: 5 of the 6 Highs were fixed and regression-tested** (C7, C2 session-id, OpenAI usage, F-prefix shadowing, M7 reindex bound); one (save_to_memory raw content) was **assessed by the maintainer as non-triggering** — secrets are already intercepted at the three chokepoints and user messages carry none, so no fix. Every previously-open item from the prior pass was re-verified; the ones still open are in §3.

---

## 1. Status at a Glance

### 1.1 Security — open (1 High + re-confirmed opens)

- **NEW-H2 — `memfiles__save_content_or_files` with a `url` is an unauthenticated SSRF fetch** (`_save_url`, `plugins/memfiles/server.py:435-475`). Only `scheme and netloc` are checked; aiohttp follows redirects. An LLM told to save `http://169.254.169.254/latest/meta-data/…` or `http://127.0.0.1:8080/…` fetches it server-side, writes the body to `<agent>.files/`, and — if the ngrok tunnel is up — returns a **public share URL** for it. Restrict to http(s) + non-local/loopback targets, or require explicit opt-in.
- **NEW-H1 (assessed, no fix) — raw user message persisted to the diary.** `save_to_memory` matches the inbox's raw `msg.content` (`inbox.py:297`) against the *sanitized* conversation copy (`service.py:1174-1188`); if sanitization ever changed the text, the turn would persist with empty `messages` and the raw value would reach the `user_message` column. **Maintainer decision (2026-08-11): not fixing.** Secrets are intercepted at all three chokepoints (inbound user messages, tool-call args, tool results) and user messages carry none, so the mismatch never triggers. Revisit only if that assumption breaks.
- **Re-confirmed open:** `config_env_get` returns resolved secret values verbatim (`tools/config.py:43-70`); `desktop_notify` single-quote-injects into a PowerShell one-liner (`platform.py:232-243`, macOS `osascript` branch has the same hole); MCP server stderr is stored raw and surfaced to the LLM via `conn.error` (`plugins/mcp/connection.py:237-241,694`); wechat `qrcode=`/`bot_token` logged unredacted (`plugins/wechat/server.py:475`, `client.py:111,374,390`); resolved URLs embedding `${VAR}` secrets logged (`connection.py:349-352,384-387`); mesh messages are unauthenticated by design (documented trust model — note only).

### 1.2 Correctness — fixed in the follow-up, except M5

All five of this pass's High correctness findings were **fixed and regression-tested in the follow-up** (§1.3):

- **C7 (was RE-OPENED)** — the App's `escape → cancel` binding no longer carries `priority=True` (`ui/app.py`), so Textual's priority pass (which checks the App first) no longer steals Esc from the modal's priority `deny`; with a modal up the App is excluded from the modal binding chain entirely. New tests reproduce the priority-pass resolution and assert the modal wins.
- **C2 (was RE-OPENED)** — `connect()` now clears `_session_id` at the top, so every reconnect initializes with no stale `mcp-session-id` (`plugins/mcp/connection.py`). Regression test: a failed reconnect leaves the session id cleared.
- **NEW-H3 (OpenAI usage)** — the usage block moved **before** the `if not event.choices: continue` guard (`llm_backends/openai.py`), so the `include_usage` final chunk (choices=[]) reports tokens and context accounting works on the default backend. Regression test uses the real `choices=[]` shape.
- **NEW-H4 (F-prefix shadowing)** — the as-is name rule now applies only to built-in routes (`_route != ProxyRoute.EXTERNAL`), so an external server named `check` advertising `check_mcp` registers as `check__check_mcp`, never shadowing the native tool; `{name}__` unregistration works again (`mcp/tool_adapter.py`). Two new tests (external must namespace, DIRECT keeps as-is).
- **M7 (was RE-OPENED)** — `_reindex_impl` increments `indexed` only when embeddings were actually stored, so a failing embedder (which returns `None`, not a raise) makes `indexed` stay 0 and the `no_progress` bound trips (`plugins/memdb/server.py`). Two new tests (failing embedder → indexed 0; healthy → indexed 1).
- **Partial M5 — still open.** `count_turns` grep mode escapes `%`/`_` without doubling backslashes and emits `LIKE ?` with no `ESCAPE '\'` (`store.py:334-339`); `memory_count(query="100%", mode="grep")` diverges from `memory_search(mode="grep")`.

### 1.3 Fixed to date (prior passes; re-verified this pass)

**Follow-up fixes from this pass** (all regression-tested, §1.2): **C7** approval-Esc steal (App escape no longer priority) · **C2** stale `mcp-session-id` on reconnect (`connect()` clears it) · **OpenAI usage chunk** reported before the `choices=[]` guard · **F-prefix** as-is rule restricted to built-in routes (no native-tool shadowing) · **M7** reindex `indexed` counts only stored embeddings (bound trips).

C1 (non-`"mqtt"` transport disables A2A, stub removed) · **C4** (subagent `_read_stdout`/`_dispatch_message` guards + `finally` resolve) · **C6** (wechat dedup key now includes text and empty-text is checked first — narrowed but see §2.2-M4) · **C8** (reserved plugin names rejected on both `mcp_set` paths) · **C9** (SSE-streamed Streamable responses parsed; response owned/closed) · **W1** (Anthropic coalesces a tool-result batch into one `user` message; cache_control on the last system block) · **W2** (Responses backend emits `function_call`/`function_call_output` items — the DESIGN "open question" is resolved) · **M2** (spawn resets process/client/port on failure — watchdog backs off, no stall) · **M3** (mcp pool + a2a client lifespan shutdown) · **M4** (process-tree kill on teardown, incl. already-exited parents) · **F-*** (memdb English, `_sys_trim` active-conversation targeting, `__`-filter unification, `rest_api_set_enabled(false)`, `cli_set_enabled` persistence, memfiles health pointer refresh, worst unredacted log sites). Subagent true-cancel via the unified `Inbox`/`cancel_correlation` is real (queued drops, running preempts).

### 1.4 Prior-claims-to-correct

- **C7 "fixed" was wrong — now genuinely fixed.** The prior REVIEW closed C7 but the fix was ineffective (Textual checks the App first in the priority pass); this pass re-opened it and actually fixed it (§1.2).
- **"README/DESIGN/zh-CN now match the code" was wrong for DESIGN.md** — its §Known Gaps & Open Items and inline callouts still listed closed items (C2, C6, C7, W2, M5–M7, memdb-Chinese, `__`-filter) as open. **Corrected in this follow-up:** DESIGN.md/README.md/README.zh-CN.md doc-synced (stale callouts removed, Known Gaps refreshed, C9/M5 remainders + N1/N3/N4 + security + logging re-listed as open).
- **"56 native tools / 14 categories" → the source defines 52 Tool classes** (50 LLM-visible + 2 harness `_sys_*`); 56 is unreachable under any config. **Corrected in this follow-up:** README.md/DESIGN.md/README.zh-CN.md now say 52; the table also moves `notify_user` to Display (code category) and lists all 8 subagent tools.

---

## 2. New Findings This Pass (Highs in §1.1/§1.2; Medium & Low below)

### 2.1 Correctness / lifecycle

| Severity | Area | Finding |
|----------|------|---------|
| **Med** | MCP | Watchdog unregisters a crashed plugin's tools by `"{name}__"`, but as-is registered tools (`mcp_set`, `wechat_*`, `a2a_*`) carry no `__` — after a crash (or after `max_restarts` gives up) the LLM keeps calling dead-client proxies for up to 30s×5, or the rest of the session (`agent/plugins.py:237-239`, `mcp/tool_adapter.py:99-102`). |
| **Med** | agent | WeChat/A2A watchdog restarts assign a fresh `poll_task` without cancelling the previous one; a restart fast enough to leave `enabled`/`client` live stacks N concurrent drain loops (`agent/service.py:1037,1051,1329,1335`). |
| **Med** | agent | `include_image` and `clear_context` mutate the **human** conversation during WeChat/remote-agent turns — only `_sys_trim` gets the active-conversation swap (`_auto_invoke`, `loop.py:437-446`); `ctx.conversation` is otherwise fixed (`tools/vision.py:58-61`, `tools/meta.py:223-228`). |
| **Med** | MCP | `MCPServerConnection.connect()` has no concurrency guard — health monitor + `call_tool` lazy/transport reconnect + `mcp_set_enabled(true)` can each spawn a transport (orphaning the loser) and, on a failed first connect, start two health monitors (`connection.py:165-252`). *(speculative on exact interleave; the code path exists)* |
| **Med** | MCP | A cancelled `connect()` leaves `_status = CONNECTING` (`except Exception` doesn't catch `CancelledError`); `_health_monitor` then skips `CONNECTING` forever and `call_tool` raises "not connected (connecting)" with no recovery (`connection.py:170,235,812,853`). |
| **Med** | MCP | `MCPClient.connect` catches `asyncio.CancelledError` and retries (30×0.1s) instead of propagating — `wait_for(connect())` cannot actually cancel (`mcp/client.py:111-123`). |
| **Med** | MCP | The gateway applies a hard 30s httpx timeout to every external-server request, contradicting the "enforcement stays in the loop" architecture — a legitimately slow tool call is aborted and the server spuriously torn down/reconnected (`connection.py:297-300`). |
| **Med** | A2A | A sync `a2a_send_task` cancelled by `a2a_cancel_task` or the loop's `tool_timeout` raises uncaught `CancelledError` and leaves `_pending_tasks[corr_id]` leaked; `cancel_task` on an in-flight sync task aborts instead of returning the documented status (`a2a/client.py:284-294,350-352`). |
| **Med** | subagent | `send_task` timeout leaves `_inflight` elevated and the record pending — a worker whose child loop hangs is permanently `is_busy`, every later send auto-queues async, and `_task_records`/`_async_results` grow unbounded (`subagent/process.py:212-217`). |
| **Med** | wechat | C6 narrowed the dedup key but does not eliminate drops: two genuine identical-text messages in one poll still collide (`from_user_id::context_token::text`, `plugins/wechat/server.py:110-121,148-154`). |
| Low | memdb | Hybrid search slices `merged[:limit]` with the raw (unclamped) LLM limit — `limit=0` returns `[]`, negative slices from the end, bypassing the `[1,200]` clamp (`server.py:498`). |
| Low | memdb | `search_semantic` filters `since/until` in Python *after* the KNN fetch (`limit*4` nearest) — on a large DB the nearest vectors can all fall outside the window, truncating results that exist (`store.py:475-517`). |
| Low | memdb | Transformer backend sniffs the real dimension after `diary_semantic` was created with the guessed default (1024); an unknown model of a different dim silently never stores embeddings (`embeddings.py:380-388`). |
| Low | MCP | SSE parsers require a literal space after `data:`/`event:` and overwrite the data buffer instead of concatenating multi-line data — valid `data:{…}` (no space) or multi-line events are silently dropped (`connection.py:504-509,630-634`). |
| Low | MCP | The documented "5s→60s backoff" is dominated by the fixed 30s health-interval sleep, so dead servers stay disconnected ~30s longer per attempt (`connection.py:809,844-848`). |
| Low | MCP | `_notify` fires untracked `asyncio.create_task` HTTP posts and writes stdio without `drain()` — teardown can surface unretrieved exceptions; the initialized notification can be dropped (`connection.py:653-683`). |
| Low | A2A | `a2a_cancel_task` reports "cancelled" from store state, then `_handle_result` flips the record to "completed" when the peer ignores the cancel (`client.py:354-361,680-696`). |
| Low | A2A | `_maybe_prune` only evicts terminal records and `record_result`/`record_cancel` never prune — a burst of async sends to slow peers grows the in-memory store past the 500 cap (`task_store.py:188-200`). |
| Low | agent | Pending A2A presence events are drained in `_footer_kwargs` even when `_auto_invoke("_sys_note")` is skipped because Esc was pressed — the "what changed" footer is silently emptied (`loop.py:370-399,754-756`). |
| Low | tools | `skill_set` update failure deletes the pre-existing skill directory — `shutil.rmtree` runs in the `except` even when `is_update=True`; a bad archive wipes the working skill (`tools/skill.py:337`). |
| Low | tools | `cli_set`/`rest_api_set` upsert drops an existing `enabled` field when rebuilding the entry (the file-fallback branches), violating the "idempotent upsert" contract (`tools/cli.py:185-188`, `rest_api.py:143-154`). Impact currently limited — nothing consumes `cli_tools[*].enabled`. |
| Low | tools | Abandoned `_async` tasks are never reaped — an `_async: true` call the LLM never polls leaves a finished task (and its held tool resources) in `_tasks` for the session (`tools/meta.py:131-139`). |
| Low | subagent | *(speculative)* a worker sync-sending back into a busy parent can deadlock both loops until `tool_timeout`/`task_timeout` fires — the busy→async guard covers sends *to* a busy worker but not the reverse. |
| Low | backends | *(speculative)* a Responses-API tool call that never emits `function_call_arguments.delta` is dropped from the batch — `_tool_index` is keyed only in the delta handler (`openai_responses.py:229-246`); unverifiable without a live endpoint. |

### 2.2 Logging (verified remaining)

`inbox_post`/`inbox_process` log raw `content` (`inbox.py:113,138-141`) · `wechat_in text=` (`service.py:1128`) · `a2a_in task=` (`service.py:1402`) · assistant `response text=` can echo a secret the model repeated (`loop.py:826-833`) · `shell_exec cmd=` and `run_python_script argv=` (`exec.py:100,177,184`) · `save_turn_failed user_msg=` (`memdb/server.py:156`) · `register_file token=` (`memfiles/server.py:138`) · `qr_fetched qrcode=` (`wechat/server.py:475`, `client.py:111`) · `notify_user` logs at **WARNING**, so `USER_NOTIFICATION title= message=` reaches both the console and the log file unredacted (`tools/display.py:160-162`). `req_start`/`conv_user` are correctly sanitized — this is the residual gap, not a blanket one.

Level misuse: `req_start`/`inbox_process`/`tool_error` at info (per-request detail should be debug); `tool_error` is info in the loop, warning in the registry, info in the mcp client — same event at three levels; `mcp_wrapper_init_failed` uses `logger.error` and drops the traceback (`service.py:392`).

### 2.3 Conformance / dead code (new this pass)

- `asyncio.to_thread` for the unbounded `SentenceTransformer` load/encode (`embeddings.py:375,393`) violates the documented `run_daemon` convention — a hung model download will hang the memdb plugin's interpreter shutdown. (`exec.py:37`'s `taskkill` `to_thread` is bounded, lower risk.)
- `subagent/tools.py` docstring claims "there is no `subagent_cancel_task`" — the tool ships and works; stale prose (`tools/subagent.py:16-18`).
- `_manager_or_hint` adds a subagent "recursion guard" gate contradicting the documented trust model (recursion is supported; the branch is effectively dead) (`tools/subagent.py:36-43`).
- `SubagentManager.broadcast` is never exposed by any tool (`subagent/process.py:501-514`).
- `get_conversation`/`set_conversation` and `AgentLoop.context_floor` are unused leftovers of the ToolContext refactor (`conversation.py:19-27`, `loop.py:223`).
- memdb/wechat close no resources on shutdown (no lifespan; `SessionStore.close()` never called; `_poll_task`/`_qr_task`/`_typing_tasks` left running) — benign at process exit, but a2a/mcp/memfiles now satisfy the contract they don't (`memdb/server.py:728-732`, `wechat/server.py:765-771`).
- *(speculative)* the memfiles eager tunnel start is awaited without a timeout inside the FastMCP lifespan — a hung `ngrok.forward` wedges the plugin and every restart (`server.py:74-81`).

---

## 3. Open Items (verified current — carried from prior passes)

### 3.1 Security & Logging

- **Unredacted log sites** — the §2.2 list; every secret-shaped value can land in `~/.slife/logs/*.log`.
- **N4** — let `credstore` supply exact-match secret values as a denylist complement to the known-shape allowlist (`logfmt.py`).
- **`config_env_get`** returns resolved secret values verbatim to the LLM (`tools/config.py:43-70`). Restrict or mask by design.
- **`desktop_notify`** single-quote injection into the PowerShell one-liner (`platform.py:232-243`) — escape or avoid shell interpolation.
- **memfiles `_save_url`** SSRF — now HIGH (§1.1-H2).
- **NEW-H1 (assessed, no fix)** — `save_to_memory` would persist raw content if sanitization ever changed the text; maintainer decision: messages carry no secrets (intercepted at all three chokepoints), so not fixing (§1.1).

### 3.2 Correctness

- **M1 remainder** — `install_python_package` still orphans the `uv` child: `asyncio.wait_for(proc.communicate(), 120)` with no tree-kill on timeout/cancel (`tools/exec.py:241-246`). Every other exec tool does tree-kill; this one doesn't.
- **M5 partial** — `count_turns` grep mode still lacks the LIKE-escaping fix (§1.2).

### 3.3 Conformance / Naming / Config / Docs

- **P4** — third-party plugins (not in the hardcoded `_plugins` set) get no watchdog; a crash is permanent until restart (`service.py:274-283`).
- **N1** — config writes are non-atomic (`write_config` = bare `write_text`, `_config_io.py:67-69`) and un-locked; the live race is cross-process (subagents write the same `slife.json5`). Funnel through one locked, atomic writer and refresh the in-memory snapshot after raw writes.
- **N3** — `switch_to_nvidia_free` tool names unverified against `nvidia-nim-mcp v2.1.2`; config runs `npx nvidia-nim-mcp` while `health.py:139` hints bunx.
- **`skill_set_enabled`** — dead: writes a `skills:` config section nothing reads (skills live on the filesystem) (`tools/skill.py:506-520`).
- **`execute_shell`** docstring says "disabled by default" — it is enabled (`tools/exec.py:4`).
- **`list_tools`** surfaces `_sys_note`/`_sys_trim` without a harness/reserved marker (`tools/meta.py:73-89`).
- **Doc drift:** DESIGN.md §Known Gaps lists closed items (C2/C6/C7/W2/M5–M7/F-lang/`__`-filter); README/DESIGN claim "56 native tools" (source: 52); README tool table misplaces `notify_user` (System) and under-lists subagent tools; `plugins/__init__.py` docstring says "memdb, mcp, wechat" and links a non-existent `docs/plugins.md`; DESIGN.md §Language policy still flags memdb Chinese (fixed).

### 3.4 Tests & CI (see §5)

---

## 4. Conformance Audit (this pass)

### 4.1 Plugins

Five built-ins (`mcp`, `memdb`, `wechat`, `memfiles`, `a2a`), all Streamable HTTP, all `create_plugin_server` + `run_plugin_server`. **Held up this pass:** lifespan shutdown (mcp pool, a2a client), watchdog restart resetting in-process state (memfiles client re-point), process-tree kill, reserved-name rejection, C9 SSE parsing, English LLM-visible strings. **Gaps:** P4 watchdog (third-party), memdb/wechat no lifespan cleanup, `_save_url` SSRF, `plugins/__init__.py` stale docstring.

### 4.2 Native tools

52 tool classes / 14 categories (see §1.4 — the "56" claim is wrong). Naming is uniform where it counts (`X_list/X_set/X_remove/X_set_enabled` for Skills/CLI/REST; `model_*`; `credential_*`; `config_env_*`; `subagent_*`; `a2a_*` in the plugin). **Gaps:** `skill_set_enabled` dead; `requires_a2a` gate still implemented though DESIGN says it is no longer the registration gate (`factory.py:65-69`); `update_context_footer` dead (only ever called with `""`); `config_env_get` leaks; upsert drops `enabled`; `_async` tasks un-reaped; `list_tools` no harness marker.

### 4.3 Harness tools

Two tiers by prefix: `_` = LLM-visible-but-reserved (`_sys_note`/`_sys_trim`); `__` = LLM-invisible plugin plumbing. The three registration paths now share the `__` predicate (F-filter confirmed). **Gap:** the as-is naming rule (`mcp_set`, `wechat_*`, `a2a_*`) means the watchdog's `{name}__` unregistration misses the plugin's own management tools (§2.1).

### 4.4 Logging

~250 call sites; the structured `event_name key=value` shape holds, but ≈2/3 of the *residual* unsanitized sites listed in §2.2 carry real user/tool/task content. No CJK in log messages; no `print()` misuse. Level drift and the `[wrapper]` relay duplication (`mcp/process.py:343-347`) remain.

### 4.5 Over-engineering

- **Dead:** `requires_a2a`, `update_context_footer`, `get_conversation`/`set_conversation`, `AgentLoop.context_floor`, `SubagentManager.broadcast`, `ok_json`/`error_json` back-compat re-export (`server_utils.py:352`), `skill_set_enabled`.
- **Redundancy:** two stderr relays (`logfmt.drain_stderr` + the `[wrapper]` relay); the subagent "recursion guard" that contradicts the trust model.
- **Good:** `_ensure_turn_consistent` is the right single-point invariant and holds at both call sites; the watchdog/backoff machinery is genuine; `run_daemon` convention is clean (except the two `to_thread` sites in §2.3); the health-monitor design is sound despite the session-id bug.

---

## 5. Tests & CI

**Current:** **1769 pass in ~25s.** The follow-up added 8 regression tests: C7 priority-pass resolution (2), MCP reconnect session-id reset (1), OpenAI usage-only chunk (1), F-prefix external/direct naming (2), memdb reindex stored-count (2). The old C7 tests that only asserted the `BINDINGS` list are kept but are now complemented by chain-resolution tests.

**Coverage gaps (unchanged from prior pass):**
- The C7 test still does not mount the real Textual app (asserts the priority-pass resolution from the declared bindings rather than a live keypress); an end-to-end `run_test()` with the modal mounted and `pilot.press("escape")` would close the last gap.
- Subagent child process is never spawned in a real test — the JSON-RPC ready handshake, `worker/cancel` preemption, response routing, shutdown ordering (`test_subagent_process.py`, `test_inbox.py` are mock-based).
- Backends' wire format never exercised against a real endpoint (W1/W2 unit-tested only).
- `tests/test_tools_shell.py:144` — `assert "�" in result or result` is tautological.
- `tests/test_main.py:41-48` and `test_ui_app.py:361-365` assert nothing (implicit no-raise).
- `ci.yml:28-38` builds wheels then `uv run pytest` re-syncs from pyproject → tests run against source, the wheel is never exercised; `pytest-cov`/`pytest-xdist` installed but unused; no deselect of slow/integration markers, no coverage gate; install scripts are never smoke-run; `publish.yml` installs an unpinned `twine`.

---

## 6. What's Good

- `slife/threads.py:run_daemon` is the documented convention and holds everywhere except the two `to_thread` sites called out.
- `_ensure_turn_consistent` enforces the no-orphan + alternation invariants at both save and load; memdb can rely on it.
- W1/W2 conversions are correct in source: Anthropic strictly alternates and tags the last system block with `cache_control`; Responses emits `function_call`/`function_call_output`.
- `cancel_correlation` preempts both queued and running tasks; subagent `_read_stdout`/`_dispatch_message` is defensive (per-message guards + `finally` resolve); the C4 fix is real.
- M4 tree-kill (incl. orphaned grandchildren), M3 lifespan pool/client shutdown, C9 SSE response parsing with owned/closed responses — all correct in source.
- memdb search is fully parameterized (no SQL injection); secret sanitization is wired into all three chokepoints plus `drain_stderr`; `SessionFormatter` + contextvars is async-safe.
- The UI renders user-supplied text with `markup=False` everywhere except the approval dialog's args preview (§2.2); the keyboard table matches DESIGN.
- The a2a/subagent namespace split is clean in code (`tools/a2a.py` gone, `mqtt__*` gone, workers in per-worker records, mesh store untouched by worker tasks).

---

## 7. Repo Hygiene

- `Jack.db` / `slife.db` (with `-wal`/`-shm`) are untracked local data — keep them out of commits (commit `bd05fc3` untracked a DB backup). `.coverage` and `logs/` are ignored.
- Doc sync done this follow-up: DESIGN.md §Known Gaps & inline callouts, README.md/README.zh-CN.md tool tables and counts all match the current code (52 tools, `notify_user` in Display, 8 subagent tools).
