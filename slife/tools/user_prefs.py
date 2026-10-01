"""user_pref_edit — USER.md, the standing user preferences.

USER.md is the per-agent File Cabinet file appended to the system prompt as
its final section, so **its contents are already in front of the model every
turn**.  That is why there is no read tool: a read would hand back the text
the model is looking at, and the section renders ``(Empty)`` when nothing is
recorded, so "what are the preferences?" is answered without a call.  One
verb, and the file is the interface.

It is a DOCUMENT, not a record store: ``write`` replaces it whole, so update
and remove are edits to text the model has read, not modes of an API.  There
is deliberately no append — a store the agent can only add to is one it can
neither review nor correct, which left a badly-worded preference fixable only
by hand-editing the file.

The write delegates to the memfiles plugin over the MCP client, so the plugin
stays USER.md's only host and its single writer, and refreshes the system
prompt on success so a change lands from the next API call.
"""

from __future__ import annotations

import json
from typing import ClassVar

from slife.agent.system_prompt import USER_PREFS_MAX_CHARS
from slife.tools.base import Tool, _MemfilesClientMixin, make_params, require_params


class UserPrefEditTool(_MemfilesClientMixin, Tool):
    """Replace the standing user preferences."""

    offline_message = (
        "Error: memfiles plugin not connected — user preferences are unavailable."
    )

    name = "user_pref_edit"
    category: ClassVar[str] = "System"

    description = (
        "Replace USER.md — the standing user preferences, held across sessions "
        "and rendered in the system prompt — with the given markdown. The whole "
        "file is overwritten and nothing is merged, so send the complete new "
        "contents, amending what the prompt shows."
    )
    parameters = make_params(
        content={
            "type": "string",
            "description": (
                "The file's complete new contents. A markdown list reads well, "
                "one preference per line."
            ),
        },
    )

    async def execute(self, content: str = "", **kwargs) -> str:
        if err := require_params(content=(content or "").strip()):
            return err
        # The prompt carries at most this much, and a write that exceeded it
        # would be silently cut on the way into every request.  Refusing here
        # is the one place the model can be told — and it is told the number.
        if len(content) > USER_PREFS_MAX_CHARS:
            return (
                f"Error: content is {len(content)} characters; the system "
                f"prompt carries at most {USER_PREFS_MAX_CHARS}. These are "
                f"standing preferences — shorten them."
            )
        raw = await self._call("__user_pref_edit", {"content": content})
        if not isinstance(raw, str):
            return "Error: unexpected memfiles plugin response."
        try:
            info = json.loads(raw)
        except ValueError:
            return raw
        if info.get("error"):
            return f"Error: {info['error']}"
        # A write is only visible once the session's system prompt is
        # re-rendered — refresh it so the change lands from the next call on
        # (touch-cached by design, and this is the write that invalidates it).
        ctx = getattr(self, "_ctx", None)
        refresh = (
            getattr(ctx, "refresh_system_prompt", None) if ctx is not None else None
        )
        if refresh is not None:
            refresh()
        return json.dumps(info, ensure_ascii=False, indent=2)
