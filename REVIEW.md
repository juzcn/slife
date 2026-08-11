# Slife Code Review

**Review date:** 2026-08-11 · **This pass:** conformance audit (plugins / native tools / harness tools / logging / over-engineering) + targeted fixes + README/DESIGN rewrite · **Prior passes:** 2026-08-06 (B/D items), 2026-08-08 (H1–H8, M1, S2/S5/S6), 2026-08-09 (H3) · **Scope:** full codebase (~24.5k lines `slife/`, `credstore/`, 71 test files ~21.6k lines, install scripts).

**Method:** parallel subsystem audits, every finding re-verified against source, then high-value fixes applied and re-tested; README/DESIGN/README.zh-CN rewritten to match the current code.

**Current state:** full suite **1742 passed** (re-confirmed after this pass's fixes). This pass fixed: memdb Chinese→English, `_sys_trim` targeting the wrong conversation, plugin-tool double-prefix naming, `rest_api_set_enabled(false)` ignored at startup, `cli_set_enabled` not persisting, the harness `__` filter inconsistency, memfiles health pointer not refreshed on watchdog restart, the worst unredacted log call sites, **C1** (A2A `transport:"http"` no longer crashes startup — non-`"mqtt"` transports disable A2A with a warning; the dead `HttpStreamableTransport` stub was removed), **C9** (streamable-http `_request_http` now reads SSE-streamed responses), **C2** (external MCP servers now get a background health-check — a died or hung server is marked `DISCONNECTED` and reconnected with backoff instead of staying `CONNECTED`), **W1/W2** (Anthropic and OpenAI-Responses backends now emit their own wire formats — Anthropic coalesces a multi-tool-result batch into one `user` message so messages strictly alternate; Responses emits `function_call`/`function_call_output` items instead of the Chat-Completions `role:"tool"`/`tool_calls` shape), and **C4/C5** (subagent `_read_stdout` no longer strands pending futures on a malformed message; A2A `CancelTask` and subagent `worker/cancel` now truly preempt the running agent loop via the same Esc mechanism — and the mesh `a2a_cancel_task` never mislabels a completed result as cancelled).

**Verdict:** the architecture remains coherent — one loop, one registry, one serial inbox, uniform tool surface. The conformance audit found the surface rules (language policy, tool naming, harness tiers, log format) were documented but not uniformly enforced; the docs had drifted further than the code. The code changes above restore the documented contracts; what remains is hardening (config-write serialization, dead code) and test/CI coverage.

---

## 1. Status at a Glance

### 1.1 Security — all prior items resolved/accepted ✅

No new open security findings beyond the logging hygiene items in §2.1. Prior: **S1** accepted (ngrok ~8h session bounds `expose_file`), **S3** accepted (WeChat `bot_token` ~24h TTL), **S4** accepted (subagent `SLIFE_CONFIG` carries the resolved key), **S2** REST URL validated http(s), **S5** installer no longer auto-kills, **S6** OAuth refresh never deletes on transient errors.

### 1.2 Correctness — open

| # | Finding | Status | Where |
|----|---------|--------|-------|
| C6 | WeChat dedup key (`from_user_id + context_token`) can **drop real messages** (per-conversation token; non-text items burn the key) | OPEN | `plugins/wechat/server.py:110-134` |
| C7 | Approval dialog "Deny (Esc)" intercepted by the App's priority `escape → cancel` binding — modal's deny never fires, loop cancels, modal sticks | OPEN | `ui/app.py:186`, `ui/approval_dialog.py:129-136` |
| C8 | MCP dispatch keyed on raw server name; `mcp`/`memdb`/`wechat`/`memfiles` not reserved — a colliding external server name misroutes | OPEN | `mcp/tool_adapter.py:224-236` |
| M2 | Watchdog restart can stall on a live-but-unconnected child (process assigned before `create_client`); backoff defeated | OPEN | `agent/plugins.py:107-115`, `service.py:479-495` |
| M3 | mcp/mqtt plugin lifespan never shuts down `ConnectionPool` / A2A client; HTTP/SSE external servers leak on exit | OPEN | `plugins/mcp/server.py:290-299`, `connection.py:846-850` |
| M4 | External MCP stdio teardown kills only the direct child (`terminate_process`), not the tree — npx/uvx grandchildren survive on Windows | OPEN | `plugins/mcp/connection.py:653-655` |
| M5 | memdb `search_grep` LIKE-escaping is a no-op (no `ESCAPE '\'` clause) — `%`/`_` queries return nothing | OPEN | `plugins/memdb/store.py:522-523,541` |
| M6 | memdb `memory_count` fts5 mode ignores `since`/`until`; search `limit` unclamped (negative → unlimited) | OPEN | `plugins/memdb/store.py:326-335`, `server.py:342,415` |
| M7 | memdb background reindex can spin forever on a persistently failing embedder | OPEN | `plugins/memdb/server.py:267-274` |

### 1.3 Fixed this pass

| ID | Finding | Fix |
|----|---------|-----|
| **F-lang** | memdb plugin returned LLM-visible strings in Chinese (regression of N5) | Translated `server.py` + `embedding_config.py` to English; updated 3 test assertions |
| **F-trim** | `_sys_trim` always trimmed the human conversation (`ctx.conversation`), even when the loop was processing a WeChat / remote-agent turn | `_auto_invoke` swaps `tool._ctx.conversation` to the active conversation for the call, then restores (`agent/loop.py`) |
| **F-prefix** | Plugin proxy names doubled the server prefix: `mcp__mcp_set`, `wechat__wechat_login` | `MCPProxyTool` registers a tool as-is when it already starts with `{server}_` (`mcp/tool_adapter.py`); docs updated |
| **F-rest** | `rest_api_set_enabled(false)` reconnected on restart | `_auto_connect_rest_apis` skips `enabled is False` (`agent/service.py`) |
| **F-cli** | `cli_set_enabled` changed `enabled` then `save_cli_tool` dropped it (no-op) | `save_cli_tool` accepts/persists `enabled` (`config.py`, `tools/cli.py`) |
| **F-filter** | Harness `__` filter differed across the 3 registration paths (`plugins.py` filtered `_`, others `__`) | Unified on the canonical `__` rule (`agent/plugins.py`) |
| **F-memfiles** | memfiles watchdog restart left `ToolContext.memfiles_client` pointing at the dead client — health checks reported a healthy plugin offline | Restart callback re-points `_tool_ctx.memfiles_client` (`agent/service.py`) |
| **F-logs** | Hot-path logs unredacted: tool-call args (`tool_timeout args=`), raw user input (`req_start`, `conv_user`), MCP wrapper stderr relay | `sanitize_secrets` in `_truncate_args`, `req_start`, and the `[wrapper]` relay (`agent/loop.py`, `mcp/process.py`) |
| **C1** | A2A `transport:"http"` crashed startup (`ValueError` in `A2AConfig.from_dict`); `HttpStreamableTransport` was a dead stub | Non-`"mqtt"` transport values disable A2A with a warning (`a2a_transport_unsupported`) at config load + in `start_a2a`; removed the never-imported `a2a/http.py` stub; tests + README/DESIGN updated |
| **C9** | Streamable-http `_request_http` only parsed single-JSON responses — a Streamable HTTP server streaming its POST response as `text/event-stream` (allowed by the MCP spec) broke `resp.json()` | Detect the `text/event-stream` content-type and read the first matching JSON-RPC message via new `_read_streamable_sse_response` (owns/closes the response, REVIEW H1 contract); 3 tests added (`plugins/mcp/connection.py`) |
| **C2** | External MCP servers had **no health-check/reconnect** — `MCPClient.ping()` was defined but never scheduled, and `MCPServerConnection` stayed `CONNECTED` after the stdio process died or an HTTP/SSE endpoint hung until a tool happened to be called | `MCPServerConnection.ping()` + a per-connection `_health_monitor` task pings every 30s, marks dead/hung servers `DISCONNECTED`, tears down the transport, and reconnects with 5s→60s exponential backoff; the monitor is cancelled by `disconnect()`/`remove_server()`, and `call_tool` lazy-reconnects a DISCONNECTED enabled server; 11 tests added (`plugins/mcp/connection.py`) |
| **W1** | **Anthropic backend** emitted consecutive `user` messages for a multi-tool-result batch (each internal `tool` msg → its own `user` block); strict-alternation endpoints (Bedrock / Bailian/Qwen) 400 | `_oa_msgs_to_anthropic` coalesces all tool results of one batch into a single `user` message and merges a user text message directly after tool results into that same block, so `messages` strictly alternate `user`/`assistant`; 3 tests added (`agent/llm_backends/anthropic.py`) |
| **W2** | **OpenAI-Responses backend** converted history to a Chat-Completions shape (`role:"tool"`, `tool_calls` on assistant) the Responses API rejects (wants `function_call` / `function_call_output` items) — would break multi-turn tool conversations | `_oa_msgs_to_responses` emits the Responses API's native items: standalone `{"type":"function_call"}` per tool call and `{"type":"function_call_output"}` per tool result, empty assistant text dropped; 2 tests added (`agent/llm_backends/openai_responses.py`) |
| **C4** | Subagent `_read_stdout` swallowed real errors and a malformed message killed the reader, stranding `_pending` sync futures until `send_task`'s timeout. (The `_push_futures` dangle was resolved earlier — the dead push machinery was deleted in the subagent-as-worker refactor; `_stop_process` already resolves `_pending` futures) | Extracted `_dispatch_message`, added non-dict `result`/`params` guards, per-message try/except (warning + keep reading), and a `finally` that resolves leftover `_pending` futures when the reader exits so `send_task` fails fast; 5 tests added (`subagent/process.py`) |
| **C5** | A2A `CancelTask` and subagent `worker/cancel` did **not** truly cancel: the receiver only acknowledged/logged, so a running agent or subagent kept going to completion. `a2a_cancel_task` also mislabelled a completed async result as "cancelled" (discarding it) | Both paths now **preempt the running agent loop — the same Esc mechanism** (`agent_loop.cancel()` via `Inbox.cancel_correlation`): A2A — the plugin drops a still-queued task (replying `Task.cancelled`), queues a cancel for the harness, which routes it to `inbox.cancel_correlation(corr_id)`; the cancelled run's reply carries the flag so the sender records the task cancelled. Subagent — the child now runs its tasks through the **same unified `Inbox`** (headless + no `save_turn` only), so `worker/cancel` → `inbox.cancel_correlation(rpc_id)` preempts the running loop. `A2AClient.cancel_task` returns `cancelled`/`completed`/`failed`/`not_found` and never consumes a completed result; 15 tests added (`a2a/{client,wire}.py`, `plugins/a2a/server.py`, `agent/inbox.py`, `subagent/headless.py`) |

### 1.4 Prior fixed (carried forward)

Pass-2: **H1** SSE transport · **H2** skill zip-slip/traversal · **H4** Responses streaming name · **H5** watchdog restart + 5 log formats · **H6** MQTT reconnection · **H7** OAuth stdout crash · **H8** stderr-tail hang · **M1** exec timeout orphaned trees. **H3** (2026-08-09) `_sys_note`/`_sys_trim` became real schema-declared tools auto-invoked by the loop.

---

## 2. What's Left to Do

### 2.1 Security & Logging

- **Unredacted log sites** — several structured call sites still log user/tool/task content without `sanitize_secrets` (the hot paths are fixed; remaining: `conv_user text=`, `inbox_post content=`, `wechat_in text=`, `shell_exec cmd=`, A2A `task=%s`, WeChat `qrcode=`/`context_token=`/HTTP bodies, memfiles `token=`, `save_turn_failed user_msg=`). Sweep all of them — secret-shaped values can land in `~/.slife/logs/*.log`.
- **N4** — let `credstore` supply exact-match secret values as a denylist complement to the known-shape allowlist (`logfmt.py`).
- **`config_env_get`** returns resolved secret values verbatim to the LLM (`tools/config.py:43-70`) — masked only by the shape allowlist. Restrict or mask by design.
- **`desktop_notify`** single-quote injection into the PowerShell one-liner (`platform.py:232-243`) — escape or avoid shell interpolation.
- **memfiles `_save_url`** is an unauthenticated GET to an arbitrary URL (SSRF surface) — restrict to http(s) + non-local targets or require explicit opt-in.

### 2.2 Correctness

- **C6** — dedup on `from_user_id + context_token + text`, or drop `_seen_keys` (the cursor already prevents server-side dups); don't burn the key on non-text items.
- **C7** — give the approval modal a real deny path that beats the App's priority `escape → cancel`.
- **M2/M3/M4** — watchdog restart must not block on a live child; plugin lifespan should shut down pools/clients; MCP stdio teardown should kill the tree (`_kill_process_tree`).
- **M5/M6/M7** — memdb: add `ESCAPE '\'`; honor `since`/`until` in fts5 count; clamp `limit`; bound the reindex loop.
- **C8** — reserve built-in server names in `mcp_set` / config load.
- **M1 remainder** — `install_python_package` timeout/cancel orphans the `uv` child (`tools/exec.py:237-242`) — add the same tree-kill as `execute_shell`.

### 2.3 Conformance / Naming / Config / Docs

- **P4** — third-party plugins (not in the hardcoded `_plugins` set) get no watchdog; a crash is permanent until restart. Extend the watchdog to auto-discovered plugins.
- **N1** — config writes are non-atomic (`write_config` = bare `write_text`) and un-locked; the live race is cross-process (subagents write the same `slife.json5`). Funnel through one locked, atomic writer and refresh the in-memory snapshot after raw writes.
- **N3** — `switch_to_nvidia_free` tool names unverified against `nvidia-nim-mcp v2.1.2`; reconcile the `bunx`/`npx` mismatch (config uses `npx`, `health.py:139` + installers say `bunx`).
- **skill_set_enabled** — dead: nothing persists a `skills:` config section (`tools/skill.py:506-520`). Decide the enable/disable store or remove the tool.
- **`execute_shell`** docstring says "disabled by default" — it's enabled (`tools/exec.py:4`).
- **`list_tools`** surfaces `_sys_note`/`_sys_trim` without a harness marker (`tools/meta.py:79-89`).
- **doc drift fixed this pass** — README/DESIGN/zh-CN now match the code (5 plugins, 56 tools/14 categories, `model_list` etc., `_`/`__` harness tiers, plugin naming rule). `plugins/__init__.py` docstring still says "built-in: memdb, mcp, wechat" and links a non-existent `docs/plugins.md`; `schema.sql` and UI messages are Chinese (deliberate for the TUI; comments fine, LLM-facing strings not).

### 2.4 Tests & CI (see §5)

Real subagent-process tests, end-to-end backend wire tests (the W1/W2 conversion shapes are unit-tested now, but never exercised against a real endpoint), CI that exercises the built wheel + install scripts, coverage gate.

---

## 3. Conformance Audit (§ this pass)

### 3.1 Plugins

Five built-in plugins (`mcp`, `memdb`, `wechat`, `memfiles`, `mqtt`), all Streamable HTTP, all `create_plugin_server` + `run_plugin_server`. **Fixed this pass:** double-prefix naming (F-prefix), memdb Chinese (F-lang), memfiles health pointer (F-memfiles). **Remaining:** watchdog gap for third-party plugins (P4), mcp/mqtt lifespan shutdown (M3), `plugins/__init__.py` stale docstring, and the mqtt plugin's poll loop leaks on restart (`service.py:1283-1367` — the old loop keeps draining after a crash+restart).

### 3.2 Native tools

56 classes / 14 categories, verified against the runtime registry. Naming is uniform where it counts (`X_list/X_set/X_remove/X_set_enabled` for Skills/CLI/REST API; `model_*`; `credential_*`); the exceptions are documented (Config has no `config_list`; Models substitutes `model_switch`). README now matches the real names. **Fixed this pass:** `cli_set_enabled` no-op, `rest_api_set_enabled(false)` ignored. **Remaining:** `skill_set_enabled` dead; `base.py` category docstring lists only 9 of the 14 categories; `requires_a2a` is dead (`factory.py:65-69` — no tool sets it `True`); `config_env_get` leaks raw secrets.

### 3.3 Harness tools

Two tiers by prefix: `_` = LLM-visible-but-reserved (`_sys_note`/`_sys_trim`, schema-declared, auto-invoked, prompt-forbidden); `__` = LLM-invisible plugin plumbing (mcp ×2, memdb ×2, wechat ×2, memfiles ×2, mqtt ×11). **Fixed this pass:** the three registration paths now share the `__` predicate (F-filter), and `_sys_trim` targets the active conversation (F-trim). **Remaining:** `list_tools` shows the `_` tools without a marker; the "Harness-only" description marker is inconsistent (mcp/memdb have it, wechat/memfiles/mqtt don't).

### 3.4 Logging

~250 call sites, ≈75–80% conform to `event_name key=value`. **Fixed this pass:** the worst unredacted sites (F-logs). **Remaining systemic:** (1) unsanitized user/tool/task content in otherwise-structured logs; (2) format drift (prose in `config.py`, freeform in `__init__.py`, the `[wrapper]` relay — now sanitized but still not `key=value`); (3) level misuse (`req_start`/`inbox_process`/`tool_error` at info; `mcp_wrapper_init_failed` at error instead of exception). No CJK in log messages; no `print()` misuse.

### 3.5 Over-engineering

- **Dead mechanisms:** `requires_a2a` (`base.py:169`), `update_context_footer` (called only with `""`, `conversation.py:44-57`). (The subagent push machinery — `set_push_notification`/`_push_futures`/`wait_for_task` — was deleted in the subagent-as-worker refactor.)
- **Redundancy:** three plugin registration paths (now one filter rule), two stderr relays (`logfmt.drain_stderr` + the hand-rolled `[wrapper]`), `ok_json`/`error_json` back-compat re-export in `server_utils.py:352`, `_classify`/`_PLUGIN_LABELS` fallback in `meta.py` that almost never runs.
- **Good:** `_ensure_turn_consistent` is exactly the right kind of single-point invariant; the watchdog/backoff/unregistration machinery is genuine; `run_daemon` convention is clean.

---

## 4. Tests & CI

**Current:** **1742 pass in ~23s** (regression tests added for H1–H8, M1 in prior passes; this pass added 11 health-check/reconnect tests for C2, 5 backend wire-format tests for W1/W2, 5 subagent `_read_stdout`/`_dispatch_message` tests for C4, and for C5 4 mesh `cancel_task` tests + 15 true-cancellation tests across inbox/a2a/subagent, plus test-assertion updates for the memdb translation).

**Gaps:**
- **Highest-risk path untested:** the subagent child now runs its tasks through the unified inbox (`subagent/headless.py`), but no test spawns a real subprocess — the JSON-RPC ready handshake, `worker/cancel` preemption of a running loop, response routing, and shutdown ordering are never exercised end-to-end (`test_subagent_process.py` / `test_inbox.py` are mock-based).
- **Backends' wire format not exercised against a real endpoint** — the W1/W2 conversion fixes are unit-tested (`test_llm_backends_anthropic.py`, `test_llm_backends_openai.py`), but no test calls a live Anthropic/Responses/Bedrock endpoint to confirm the shapes are accepted. H3 is covered by `tests/test_tools_harness.py`; H4 by streaming tests.
- `tests/test_tools_shell.py:143` — `assert "�" in result or result` is tautological.
- `ci.yml:34-38` builds a wheel but `uv run pytest` re-syncs from pyproject → tests run against source, the **wheel is never exercised**; `pytest-cov`/`pytest-xdist` installed but unused; integration/slow/e2e markers run on every PR with no deselect and no coverage gate.
- **No CI job exercises the install scripts** (no shellcheck / PSScriptAnalyzer / smoke run); `publish.yml` installs an unpinned `twine`.
- `tests/test_main.py:41-48` and `test_ui_app.py:361-365` assert nothing (implicit no-raise tests).

---

## 5. What's Good

- `slife/threads.py:run_daemon` is exactly the documented convention; no `run_in_executor` remains anywhere in `slife/`.
- Secret sanitization is a disciplined known-shape allowlist threaded through the loop, conversation, and stderr drains; `SessionFormatter` + contextvars is async-safe.
- `_ensure_turn_consistent` enforces the no-orphan + alternation invariants at every save/load/append point — the memdb persistence layer can rely on the harness's guarantee.
- Watchdog infrastructure (backoff, tool unregistration by `{name}__`, health records, `_stopping` guard) is genuine and now actually restarts every built-in plugin.
- Serial inbox with unconditional turn persistence in `finally` — memory survives cancel/error/max-iterations.
- `terminate_process` escalating force + `_close_pipe_transports` shows careful Windows ProactorEventLoop handling; the subagent stderr write bypasses the GBK codec crash correctly.
- `exec.py` uses `create_subprocess_exec` (no shell) for the non-shell tools; `credentials.py` never echoes secret values.
- The memfiles RFC 5987 `Content-Disposition` fix and the memdb turn-invariant persistence are both correct in the source.

---

## 6. Repo Hygiene

- `Jack.db` / `slife.db` (with `-wal`/`-shm`) are untracked local data — already gitignored/untracked; keep them out of commits (commit `bd05fc3` untracked a DB backup).
- `.coverage` and `logs/` are ignored. No stray `.VSCodeCounter/` regeneration was seen this pass.
