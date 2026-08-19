"""Notification tool — push a desktop notification to the human operator.

``notify_user`` is a pure UI surface: the LLM never sees the notification
itself, it just triggers the display.  Files and images are never rendered
in-terminal — the agent hands the user a path / URL and they open it with
the OS.
"""

from __future__ import annotations

from typing import ClassVar

from slife.tools.base import Tool


class NotifyUserTool(Tool):
    """Push a desktop notification to the human operator.

    A pure UI tool — it only triggers the display; the LLM never sees
    the notification itself.
    """

    name: ClassVar[str] = "notify_user"
    category: ClassVar[str] = "Display"
    description: ClassVar[str] = (
        "Send a desktop notification to the human user."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short notification title (e.g. 'Task Complete', 'Alert').",
            },
            "message": {
                "type": "string",
                "description": "The notification body — be concise (one sentence).",
            },
        },
        "required": ["message"],
    }

    async def execute(self, title: str = "slife", message: str = "", **kwargs) -> str:
        if not message:
            return "Error: message is required."

        # Log for the session file at WARNING (the console is capped below
        # WARNING, so this is diagnostic-only; the notification below is the
        # user-facing channel).
        import logging
        logging.getLogger(__name__).warning(
            "USER_NOTIFICATION title=%s message=%s", title, message,
        )

        # Fire desktop notification (best-effort, non-blocking).
        # Daemon thread: a hung notify backend must never block shutdown.
        from slife.platform import desktop_notify
        from slife.threads import run_daemon
        run_daemon(desktop_notify, title, message, name="desktop-notify")

        return f"Notification sent: [{title}] {message}"
