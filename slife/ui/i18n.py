"""TUI internationalization — English + Chinese, detected via sys-lang.

A single translation table plus a :func:`t` accessor.  Two languages only
(``en`` / ``zh``) — no YAML, no catalogs, no Pydantic.  The point of using
``sys-lang`` over a full i18n framework is to keep this trivial: the whole
TUI surface is ~40 strings.

Language is resolved once at import from the OS locale (Chinese → ``zh``,
everything else → ``en``).  Tests override it via :func:`set_language` —
the autouse fixture in ``conftest.py`` pins it to ``en`` so existing English
assertions stay valid regardless of the dev machine's locale.
"""

from __future__ import annotations

from sys_lang import get_sys_lang


def _detect_language() -> str:
    """``"zh"`` for any Chinese locale, else ``"en"``.

    ``sys-lang`` returns an ISO code like ``zh`` / ``zh_CN`` / ``zh_HK``;
    any ``zh*`` prefix counts as Chinese (Simplified / Traditional / region
    variants share the same zh strings here).  Detection never raises —
    ``get_sys_lang`` falls back to ``en`` internally on failure.
    """
    try:
        code = get_sys_lang(region=False).lower()
    except Exception:
        return "en"
    return "zh" if code.startswith("zh") else "en"


# Resolved once at import; overridable from tests via set_language().
_LANGUAGE: str = _detect_language()


def set_language(lang: str) -> None:
    """Override the active language (tests / future config)."""
    global _LANGUAGE
    _LANGUAGE = lang


def get_language() -> str:
    """Return the active language code (``"en"`` or ``"zh"``)."""
    return _LANGUAGE


# key → {lang: text}.  Every user-facing TUI string lives here.  Text may
# carry {placeholders} consumed by str.format.  Emoji / status glyphs
# (✗ ⚠ ✅ ⏹ 🔌 ⚡ 📅 ▸ ▾ ● ◌ ↑/↓) stay in the string — they are universal
# symbols, not localized words.
_STRINGS: dict[str, dict[str, str]] = {
    # ── Chat stream prefixes & system messages (app.py, restore.py) ──
    "autonomous_prefix": {
        "en": "⚡ autonomous: ",
        "zh": "⚡ 自主: ",
    },
    "schedule_prefix": {
        "en": "📅 scheduled: ",
        "zh": "📅 定时: ",
    },
    "interrupted": {
        "en": "⏹ Interrupted",
        "zh": "⏹ 已中断",
    },
    "restore_failed": {
        "en": "✗ Restore failed: {err}",
        "zh": "✗ 恢复失败: {err}",
    },
    "restored_partial": {
        "en": "✅ Restored exit-time context ({n} turns; {skipped} earlier "
              "turns not loaded — use turn_search to find them)",
        "zh": "✅ 已恢复退出时的上下文（{n} 轮；{skipped} 轮更早记录未载入，"
              "可用 turn_search 查找）",
    },
    "restored_ok": {
        "en": "✅ Restored exit-time context, continue",
        "zh": "✅ 已恢复退出时的上下文，继续吧",
    },
    "memory_broken": {
        "en": "✗ Memory save failed: {err}\n"
              "Memory is a core feature — new messages are paused. "
              "Fix the database and restart.",
        "zh": "✗ 记忆保存失败: {err}\n"
              "记忆是核心功能 — 已停止处理新消息,请修复数据库后重启。",
    },
    "plugin_start_failed": {
        "en": "⚠ Plugin start failed ({name}): {err}",
        "zh": "⚠ 插件启动失败 ({name}): {err}",
    },
    "plugin_ready": {
        "en": "🔌 Plugin ready: {name}",
        "zh": "🔌 插件已就绪: {name}",
    },
    "plugin_ready_degraded": {
        "en": "🔌 Plugin ready (degraded): {name}{detail}",
        "zh": "🔌 插件已就绪（降级）: {name}{detail}",
    },
    "plugin_skipped": {
        "en": "ℹ️ Plugin not started: {name}",
        "zh": "ℹ️ 插件未启动: {name}",
    },
    "plugin_ready_failed": {
        "en": "⚠ Plugin ready failed: {name}",
        "zh": "⚠ 插件就绪失败: {name}",
    },
    "required_failed": {
        "en": "✗ Required component failed: {name} ({reason})\n"
              "{name} is a core system component — cannot run without it. "
              "Fix and restart.",
        "zh": "✗ 必要组件加载失败: {name}（{reason}）\n"
              "{name} 是系统核心组件 — 无法在缺少它的状态下运行，请修复后重启。",
    },
    "memdb_unavailable": {
        "en": "✗ Memory database unavailable: {err}\n"
              "Memory is a core feature — fix the database and restart.",
        "zh": "✗ 必要组件加载失败: memdb（{err}）\n"
              "memdb 是系统核心组件 — 无法在缺少它的状态下运行，请修复后重启。",
    },

    # ── approval_prompt.py ──
    "approval_requests": {
        "en": " requests approval",
        "zh": " 请求批准",
    },
    "approval_more": {
        "en": "… {n} more",
        "zh": "… 还有 {n} 项",
    },
    "approval_approve": {
        "en": "Approve",
        "zh": "批准",
    },
    "approval_deny": {
        "en": "Deny",
        "zh": "拒绝",
    },
    "approval_approved": {
        "en": "✓ Approved",
        "zh": "✓ 已批准",
    },
    "approval_denied": {
        "en": "✗ Denied",
        "zh": "✗ 已拒绝",
    },

    # ── model_picker.py ──
    "picker_title": {
        "en": "Switch model",
        "zh": "切换模型",
    },
    "picker_select": {
        "en": "Select",
        "zh": "选择",
    },
    "picker_pick": {
        "en": "Pick",
        "zh": "确认",
    },
    "picker_cancel": {
        "en": "Cancel",
        "zh": "取消",
    },
    "picker_switched": {
        "en": "✓ Switched",
        "zh": "✓ 已切换",
    },
    "picker_canceled": {
        "en": "✗ Canceled",
        "zh": "✗ 已取消",
    },
    "no_models": {
        "en": "No models configured.",
        "zh": "未配置任何模型。",
    },

    # ── tool_display.py ──
    "td_arguments": {
        "en": "Arguments",
        "zh": "参数",
    },
    "td_result": {
        "en": "Result",
        "zh": "结果",
    },
    "td_error": {
        "en": "Error",
        "zh": "错误",
    },
    "td_no_args": {
        "en": "(no arguments)",
        "zh": "（无参数）",
    },
    "td_more_lines": {
        "en": "… {n} more lines …",
        "zh": "… 还有 {n} 行 …",
    },
    "td_running": {
        "en": "running",
        "zh": "运行中",
    },
    "td_done": {
        "en": "done",
        "zh": "完成",
    },
    "td_error_label": {
        "en": "error",
        "zh": "错误",
    },
    "td_pending": {
        "en": "pending",
        "zh": "等待",
    },

    # ── chat.py ──
    "thinking_collapsed": {
        "en": "Thinking ({n} chars)",
        "zh": "思考（{n} 字）",
    },
    "thinking_expanded": {
        "en": "Thinking…",
        "zh": "思考…",
    },

    # ── status bar / input placeholder (app.py) ──
    "status_starting": {
        "en": "⏳ starting…",
        "zh": "⏳ 启动中…",
    },
    "status_processing": {
        "en": "⏳ processing",
        "zh": "⏳ 处理中",
    },
    "status_queued": {
        "en": "⏳ {n} queued",
        "zh": "⏳ {n} 个排队中",
    },
    "input_placeholder": {
        "en": "Message Slife…",
        "zh": "给 Slife 发消息…",
    },
    "status_keybinds": {
        "en": "│ Ctrl+C quit  Esc cancel  Ctrl+S model  Home/End scroll",
        "zh": "│ Ctrl+C 退出  Esc 取消  Ctrl+S 模型  Home/End 滚动",
    },

    # ── handler.py ──
    "max_iterations": {
        "en": "✗ Agent exceeded maximum of {n} iterations",
        "zh": "✗ 已达最大迭代次数 {n}",
    },

    # ── A2A activity (app.py) ──
    "task_completed": {
        "en": "✓ task from {source} completed",
        "zh": "✓ 来自 {source} 的任务已完成",
    },
    "loop_error": {
        "en": "✗ {err}",
        "zh": "✗ {err}",
    },
    "turn_error": {
        "en": "✗ Error: {err}",
        "zh": "✗ 错误: {err}",
    },

    # ── notify_user tool + OAuth desktop notification ──
    # The notification *body* is LLM-/system-supplied, not localized.  Only
    # the fixed framing strings are translated.
    "notify_default_title": {
        "en": "slife",
        "zh": "slife",
    },
    "notify_oauth_title": {
        "en": "slife — OAuth authorization required",
        "zh": "slife — 需要进行 OAuth 授权",
    },
    "notify_sent": {
        "en": "Notification sent: [{title}] {message}",
        "zh": "已发送通知：[{title}] {message}",
    },
}


def t(key: str, **fmt: object) -> str:
    """Return the localized string for *key*, formatted with *fmt*.

    Missing keys raise ``KeyError`` — never silently fall back, so a typo'd
    key surfaces at the call site rather than rendering an empty/blank slot
    in the TUI.  ``str.format`` is always applied so a caller who forgets a
    placeholder (e.g. ``t("restore_failed")`` without ``err=``) raises
    ``KeyError`` at the call site instead of silently rendering ``{err}``
    in the TUI.  Strings without placeholders format to themselves.
    """
    entry = _STRINGS[key]
    text = entry.get(_LANGUAGE, entry["en"])
    return text.format(**fmt)
