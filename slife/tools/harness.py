"""Harness tools — context status and context trimming.

These are real, schema-declared tools (so the Anthropic / OpenAI-Responses
backends' history validation accepts them), auto-invoked by the loop on the
harness's behalf via ``AgentLoop._auto_invoke`` — the system prompt forbids
the LLM from calling them.  If the LLM ignores that and calls them anyway:
``_sys_note`` is pure (only reads state); ``_sys_trim`` genuinely compacts
the context to the floor — a legitimate action, not a no-op.

One category per file: ``Harness`` lives here, next to ``a2a.py`` (A2A),
``exec.py`` (Execution), etc.
"""

from __future__ import annotations

from slife.tools.base import Tool, make_params

#: Optional render kwargs the loop passes to ``_sys_note`` each turn.
#: All optional — the tool degrades to a default status if called bare.
_SYS_NOTE_PARAMS = make_params(
    context_window={"type": "integer", "default": 0,
                    "description": "上下文窗口大小。"},
    last_context_tokens={"type": "integer", "default": 0,
                         "description": "上一轮上下文 token 数。"},
    model_name={"type": "string", "default": "",
                "description": "当前模型显示名。"},
    input_modalities={"type": "string", "default": "",
                      "description": "模型输入模态。"},
    cwd={"type": "string", "default": "",
         "description": "当前工作目录。"},
    shell={"type": "string", "default": "",
           "description": "当前 shell。"},
    context_time_start={"type": "string", "default": "",
                        "description": "上下文覆盖起始时间。"},
    presence_events={"type": "array", "default": [],
                     "description": "自上次轮询以来的 peer 上线/下线事件。"},
)


class SysNoteTool(Tool):
    """Report the current context status (time, usage %, tokens, peers)."""

    name = "_sys_note"
    category = "Harness"
    description = "当前上下文状态：时间、上下文占用百分比、token 用量、peer 上线/下线事件。"
    parameters = _SYS_NOTE_PARAMS

    async def execute(self, **kwargs) -> str:
        # Strip harness meta params (_timeout/_async) the schema injects on
        # every tool — they are not build_context_status parameters.
        clean = {k: v for k, v in kwargs.items() if k not in ("_timeout", "_async")}
        from slife.agent.system_prompt import build_context_status
        return build_context_status(**clean)


class SysTrimTool(Tool):
    """Trim the oldest complete turns down to the floor.

    Deliberately has **no** ceiling check: the loop decides *when* to trim
    (the gate lives outside the tool), and if the LLM calls it directly it
    genuinely compacts the context — a legitimate action, not a no-op.
    """

    name = "_sys_trim"
    category = "Harness"
    description = "移除最旧完整轮次，将上下文压缩到保留比例（context_floor，默认 20%）以释放空间。"
    parameters = make_params(
        memory_saved={"type": "boolean", "default": True,
                      "description": "被移除内容是否已存入记忆库。"},
    )

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        if ctx is None or ctx.conversation is None or ctx.config is None:
            return "Error: _sys_trim 缺少运行上下文（conversation/config）。"
        conversation = ctx.conversation
        config = ctx.config
        context_window = config.active_model.context_window
        floor = config.context_floor
        memory_saved = bool(kwargs.get("memory_saved", True))

        target = int(context_window * floor)
        turns, tokens_freed = conversation.extract_oldest_turns(target)
        if not turns:
            return "无可裁剪的完整轮次。"

        # ── Build human-readable summary ──────────────────────────
        summary_parts = []
        for idx, turn in enumerate(turns, 1):
            user_msg = turn.get("user_message", "(无文本)")
            est = turn.get("estimated_tokens", 0)
            if len(user_msg) > 80:
                user_msg = user_msg[:80] + "..."
            summary_parts.append(
                f'- 轮次{idx}: "{user_msg}" (约{est} tokens)'
            )
        turns_summary = "\n".join(summary_parts)
        if len(turns_summary) > 2000:
            turns_summary = turns_summary[:2000] + "\n...（摘要过长已截断）"

        if memory_saved:
            head = (
                f"已裁剪 {len(turns)} 个最旧轮次（约 {tokens_freed} tokens），"
                f"内容已存入记忆库，如需回顾请用 memory_search。"
            )
        else:
            head = (
                f"已裁剪 {len(turns)} 个最旧轮次（约 {tokens_freed} tokens），"
                f"内容已丢弃。"
            )
        return f"{head}\n\n{turns_summary}" if turns_summary else head
