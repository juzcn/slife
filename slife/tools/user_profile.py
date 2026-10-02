"""profile_edit — the user's standing profile (the cabinet's ``USER.md``).

The profile is the per-agent File Cabinet file appended to the system prompt as
its final section, so **its contents are already in front of the model every
turn**.  That is why there is no read tool: a read would hand back the text the
model is looking at, and the section renders ``(Empty)`` when nothing is
recorded, so "what does the user want me to hold?" is answered without a call.
One verb, and the file is the interface.

It is a DOCUMENT, not a record store: ``write`` replaces it whole, so update
and remove are edits to text the model has read, not modes of an API.  There
is deliberately no append — a store the agent can only add to is one it can
neither review nor correct, which left a badly-worded entry fixable only by
hand-editing the file.

The write delegates to the memfiles plugin over the MCP client, so the plugin
stays USER.md's only host and its single writer, and refreshes the system
prompt on success so a change lands from the next API call.
"""

from __future__ import annotations

import json
from typing import ClassVar

from slife.agent.system_prompt import USER_PROFILE_MAX_CHARS
from slife.tools.base import Tool, _MemfilesClientMixin, make_params, require_params


class ProfileEditTool(_MemfilesClientMixin, Tool):
    """Replace the user's standing profile."""

    offline_message = (
        "Error: memfiles plugin not connected — the user profile is unavailable."
    )

    name = "profile_edit"
    category: ClassVar[str] = "System"

    description = (
        "Replace the user's standing profile — their own words about who they "
        "are, how they want you to work, and what to keep in mind. It is "
        "already in front of you every turn, so a write is live from the next "
        "call. The whole profile is replaced; nothing is merged, so send the "
        "complete new text."
    )
    parameters = make_params(
        content={
            "type": "string",
            "description": (
                "The complete new profile, in markdown. Only what must be in "
                "front of you every turn — the rest belongs in notes."
            ),
        },
    )

    async def execute(self, content: str = "", **kwargs) -> str:
        if err := require_params(content=(content or "").strip()):
            return err
        # The prompt carries at most this much, and a write that exceeded it
        # would be silently cut on the way into every request.  Refusing here
        # is the one place the model can be told — and it is told the number.
        if len(content) > USER_PROFILE_MAX_CHARS:
            return (
                f"Error: content is {len(content)} characters; the system "
                f"prompt carries at most {USER_PROFILE_MAX_CHARS}. The profile "
                f"is rendered every turn — shorten it."
            )
        raw = await self._call("__user_profile_edit", {"content": content})
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
