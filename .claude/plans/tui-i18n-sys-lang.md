# TUI i18n via sys-lang (English + Chinese)

## Goal
Detect the OS language with `sys-lang`; render every user-facing TUI string in
English on non-Chinese systems and Chinese on Chinese systems. Today the TUI
mixes the two (Chinese system messages + English tool/approval/picker labels),
which reads as unpolished.

`sys-lang` is the right detector: on Windows it queries `Get-Culture` via
PowerShell (the only path that actually works — env vars are unset and
`locale.getlocale()` returns `None` on a default Windows install), and on
*nix it reads `LC_ALL`/`LC_MESSAGES`/`LANG`/`LANGUAGE`. Zero dependencies.

## Design

### 1. New module: `slife/ui/i18n.py`

A single translation table + a `t(key, **fmt)` accessor. Two languages only
(`en`, `zh`); no YAML, no catalogs, no Pydantic — the whole point of picking
`sys-lang` over `kiarina-i18n` was to keep this trivial.

```python
"""TUI internationalization — English + Chinese, detected via sys-lang."""

from __future__ import annotations

from sys_lang import get_sys_lang

# Resolved once at import: "zh" for any Chinese locale (zh, zh-CN, zh-HK,
# zh-TW …), else "en".  Overridable from tests via set_language().
_LANGUAGE: str = "zh" if get_sys_lang(region=False).lower().startswith("zh") else "en"

def set_language(lang: str) -> None:
    """Override the active language (tests / future config)."""
    global _LANGUAGE
    _LANGUAGE = lang

def get_language() -> str:
    return _LANGUAGE

# key -> {lang: text}.  Every user-facing string lives here.  Text may carry
# {placeholders} for str.format().  Emoji/status glyphs (✗ ⚠ ✅ ⏹ 🔌 ⚡ 📅 …)
# stay in the string — they are universal, not localized words.
_STRINGS: dict[str, dict[str, str]] = {
    "autonomous_prefix":   {"en": "⚡ autonomous: ", "zh": "⚡ 自主: "},
    "schedule_prefix":    {"en": "📅 scheduled: ",  "zh": "📅 定时: "},
    "interrupted":        {"en": "⏹ Interrupted",  "zh": "⏹ 已中断"},
    "restore_failed":     {"en": "✗ Restore failed: {err}", "zh": "✗ 恢复失败: {err}"},
    "restored_partial":   {
        "en": "✅ Restored exit-time context ({n} turns; {skipped} earlier turns not loaded — use turn_search to find them)",
        "zh": "✅ 已恢复退出时的上下文（{n} 轮；{skipped} 轮更早记录未载入，可用 turn_search 查找）",
    },
    "restored_ok":        {"en": "✅ Restored exit-time context, continue", "zh": "✅ 已恢复退出时的上下文，继续吧"},
    "memory_broken":       {
        "en": "✗ Memory save failed: {err}\nMemory is a core feature — new messages are paused. Fix the database and restart.",
        "zh": "✗ 记忆保存失败: {err}\n记忆是核心功能 — 已停止处理新消息,请修复数据库后重启。",
    },
    "plugin_start_failed":{"en": "⚠ Plugin start failed ({name}): {err}", "zh": "⚠ 插件启动失败 ({name}): {err}"},
    "plugin_ready":       {"en": "🔌 Plugin ready: {name}", "zh": "🔌 插件已就绪: {name}"},
    "plugin_ready_degraded":{"en":"🔌 Plugin ready (degraded): {name}{detail}","zh":"🔌 插件已就绪（降级）: {name}{detail}"},
    "plugin_skipped":     {"en": "ℹ️ Plugin not started: {name}", "zh": "ℹ️ 插件未启动: {name}"},
    "plugin_ready_failed":{"en": "⚠ Plugin ready failed: {name}", "zh": "⚠ 插件就绪失败: {name}"},
    "required_failed":    {
        "en": "✗ Required component failed: {name} ({reason})\n{name} is a core system component — cannot run without it. Fix and restart.",
        "zh": "✗ 必要组件加载失败: {name}（{reason}）\n{name} 是系统核心组件 — 无法在缺少它的状态下运行，请修复后重启。",
    },
    "memdb_unavailable":  {
        "en": "✗ Memory database unavailable: {err}\nMemory is a core feature — fix the database and restart.",
        "zh": "✗ 必要组件加载失败: memdb（{err}）\nmemdb 是系统核心组件 — 无法在缺少它的状态下运行，请修复后重启。",
    },
    # ── approval_prompt.py ──
    "approval_requests":  {"en": " requests approval", "zh": " 请求批准"},
    "approval_more":      {"en": "… {n} more", "zh": "… 还有 {n} 项"},
    "approval_approve":   {"en": "Approve", "zh": "批准"},
    "approval_deny":      {"en": "Deny", "zh": "拒绝"},
    "approval_approved":  {"en": "✓ Approved", "zh": "✓ 已批准"},
    "approval_denied":    {"en": "✗ Denied", "zh": "✗ 已拒绝"},
    # ── model_picker.py ──
    "picker_title":      {"en": "Switch model", "zh": "切换模型"},
    "picker_select":      {"en": "Select", "zh": "选择"},
    "picker_pick":        {"en": "Pick", "zh": "确认"},
    "picker_cancel":      {"en": "Cancel", "zh": "取消"},
    "picker_switched":    {"en": "✓ Switched", "zh": "✓ 已切换"},
    "picker_canceled":    {"en": "✗ Canceled", "zh": "✗ 已取消"},
    "no_models":          {"en": "No models configured.", "zh": "未配置任何模型。"},
    # ── tool_display.py ──
    "td_arguments":       {"en": "Arguments", "zh": "参数"},
    "td_result":          {"en": "Result", "zh": "结果"},
    "td_error":           {"en": "Error", "zh": "错误"},
    "td_no_args":         {"en": "(no arguments)", "zh": "（无参数）"},
    "td_more_lines":      {"en": "… {n} more lines …", "zh": "… 还有 {n} 行 …"},
    "td_running":         {"en": "running", "zh": "运行中"},
    "td_done":            {"en": "done", "zh": "完成"},
    "td_error_label":     {"en": "error", "zh": "错误"},
    "td_pending":         {"en": "pending", "zh": "等待"},
    # ── chat.py ──
    "thinking_collapsed": {"en": "Thinking ({n} chars)", "zh": "思考（{n} 字）"},
    "thinking_expanded":  {"en": "Thinking…", "zh": "思考…"},
    # ── status bar / placeholder ──
    "status_starting":    {"en": "⏳ starting…", "zh": "⏳ 启动中…"},
    "status_processing":  {"en": "⏳ processing", "zh": "⏳ 处理中"},
    "status_queued":      {"en": "⏳ {n} queued", "zh": "⏳ {n} 个排队中"},
    "input_placeholder":  {"en": "Message Slife…", "zh": "给 Slife 发消息…"},
    # ── handler.py ──
    "max_iterations":     {"en": "✗ Agent exceeded maximum of {n} iterations", "zh": "✗ 已达最大迭代次数 {n}"},
}

def t(key: str, **fmt) -> str:
    """Return the localized string for *key*, formatted with *fmt*.

    Missing keys raise KeyError (never silently fall back — a typo'd key
    is a bug we want to catch).  Unknown format fields are passed through
    untouched via safe_substitute-style behavior?  No — str.format raises
    on missing fields; keep it strict so typos surface at the call site.
    """
    entry = _STRINGS[key]
    text = entry.get(_LANGUAGE, entry["en"])
    return text.format(**fmt) if fmt else text
```

Notes:
- Emoji/glyphs (✗ ⚠ ✅ ⏹ 🔌 ⚡ 📅 ▸ ▾ ● ◌ ↑/↓) are **not** localized.
- Tool-name friendly labels (`Web search`, `Run command`) stay English-derived
  (`tool_name.replace("_"," ").capitalize()`) — they're tool identifiers, not
  prose; translating them would mismatch the tool's actual name and confuse the
  user. `_friendly_label` is untouched.
- `restore_prefix` ("You> ", "You(Wechat)> ") stays — it's a structural label.

### 2. Dependency

Add `sys-lang>=1.0.1` to `pyproject.toml` `[project].dependencies`.

### 3. Wire-up (6 UI files)

Replace each hardcoded string with `t(key, …)`:

- **app.py** — `⚡ 自主: ` / `📅 定时: ` (2), `⏹ 已中断`, memory-broken,
  plugin start/ready/skipped/failed (5), required-failed, memdb-unavailable,
  status-bar `starting…`/`processing`/`queued`, input placeholder.
- **restore.py** — `📅 定时: `/`⚡ 自主: `, `✗ 恢复失败`, the two `✅ 已恢复…`
  messages.
- **approval_prompt.py** — "requests approval", "… N more", Approve/Deny
  labels, ✓ Approved / ✗ Denied.
- **model_picker.py** — "Switch model", Select/Pick/Cancel, ✓ Switched /
  ✗ Canceled, "No models configured."
- **tool_display.py** — `_STATUS_LABEL` dict → driven through `t()` (running/
  done/error/pending), Arguments/Result/Error section headers, "(no
  arguments)", "… N more lines …".
- **chat.py** — "Thinking ({n} chars)", "Thinking…", usage line ("tokens").
- **handler.py** — max-iterations message.

Status-bar keybind hint (`Ctrl+C quit  Esc cancel …`) — translate to Chinese
key names? **No.** Key caps (`Ctrl`, `Esc`, `Home`, `End`) are universal;
translating them to "退出/取消" breaks the "key → action" scan. Leave English.

### 4. Test coupling

- **conftest.py** — add an `autouse` fixture that calls `set_language("en")`
  before each UI test.  The module-level `_LANGUAGE` is computed at import
  from the real OS; on the dev's Chinese Windows that's `"zh"`, which would
  flip every UI string and break ~30 assertions that check English text
  (`"Arguments"`, `"Thinking"`, `"Switched"`, `"running"`, …).  Pinning to
  `en` keeps the existing assertions valid and matches the "tests are in
  English" convention.
- **test_ui_restore.py** — the two assertions on Chinese strings are for
  *schedule-trigger content the agent produced* (`"已派发定时任务。"`,
  `"[Schedule …] 定时任务触发。"`), not UI chrome — those are test data,
  unaffected by i18n.  The `"📅 定时: "` prefix assertion (line 326) is
  updated to use `t("schedule_prefix")` so it tracks the fixture language.
- No new tests strictly required, but add one small test
  (`tests/test_ui_i18n.py`) asserting `t()` returns English for `en` and
  Chinese for `zh` on a couple of keys + that `set_language` overrides —
  guards the contract cheaply.

### 5. Out of scope
- Non-UI strings (agent loop, plugin internals, log messages) stay as-is —
  per [[log-terminal-tui-separation]], logs are for devs and stay English.
- No config-file language override (set_language is test-only for now; a
  `slife.json5` `"language"` key can be added later if needed).

## Implementation order
1. Add `sys-lang` to pyproject + `uv sync`.
2. Write `slife/ui/i18n.py`.
3. Wire app.py + restore.py (the Chinese system messages).
4. Wire approval_prompt.py + model_picker.py (the two TUI interaction tools).
5. Wire tool_display.py + chat.py + handler.py.
6. Add conftest autouse fixture + the tiny i18n test.
7. Run the full UI test suite → green.
