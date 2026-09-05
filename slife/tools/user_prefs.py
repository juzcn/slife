"""add_user_pref — record a standing user preference into USER.md.

USER.md is the per-agent "User Preferences" file appended to the system
prompt at build time — the one place the user's own directives live in the
prompt.  This tool is the model's single write path into it: it delegates
to the memfiles plugin's internal ``__user_pref_append`` (a deterministic,
structure-preserving read-merge-write through the plugin's own file lock),
then, on success, refreshes the system prompt so the new preference is
live from the next API call.  It transcribes a preference the user actually
stated; it does not author preferences on its own.
"""

from __future__ import annotations

import json
import logging
from typing import ClassVar

from slife.tools.base import Tool, make_params

logger = logging.getLogger(__name__)

_OFFLINE = (
    "Error: memfiles plugin not connected — user preferences are unavailable."
)


class _MemfilesCallMixin:
    """Delegate a call to the memfiles plugin over its MCP client.

    Mirrors the ``_ScheduleMixin`` split in ``slife/tools/schedule.py``: the
    main process never touches the plugin's store — every data op reaches it
    through ``ToolContext.memfiles_client``.
    """

    def _client(self):
        ctx = getattr(self, "_ctx", None)
        return getattr(ctx, "memfiles_client", None) if ctx is not None else None

    async def _call(self, tool: str, arguments: dict | None = None):
        client = self._client()
        if client is None:
            return _OFFLINE
        try:
            return await client.call_tool(tool, arguments)
        except Exception as e:
            logger.debug("memfiles_tool_error tool=%s err=%s", tool, e)
            return f"Error: {tool} failed — {e}"


class AddUserPrefTool(_MemfilesCallMixin, Tool):
    """Record one standing user preference the user has stated."""

    name = "add_user_pref"
    category: ClassVar[str] = "System"

    description = (
        "Record a standing user preference into USER.md (the 'User "
        "Preferences' file appended to the system prompt, kept across sessions)."
    )
    parameters = make_params(
        preference={
            "type": "string",
            "description": (
                "Bold key term + statement, e.g. \"**Search** — use Baidu for "
                "Chinese news.\""
            ),
        },
    )

    async def execute(self, preference: str = "", **kwargs) -> str:
        if not (preference or "").strip():
            return "Error: preference is required."
        raw = await self._call("__user_pref_append", {"preference": preference})
        if not isinstance(raw, str):
            return "Error: unexpected memfiles plugin response."
        try:
            info = json.loads(raw)
        except ValueError:
            return raw
        if info.get("error"):
            return f"Error: {info['error']}"
        # A successful write is only visible once the session's system prompt
        # is re-rendered — refresh it so the new preference lands from the
        # next call onward (touch-cached by design; duplicates skip it).
        if info.get("appended"):
            ctx = getattr(self, "_ctx", None)
            refresh = (
                getattr(ctx, "refresh_system_prompt", None)
                if ctx is not None else None
            )
            if refresh is not None:
                refresh()
        return json.dumps(info, ensure_ascii=False, indent=2)