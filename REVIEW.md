# Slife Code Review

**Date:** 2026-08-06 · **Scope:** full codebase (~23k lines: `slife/`, `credstore/`, `skills/`, install scripts, tests) · **Method:** subsystem-by-subsystem audit of agent core, tool system, plugins/MCP, A2A/subagent, UI/platform/credstore, cross-checked against README/DESIGN claims.

**Verdict:** the architecture is coherent and unusually disciplined for its size — one loop, one registry, one inbox, uniform tool surface. The issues below are mostly accretions from fast iteration: stale docstrings, dead code, one non-functional transport shipped as if functional, missing resilience (no plugin restart), and global-state coupling. Nothing structural needs rework.

This review accompanies a documentation rewrite (`README.md`, `README.zh-CN.md`, `DESIGN.md`); doc drift found during the review is listed in §4 and already fixed in those files.

---

## 1. Bugs & Correctness Issues

Ordered by severity.

### B1 — A2A `transport: "http"` is configured-then-crashes (high)

`HttpStreamableTransport` implements only `connect()`/`disconnect()`; `publish()`, `subscribe()`, and `messages()` raise `NotImplementedError` (`slife/a2a/http.py:181,195,208`). Nothing validates `mqtt.transport` at config-load time, so a user who sets `transport: "http"` gets a runtime crash on first task send instead of a clear startup error.
**Fix:** reject `transport: "http"` in `A2AConfig.from_dict` with an explicit "not implemented yet" error, or gate it behind an experimental flag. (Longer term: implement or delete — see §3.)

### B2 — Image attachments fail silently (high)

`prepare_image_url()` returns `None` when a file doesn't exist or can't be read, and `Conversation.add_user_message()` silently skips `None` blocks (`slife/agent/multimodal.py:19-49`, `conversation.py:137-143`). A typo in `@D:\Downloads\eror.png` produces **no feedback at all** — the message is sent as plain text and the user assumes the model saw the image.
**Fix:** when an attachment can't be resolved, surface a visible warning (TUI system message + a note appended to the user message so the LLM knows an attachment was dropped).

### B3 — No recovery when a plugin or external MCP server dies (high)

Two related gaps:

1. Built-in plugins (`mcp`, `memdb`, `wechat`, `memfiles`) are spawned once via `MCPWrapperProcess`; there is no watchdog — a crashed plugin stays dead for the rest of the session while its proxy tools remain registered and return errors on every call.
2. The external-MCP gateway performs no health checks and no reconnection. `MCPClient.ping()` exists but is never scheduled; a stdio server that exits remains `CONNECTED` in `mcp_list_servers` output.

**Fix:** add a watchdog task per `PluginLifecycle` (restart on process exit, re-register tools); in `MCPServerConnection.call_tool`, attempt one reconnect on transport errors before returning the error string; refresh status on failure.

### B4 — Subagent results are attributed to the `human` channel (medium)

`on_task_complete` posts subagent completion messages with `source=HUMAN` (`slife/agent/service.py:1397-1406`). They land in the human conversation and are persisted to the diary with `channel = "human"`, making it impossible to distinguish delegated work from actual user turns in `memory_search`/audit.
**Fix:** introduce a `subagent` source (as `HUMAN`/`WECHAT` sentinels already exist in `slife/a2a/identity.py`) and route it to the human conversation explicitly.

### B5 — Secret sanitization is a known-shape allowlist, not a guarantee (medium)

`sanitize_secrets()` (`slife/logfmt.py:294-315`) matches a curated pattern list: provider prefixes (`sk-`, `AIza…`, `fw_`, `nvapi-`, `bce-v3/ALTAK-`, `gh[psu]_`, `ya29.`, `pypi-`), `Authorization: Bearer` headers, and `key=value` pairs with credential-like names (`api_key`, `secret`, `token`, `password`, `auth_token`). The code explicitly opts out of generic hex/blob heuristics — a deliberate choice that avoids false positives (git SHAs, hashes pass through untouched), but means:

- Any secret not matching a known shape — custom tokens, short keys, novel providers — passes straight into the LLM context and the diary.
- The `key=value` pattern masks on *name* alone (`token=<anything>`), so non-secret values assigned to credential-named variables get masked — a mild false-positive path.

The old documentation claim "secrets never reach the LLM" overstates what pattern matching can promise.
**Fix:** (a) keep the documentation phrased as "known secret patterns are masked" (done in this update); (b) consider letting `credstore` supply the user's actually-stored values as an exact-match denylist — precise, zero false positives/negatives for stored secrets, complements the pattern list.

### B6 — Misleading subagent error message (low)

Three subagent tools return "Subagent support is not enabled. Add a `[subagent]` section to slife.json5." when `get_manager()` is `None` (`slife/tools/a2a.py:141,659,710`). But `AgentService.start_subagent()` creates the manager **unconditionally** in the main process — the `[subagent]` section is only optional tuning (`max_subagents`/`task_timeout` defaults apply without it). The only real cases where the manager is absent are: running inside a subagent process itself (`SLIFE_SUBAGENT_NAME` recursion guard — intentional) or calling before service startup. Adding the section fixes neither.
**Fix:** rewrite the message per case — "not available inside a subagent" vs. "service not ready"; mention the config section only as an optional tuning knob.

### B7 — Stale docstrings contradict the code (low)

- `slife/plugins/memfiles/__init__.py` describes "HMAC-signed tokens"; the implementation since the sharing simplification is plain random hex tokens (`secrets.token_hex(15)`, `slife/memfiles/token.py`).
- `Conversation.add_user_message`'s docstring says images are "shared via a signed URL served by the memfiles server"; the code base64-encodes local files (`conversation.py:119-121` vs `prepare_image_url`).

Both are leftovers of reverted refactors (commits `caf1676` ↔ `c43d10d`, `fe9304b` ↔ `a00ba2f`). **Fix:** rewrite both docstrings to match current behavior.

### B8 — Dead code (low)

- `Conversation.trim_context()` (`conversation.py:364-423`) was superseded by `extract_oldest_turns()` and is no longer called by the loop. Remove it (and any tests targeting only it) to avoid future edits landing in the wrong method.
- `slife/a2a/tools.py` is a back-compat shim re-exporting `slife.tools.a2a`; its only importer is `slife/a2a/__init__.py` itself. Nothing external is known to depend on it — collapse it into `__init__` or delete with the re-exports inlined.

### B9 — Mixed languages in user-facing strings (low)

Tool-layer error strings are inconsistently localized: the MCP client returns Chinese errors (`工具 '{name}' 执行失败`, `slife/mcp/client.py:228`), the approval-denial message is Chinese (`loop.py:576`), and the subagent-completion relay posted into the inbox is Chinese (`service.py:1401`) — while most native tools answer in English. Pick one language for machine-generated tool results (English recommended — it's also easier for the LLM to reason about) and keep Chinese for UI chrome only.

### B10 — Correlation with HARDISSUES.md

The open issues file is consistent with what this review found:

- **#1 (input focus loses content):** resolved 2026-08-06 — CSS box-model bug, not state loss. `#user-input` declared `height: 3` under Textual's default `border-box` sizing plus `padding: 1 1` (2 rows); the unfocused `border-top` consumed the last row, collapsing the content region to **0 rows** so the intact `value` had nowhere to render (verified via headless pilot: `content=78x0` blurred vs `78x1` focused). Fixed in `slife/ui/slife.tcss`: the `border-top` slot is now occupied in both focus states (invisible when focused), so geometry is identical — no collapse, no shift. Incidental finding: Textual 8 does not stack same-edge docks — the StatusBar overlaps the input's bottom row, which must therefore remain a blank spare row.
- **#4 (Esc during tool execution):** confirmed by design — the cancel event is checked *before* each iteration, *after* each stream, and *before* each tool batch, but not inside running tools. A long `execute_shell` runs to its own timeout even after Esc. The documented decision ("决定放弃") is defensible given the `_timeout` escape hatch, but the `asyncio.gather` batch means one slow tool delays cancellation of the whole turn; consider `gather` with early cancel propagation.

---

## 2. Design Concerns

Not bugs, but structural debt that will compound.

### D1 — Module-level singletons as implicit wiring

`set_conversation()`/`get_conversation()`, `get_registry()`, `_rest_api_mcp_client` (`tools/rest_api.py`), `_tasks` (`tools/meta.py`), `_tunnel` (`memfiles/tunnel.py`) — tools reach process-global state instead of receiving a context object. Consequences: only one agent service per process, tests must monkeypatch globals, and initialization order is implicit.
**Suggestion:** thread a small `ToolContext` (conversation, registry, service refs) through `from_config()`; keep the globals only as a migration bridge.

### D2 — Magic-string dispatch in `MCPProxyTool`

Execution routing switches on the server *name* being exactly `"mcp"`, `"memdb"`, or `"wechat"` (`slife/mcp/tool_adapter.py:110-155`). Plugin identity and call routing are conflated; renaming a plugin silently changes dispatch.
**Suggestion:** give each plugin client a registered route (callback or enum) instead of string-matching names.

### D3 — Config parsed in two places

`rest_apis` and `cli_tools` are read ad-hoc from `slife.json5` by the tools themselves (`_read_config()`), bypassing the `Config` dataclass. There is no single source of truth, no validation, and concurrent writers (config tools + tools modules) race on the file.
**Suggestion:** parse all sections in `Config.from_json5`; hand immutable snapshots to tools; funnel all writes through one writer (the `_config_io` helpers already exist).

### D4 — Harness-only tools filtered by name blocklist

`memory_save_turn`/`memory_get_recent_turns` and `wechat_drain_incoming`/`wechat_dispatch_reply` are hidden from the LLM by hardcoded name filters in `service.py` (~:864-891). A renamed or new harness tool leaks into the LLM's surface silently.
**Suggestion:** mark tools harness-only at the source (FastMCP tool metadata or a naming convention enforced by a test).

### D5 — Approval gate covers only external MCP tools

`requires_approval` exists solely for external MCP servers. Native high-impact tools (`execute_shell`, `install_python_package`, `add_skill` from arbitrary archives) cannot be gated at all. Given the "minimum harness" philosophy this is a deliberate default-off stance, but the *capability* should be uniform.
**Suggestion:** honor a per-tool `require_approval` in the `tools:` config overrides for native tools too (the loop already checks `getattr(tool, 'requires_approval')`).

### D6 — Naming inconsistency across managed categories

Three word orders coexist: `list_models` (verb-noun), `cli_list_tools` (prefix-verb-noun), `rest_api_list` (prefix-noun-verb), `mcp_list_servers` (prefix-verb-noun). The LLM copes, but discoverability and muscle memory suffer.
**Suggestion:** standardize on `prefix_verb_noun` (`model_list`, `skill_list`, …) at the next breaking release, with aliases for one version.

### D7 — `switch_to_nvidia_free` is vendor logic in the core

A native tool hardcodes queries against the `nvidia-nim` MCP server (`tools/models.py:443+`). One vendor's free-tier workflow doesn't belong in core tool surface.
**Suggestion:** ship it as a skill (SKILL.md + script) using the generic `switch_model` in-memory variant, or generalize to a `switch_model_temporary` tool.

### D8 — Serial inbox with no prioritization

One FIFO queue, one turn at a time. A long human coding task blocks WeChat auto-replies and A2A results indefinitely (only "⏳ N queued" in the status bar). This is the right default for coherence, but worth a knob.
**Suggestion (later):** optional per-source limits or a lightweight "busy — will respond later" auto-ack for remote sources.

### D9 — Volatile A2A task store

`TaskRecord` state is in-memory only and lost on restart (documented now, but users will still be surprised when `a2a_list_tasks` empties). Since memdb's SQLite file is already per-agent, persistence is nearly free.
**Suggestion:** optionally persist task records in the agent DB (or document the volatility at the tool description level — the `a2a_list_tasks` description should say "this session only").

### D10 — ngrok free-tier URL volatility

Every restart gets a new `*.ngrok-free.dev` URL; previously shared links die. Fine for ephemeral vision use, painful for `save_content_or_files` "persistent" storage.
**Suggestion:** document prominently; support ngrok paid stable domains (SDK already honors them) and/or an alternative tunnel provider behind the same `Tunnel` interface.

---

## 3. Improvement Roadmap

**Quick wins (days):**
1. B1 guard: fail fast on `transport: "http"` at config load.
2. B2: visible warning for unresolvable image attachments.
3. B7/B8: fix stale docstrings, delete `trim_context()`.
4. B6: rewrite the subagent error message.
5. B9: unify tool-result language to English.
6. D9-lite: add "this session only" to `a2a_list_tasks` description.

**Medium term (weeks):**
7. B3: plugin watchdog + external MCP reconnect-on-failure.
8. B5: exact-match denylist from credstore + narrower hex pattern.
9. D3: consolidate all config sections into `Config`.
10. D4: flag-based harness-tool filtering.
11. B4: `subagent` channel attribution.
12. D5: optional approval for native tools via `tools:` overrides.

**Larger bets (when the above is quiet):**
13. D1: replace module singletons with a `ToolContext`.
14. Implement or delete the HTTP A2A transport (B1's root cause).
15. D6: managed-tool naming standardization with aliases.
16. D7: move vendor-specific model switching to a skill.
17. Token counting: use a real tokenizer when the provider exposes one (tiktoken/Anthropic count API), keep chars÷3 as fallback.
18. Tests/CI: add node to CI images and run the `e2e` MCP filesystem test; consider a coverage floor (unit coverage is broad — 57 test files — but e2e is a single test).

---

## 4. Documentation Drift Found (fixed in this update)

For the record — claims in the previous README/DESIGN that contradicted the code:

| Old claim | Reality |
|-----------|---------|
| MemFiles tool `include_image` | Renamed to `prepare_image` (commit `a00ba2f`) |
| Meta category = `list_tools` only | 4 tools: `list_tools`, `check_async`, `cancel_async`, `clear_context` |
| Five managed categories incl. **Native** | Native isn't managed (only `native_tool_set`); **Models** is |
| Synthetic tools `_trim_context` / `_context_status` | Actually `_sys_trim` / `_sys_note` |
| "Images via HTTPS URLs through the memfiles tunnel — no base64 in context" | Local files are base64 data URIs; tunnel URLs only via `expose_file` |
| `Ctrl+C` copies outside the input | `Ctrl+C` quits unconditionally; copy is `Ctrl+Y` on tool calls |
| "No approval gates" | Opt-in gate exists per external MCP server (`require_approval`) |
| Agent isolation via `author` column / vec0 partition | Per-agent DB file (`~/.slife/<agent>.db`); no `author` column |
| Ngrok monitor "polls every 15 s" | One-shot monitor check with a single retry; initial start retries 3× |
| slife-mcp "health-check" | No health checks or reconnect (see B3) |
| "Three transports" (equal) | HTTP transport is a connect-only skeleton (see B1) |
| GGUF embedding default "Q4_K_M" | Quantization is whatever file the user provides; not code-specified |

Also fixed: README tool table now lists all 12 categories with accurate names, and the MemDB tool list was completed.
