"""Context management tools.

Tools:
    clear_context  — reset LLM conversation history to eliminate context pollution
"""

from __future__ import annotations

import logging

from slife.tools.base import Tool, NO_PARAMS

logger = logging.getLogger(__name__)


class ClearContextTool(Tool):
    """Clear the current LLM conversation context, preserving the system prompt.

    Use this when the conversation has accumulated irrelevant, contradictory,
    or confusing information that degrades response quality (context pollution).
    After clearing, the LLM starts fresh — only the system prompt remains.
    """

    name = "clear_context"
    description = (
        "Clear the current conversation history to reset the LLM's context. "
        "This preserves the system prompt but removes all prior messages "
        "(user, assistant, and tool results). "
        "Use when context pollution is degrading response quality — e.g. "
        "repeated errors, contradictory information, or irrelevant tangents "
        "that the model can't ignore. "
        "After calling this, the conversation is fresh and the next message "
        "starts a clean turn."
    )
    parameters = NO_PARAMS

    async def execute(self, **kwargs) -> str:
        from slife.agent.conversation import get_conversation

        conv = get_conversation()
        if conv is None:
            return (
                "Conversation is not yet initialised. "
                "This tool must be called after the agent service has started."
            )

        removed = conv.clear_history()
        if removed == 0:
            return (
                "Context is already clean — no old turns to remove. "
                "Only the system prompt and current turn are present."
            )

        remaining = len(conv.messages)

        logger.info(
            "clear_context removed=%d remaining=%d (system prompt + current turn preserved)",
            removed, remaining,
        )

        return (
            f"[OK] Context cleared. "
            f"Removed {removed} old message(s); {remaining} remaining "
            f"(system prompt + current turn preserved). "
            f"Memory / persistent storage is NOT affected."
        )
