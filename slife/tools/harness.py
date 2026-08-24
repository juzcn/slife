"""Harness tools — context status.

These are real, schema-declared tools (so the Anthropic / OpenAI-Responses
backends' history validation accepts them), auto-invoked by the loop on the
harness's behalf via ``AgentLoop._auto_invoke`` — the system prompt forbids
the LLM from calling them.  ``_sys_note`` is pure (only reads state).

Context trimming no longer lives here: it runs internally after each turn
is persisted (``AgentLoop._trim_after_save``), marking the cut with a
runtime-only ``[TrimContext: N]`` footnote instead of a tool call.

One category per file: ``Harness`` lives here, next to ``exec.py``
(Execution), etc.  (The A2A tools live in the ``a2a`` plugin —
``plugins/a2a/server.py``.)

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
