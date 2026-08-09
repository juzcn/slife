# Slife Code Review

**Review date:** 2026-08-08 · **Original pass:** 2026-08-06 (B1–B10, D1–D10) · **Scope:** full codebase (~45k lines: `slife/`, `credstore/`, `skills/`, install scripts, 90 test files) · **Method:** 7 parallel subsystem audits cross-checked against README/DESIGN claims; every HIGH finding re-verified against source; fixes applied in order and re-tested after each.

**Current state:** full suite **1726 passed**. All pass-2 high-severity bugs (H1–H8, M1) are **fixed**; pass-1 is mostly fixed. H3 was resolved on 2026-08-09 (see §4).

**Verdict:** the architecture is coherent — one loop, one registry, one serial inbox, uniform tool surface. The Aug-6 pass was largely actioned; the Aug-8 pass found and fixed a cluster of latent defects in non-default paths (SSE transport, Responses backend, plugin watchdog, MQTT reconnect, OAuth device flow, subprocess leaks). What remains is hardening: security (§2.1), a few correctness gaps (§2.2), config-write serialization, test/CI coverage, and naming/doc cleanup.

---

## 1. Status at a Glance

### 1.1 Security — all resolved ✅

> **Resolved 2026-08-08** — **S1** accepted (ngrok free session ~8h bounds `expose_file` exposure). **S3** accepted (WeChat `bot_token` ~24h TTL). **S5** `install.ps1` auto-kill **removed** — the installer only warns (precise `uv\tools\slife` match) and the user closes slife themselves. **S2** REST spec/base URL now validated as http(s) before the npx spawn. **S6** OAuth refresh no longer deletes tokens on transient transport errors (only on 4xx); RFC 8628 terminal poll errors (`access_denied` etc.) abort immediately. **S4** accepted by decision — the subagent `SLIFE_CONFIG` env var *does* carry the resolved `api_key` (config inheritance is how the subagent gets the model config, and the key isn't always in `os.environ`), but it's a same-user process, Windows has no `/proc/…/environ`, and a normal config stays far under the 32KB env-block limit.

### 1.2 Open / Partial — Correctness & Design (§2.2 for detail)

| # | Finding | Status | Where |
|----|---------|--------|-------|
| C1 (B1) | A2A `transport:"http"` **hard-crashes startup** (uncaught `ValueError`); `start_a2a` http branch is dead code; `HttpStreamableTransport` still raises `NotImplementedError` | OPEN | `a2a/config.py:82-89`, `service.py:1285-1295` |
| C2 (B3½) | External MCP servers have **no health-check/reconnect** — `MCPClient.ping()` defined, never scheduled; a died stdio server stays `CONNECTED` until a tool call fails | OPEN | `mcp/client.py:255`, `connection.py` |
| ~~C3~~ ✅ | `save_to_memory` raw-vs-sanitized match — **accepted 2026-08-08**: real user messages don't contain secret-shaped strings, so the stored (sanitized) content matches in practice | ACCEPTED | `service.py:1148-1167` |
| C4 | Subagent `_stop_process` leaves `_push_futures` dangling (spurious 120s timeouts); `_read_stdout` swallows real errors while `_running` stays `True` | OPEN | `subagent/process.py:114-130,272` |
| C5 | A2A `cancel_task` is a no-op for subagents and mis-routes subagent ids into MQTT; MQTT async-task tool descriptions promise inbox push that never happens | OPEN | `tools/a2a.py:439-451`, `client.py:596-621` |
| C6 | WeChat dedup key (`from_user_id + context_token`) can **drop real messages** (`context_token` is per-conversation, so 2nd+ messages share the key) | OPEN | `plugins/wechat/server.py:110-134` |
| C7 | Approval dialog "Deny (Esc)" is intercepted by the App's priority `escape → cancel` binding — the modal's `on_key` never fires | OPEN | `ui/approval_dialog.py:129-136`, `ui/app.py:184` |
| C8 (D2) | MCP dispatch centralized behind `ProxyRoute` but still **keyed on the raw server name**; nothing reserves `"mcp"`/`"memdb"`/`"wechat"`, so an external server with that name misroutes | PARTIAL | `mcp/tool_adapter.py:253-265` |
| C9 (D4) | Harness-only tool filtering uses **three inconsistent mechanisms** (`_`-prefix, a hardcoded `mcp_call_tool` exception, a `"harness-only"` description marker) | PARTIAL | `service.py:261,441` |
| N1 (D3) | Config **write race** persists — no file lock, two writers (Config methods + raw `read/write_config`), concurrent `asyncio.gather` tools clobber the file and stale the in-memory snapshot | PARTIAL | `tools/_config_io.py`, `config.py` |
| N2 (D6) | Tool naming inconsistent: `model_list` / `cli_list_tools` / `rest_api_list` / `mcp_list_servers`; README is a third naming layer | OPEN | `tools/` |
| N3 (D7) | `switch_to_nvidia_free` **always errors out of the box** (hardcodes provider `"nvidia"` + `nim_*` tool names not re-verified against `nvidia-nim-mcp v2.1.2`); docs/installers still say `bunx`, config runs `npx` | OPEN | `tools/models.py:571,593,668` |
| N4 (B5) | Secret sanitizer is a known-shape allowlist — **no exact-match denylist** from credstore for the user's real secrets | OPEN | `logfmt.py` |
| ~~N5 (B9)~~ ✅ | Residual Chinese in tool output — translated to English (also the 3 sibling memdb status strings) | FIXED | — |
| ~~N6 (B7)~~ ✅ | Stale HMAC docstring + dead `# ═══ include_image` stub — docstring corrected, stub + unused `Config` import removed | FIXED | — |
| ~~N7 (D9)~~ ✅ | `a2a_list_tasks` description now warns the store is **in-memory and empty after restart** | FIXED | `tools/a2a.py:471-478` |

### 1.3 Deferred (§4)

| H3 | Synthetic `_sys_note` / `_sys_trim` tool calls break the Anthropic & OpenAI-Responses backends | ✅ **RESOLVED 2026-08-09** — both became real schema-declared tools, auto-invoked by the loop (see §4) |

### 1.4 Fixed (§3)

Pass-2: **H1** SSE transport · **H2** skill path-traversal/zip-slip · **H4** Responses streaming name · **H5** memdb watchdog + retry-on-failure (+ 5 broken log formats) · **H6** MQTT reconnection recovery · **H7** OAuth device-flow stdout crash · **H8** `_read_stderr_tail` hang · **M1** exec timeout orphaned process trees.

Pass-1: **B2** image-attachment feedback · **B4** subagent channel attribution · **B6** subagent error message · **B8** dead code (`trim_context`, `a2a/tools.py`) · **B9** (client error paths) · **D5** native-tool approval gate · **D10** ngrok endpoint pooling.

---

## 2. What's Left to Do

### 2.1 Security

**All security items resolved/accepted as of 2026-08-08** — see the §1.1 note (S1/S3/S4 accepted, S2/S5/S6 fixed). No open security findings.

### 2.2 Correctness

- **C1 (B1)** — catch the `A2AConfig` `transport:"http"` `ValueError` and **disable A2A with a warning** instead of crashing startup; delete the dead http branch and either implement or remove `HttpStreamableTransport`.
- **C2 (B3½)** — schedule `MCPClient.ping()` as a background health-check for external stdio servers; on failure, mark `DISCONNECTED` and trigger the existing lazy reconnect.
- ~~**C3**~~ — **accepted 2026-08-08**, no fix: the conversation holds no secret-shaped strings by save time, so the raw↔sanitized match holds in practice.
- **C4** — resolve/cancel `_push_futures` in `_stop_process`; distinguish EOF from real failure in `_read_stdout` and surface the error instead of swallowing.
- **C5** — make `a2a_cancel_task` actually cancel the running subagent task (not just pop a stored result); either wire MQTT async results into the inbox or correct the tool descriptions.
- **C6** — dedup on `from_user_id + context_token + text` (or drop the redundant `_seen_keys` — the cursor already prevents server-side dups).
- **C7** — add a Screen-level `escape` binding to the approval modal (or block the App's priority binding while a modal is active).

### 2.3 Naming / Config / Docs

- **N1 (D3)** — funnel all config writes through one locked writer; refresh the in-memory snapshot after raw-file writes.
- **N2 (D6)** — standardize tool names on `prefix_verb_noun` at the next breaking release.
- **N3 (D7)** — fix `switch_to_nvidia_free` against the shipped `nvidia-nim-mcp` tool names or move it to a skill; reconcile `bunx`→`npx` in DESIGN/health/installers.
- **N4 (B5)** — let `credstore` supply exact-match secret values as a denylist complement.
- ~~**N5/N6/N7**~~ — done 2026-08-08 (Chinese strings → English incl. the sibling memdb status strings; memfiles HMAC docstring corrected; meta.py stub + unused `Config` import removed; `a2a_list_tasks` notes the in-memory store).

### 2.4 Tests & CI (see §5)

Real subagent-process tests, end-to-end backend tests, CI that exercises the built wheel + install scripts, coverage gate.

---

## 3. Fixed — Log

| ID | What | Fix (one line) | Tests added |
|----|------|----------------|-------------|
| H1 | SSE transport broken by construction | `_connect_http` hands the response to `_read_sse_stream` (owns + closes it); endpoint accepts bare path or JSON `{uri}`; SSE client carries user headers | `test_sse_connect_stream_stays_open`, `test_sse_connect_bare_path_endpoint` |
| H2 | Skill path traversal / zip-slip / rmtree | `_ensure_within()` on name + file paths + remove; `_extract_zip_safely()` manual member validation (zipfile has no `filter=` param) | `TestSkillSecurity` (4) |
| H4 | Responses streaming never got the tool name; id used item-id not call-id | record `{name, call_id}` on `output_item.{added,done}`; deterministic index; emit `call_id` | `TestOpenAIResponsesBackend` (2) |
| H5 | memdb watchdog could never restart; one failed restart killed it | loop alternates wait/restart; failed restart backs off & retries; fallback spawn drops `_harness_tools` guard; + 5 broken log formats | `TestWatchdogRestart` (2) |
| H6 | MQTT no reconnection recovery | `_on_connect` restores `_connected`, re-subscribes, fires `on_reconnect` (re-announce presence); `messages()` gated on `_closed`; `_connect_event` created in `__init__` | `TestMQTTAdapterReconnect` (4) |
| H7 | OAuth device flow crashed on closed stdout | instructions → stderr with `[OAUTH]`/`[OAUTH-ACTION]` markers; parent surfaces at WARNING + desktop notification | `test_emit_user_message…`, closed-stdout completion, marker protocol (3) |
| H8 | `_read_stderr_tail` hung on a live-but-silent child | bounded read: ≤40 lines, each with 1s `wait_for` | `TestMCPWrapperProcessStderrTail` (2) |
| M1 | `execute_shell`/`run_python_script` timeouts orphaned the process tree | `_kill_process_tree()` — `taskkill /F /T` on Windows, `killpg` on POSIX (`start_new_session=True`) | `TestKillProcessTree` (2) |
| B2/B4/B6/B8/B9/D5/D10 | pass-1 items | see §1.4 | — |

---

## 4. Resolved — H3

Synthetic `_sys_note` / `_sys_trim` tool calls broke the Anthropic & OpenAI-Responses backends (both validate history tool names against the declared `tools` list → 400 on the first turn).

**Resolved 2026-08-09** — the fix went further than the agreed fold-to-text: instead of hiding the synthetic pairs at serialization time, `_sys_note` / `_sys_trim` became **real, schema-declared native tools** (`slife/tools/harness.py`), auto-invoked by `AgentLoop._auto_invoke()` through the same execution path as LLM-requested tools. Because their names are now in the declared `tools` list, history validation passes on every backend; no backend serialization special-casing is needed, and DeepSeek (which never validated) is untouched.

- **Design preserved.** The static system prompt stays byte-identical (prompt-cache hits survive); the dynamic status is a message-stream tool pair, never a second `system` message or injected assistant text (no false author attribution).
- **New constraints kept the design honest.** The system prompt §6 forbids the LLM from calling `_`-prefixed tools; `_sys_note` is pure (reads state), `_sys_trim` genuinely trims to the floor (a legitimate action if the LLM calls it anyway).
- **Related fix.** The harness tool-call pairs — and every turn — must alternate user/assistant on the wire, so cancelled/errored turns close with an assistant message (`_ensure_turn_closed`) and restored history is normalized (`Conversation.ensure_alternation()`). This also closes a latent consecutive-user 400.

---

## 5. Tests & CI

**Current:** **1691 tests pass in ~19s** (was 1670 at review start; +21 regression tests from the fixes).

**Gaps:**
- **Highest-risk path untested:** `tests/test_subagent_process.py` is entirely mock-based — the JSON-RPC ready handshake, response routing, shutdown ordering, and `send_task` future resolution are never exercised against a real subprocess (and C4 above is real).
- **Backends' wire format untested end-to-end** — H3 and H4 were invisible because tests mock streams with well-formed shapes (`conftest.py:209-211`). H4 now has streaming tests; H3 is covered by `tests/test_tools_harness.py` (schema declaration + Anthropic wire alternation).
- `tests/test_tools_shell.py:143` — `assert "�" in result or result` is tautological.
- `ci.yml:34-38` builds a wheel but `uv run pytest` re-syncs from pyproject → tests run against source, the **wheel is never exercised**; `pytest-cov`/`pytest-xdist` installed but unused; integration/slow/e2e markers run on every PR with no deselect and no coverage gate.
- **No CI job exercises the install scripts** (no shellcheck / PSScriptAnalyzer / smoke run); `publish.yml` installs an unpinned `twine`.
- `tests/test_main.py:41-48` and `test_ui_app.py:361-365` assert nothing (implicit no-raise tests).

---

## 6. What's Good

- `slife/threads.py:run_daemon` is exactly the documented convention (daemon thread + `call_soon_threadsafe`, closed-loop guard); no `run_in_executor` remains anywhere in `slife/` — the exit-hang fix is clean and the OAuth notification reuses it.
- Secret sanitization is a disciplined known-shape allowlist threaded through `drain_stderr`; `SessionFormatter` + contextvars is async-safe.
- Atomic registry writes (mkstemp + `Path.replace`) in `memfiles/token.py`; registry-based path resolution means no user-controlled path construction in the memfiles server.
- Watchdog infrastructure (backoff, tool unregistration by `{name}__`, health records, `_stopping` guard) is genuinely present and now actually restarts every plugin (H5).
- Serial inbox with unconditional turn persistence in `finally` — memory survives cancel/error/max-iterations, matching DESIGN.
- `terminate_process` escalating force + `_close_pipe_transports` shows careful Windows ProactorEventLoop handling; the subagent stderr write bypasses the Windows GBK codec crash correctly.
- `exec.py` uses `create_subprocess_exec` (no shell) for the non-shell tools; `credentials.py` never echoes secret values.

---

## 7. Repo Hygiene

- `.VSCodeCounter/` is regenerating and untracked (commit `4cef127` removed it) — the only untracked file in `git status`, and `.gitignore` doesn't cover it. Add it (`logs/` and `.coverage` are already ignored).
