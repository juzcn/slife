"""Harness tools — context status and context trimming.

These are real, schema-declared tools (so the Anthropic / OpenAI-Responses
backends' history validation accepts them), auto-invoked by the loop on the
harness's behalf via ``AgentLoop._auto_invoke`` — the system prompt forbids
the LLM from calling them.  If the LLM ignores that and calls them anyway:
``_sys_note`` is pure (only reads state); ``_sys_trim`` genuinely compacts
the context to the floor — a legitimate action, not a no-op.

One category per file: ``Harness`` lives here, next to ``a2a.py`` (A2A),
``exec.py`` (Execution), etc.

Language policy: descriptions / parameter docs / result strings are English
(see DESIGN.md) — these are model-visible.
"""

from __future__ import annotations

from slife.tools.base import Tool, make_params

#: Optional render kwargs the loop passes to ``_sys_note`` each turn.
#: All optional — the tool degrades to a default status if called bare.
_SYS_NOTE_PARAMS = make_params(
    context_window={"type": "integer", "default": 0,
                    "description": "Context window size."},
    last_context_tokens={"type": "integer", "default": 0,
                         "description": "Context token count from the previous turn."},
    model_name={"type": "string", "default": "",
                "description": "Current model display name."},
    input_modalities={"type": "string", "default": "",
                      "description": "Model input modalities."},
    cwd={"type": "string", "default": "",
         "description": "Current working directory."},
    shell={"type": "string", "default": "",
           "description": "Current shell."},
    context_time_start={"type": "string", "default": "",
                        "description": "Context coverage start time."},
    presence_events={"type": "array", "default": [],
                     "description": "Peer online/offline events since the last poll."},
)


class SysNoteTool(Tool):
    """Report the current context status (time, usage %, tokens, peers)."""

    name = "_sys_note"
    category = "Harness"
    description = "Current context status: time, context usage %, token usage, peer online/offline events."
    parameters = _SYS_NOTE_PARAMS

    async def execute(self, **kwargs) -> str:
        # Strip harness meta params (_timeout/_async/_approve) the schema
        # injects on every tool — they are not build_context_status params.
        clean = {k: v for k, v in kwargs.items() if k not in ("_timeout", "_async", "_approve")}
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
    description = "Remove the oldest complete turns and compact the context down to the retention ratio (context_floor, default 20%) to free space."
    parameters = make_params(
        memory_saved={"type": "boolean", "default": True,
                      "description": "Whether the removed content was saved to the memory store."},
    )

    async def execute(self, **kwargs) -> str:
        ctx = getattr(self, "_ctx", None)
        if ctx is None or ctx.conversation is None or ctx.config is None:
            return "Error: _sys_trim missing runtime context (conversation/config)."
        conversation = ctx.conversation
        config = ctx.config
        context_window = config.active_model.context_window
        floor = config.context_floor
        memory_saved = bool(kwargs.get("memory_saved", True))

        target = int(context_window * floor)
        turns, tokens_freed = conversation.extract_oldest_turns(target)
        if not turns:
            return "No complete turns to trim."

        # ── Build human-readable summary ──────────────────────────
        summary_parts = []
        for idx, turn in enumerate(turns, 1):
            user_msg = turn.get("user_message", "(no text)")
            est = turn.get("estimated_tokens", 0)
            if len(user_msg) > 80:
                user_msg = user_msg[:80] + "..."
            summary_parts.append(
                f'- Turn {idx}: "{user_msg}" (~{est} tokens)'
            )
        turns_summary = "\n".join(summary_parts)
        if len(turns_summary) > 2000:
            turns_summary = turns_summary[:2000] + "\n... (summary truncated)"

        if memory_saved:
            head = (
                f"Trimmed {len(turns)} oldest turns (~{tokens_freed} tokens); "
                f"content saved to the memory store. Use memory_search to review."
            )
        else:
            head = (
                f"Trimmed {len(turns)} oldest turns (~{tokens_freed} tokens); "
                f"content discarded."
            )
        return f"{head}\n\n{turns_summary}" if turns_summary else head
