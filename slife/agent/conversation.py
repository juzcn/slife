"""Conversation history management in OpenAI message format.

Supports multimodal messages (text + images) for vision-capable models.
"""

import logging

from slife.agent.multimodal import encode_image

logger = logging.getLogger(__name__)


class Conversation:
    """Manages the message list for an LLM conversation.

    Messages follow the OpenAI format with roles:
    system, user (text or multimodal), assistant, tool.
    """

    def __init__(self, system_prompt: str | None = None):
        self.messages: list[dict] = []
        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})
            logger.debug("conv_init sys_prompt_len=%d", len(system_prompt))

    def _repair_orphaned_tool_calls(self) -> int:
        """Add synthetic error results for any assistant tool_calls that
        lack corresponding tool result messages.

        When a user interrupts a running request (e.g. by sending a new
        message), the conversation may end with an ``assistant(tool_calls=…)``
        message that has no follow-up tool result.  The OpenAI API rejects
        this with a 400 error.  This method repairs the history so it is
        always well-formed before any new message is appended.

        Returns:
            Number of synthetic tool results added.
        """
        repaired = 0
        # Walk backwards: for each assistant message with tool_calls,
        # check that the next message(s) are tool results with matching ids.
        i = len(self.messages) - 1
        pending_ids: list[str] = []
        while i >= 0:
            msg = self.messages[i]
            role = msg.get("role", "")
            if role == "assistant" and msg.get("tool_calls"):
                # Collect expected tool_call_ids
                expected = {tc["id"] for tc in msg["tool_calls"]}
                # Check if the following messages (which we already scanned)
                # provide results for all of them
                matched = set()
                for pid in list(pending_ids):
                    if pid in expected:
                        matched.add(pid)
                        pending_ids.remove(pid)
                missing = expected - matched
                for tc_id in missing:
                    logger.warning(
                        "conv_orphan_repair tool_call_id=%s", tc_id,
                    )
                    # Insert synthetic error tool result right after the
                    # assistant message (before whatever comes next).
                    insert_at = i + 1
                    self.messages.insert(
                        insert_at,
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": "Error: request cancelled by user",
                        },
                    )
                    repaired += 1
            elif role == "tool":
                pending_ids.append(msg.get("tool_call_id", ""))
            i -= 1
        return repaired

    def add_user_message(
        self, content: str, images: list[str] | None = None
    ) -> None:
        """Add a user message, optionally with attached images.

        If images are provided, the content is sent as a multimodal array
        of content blocks (text + image_url parts). Otherwise, a plain
        text content string is used.

        Args:
            content: The user's text input.
            images: Optional list of image file paths to attach.
        """
        # Ensure the conversation is well-formed before adding a user
        # message.  If a previous request was cancelled during tool
        # execution, there may be orphaned tool_calls without results.
        self._repair_orphaned_tool_calls()

        if images:
            parts: list[dict] = [{"type": "text", "text": content}]
            for img_path in images:
                parts.append(encode_image(img_path))
            self.messages.append({"role": "user", "content": parts})
            logger.debug("conv_user text=%.80s imgs=%d", content, len(images))
        else:
            self.messages.append({"role": "user", "content": content})
            logger.debug("conv_user text=%.80s", content)

    def add_assistant_message(
        self, content: str | None, tool_calls: list | None = None
    ) -> None:
        """Add an assistant message, optionally with tool calls."""
        msg: dict = {"role": "assistant"}
        msg["content"] = content if content is not None else ""
        if tool_calls:
            msg["tool_calls"] = tool_calls
            tc_names = [
                tc.get("function", {}).get("name", "?")
                for tc in tool_calls
            ]
            logger.debug("conv_assistant tool_calls=%s", tc_names)
        else:
            logger.debug("conv_assistant text_len=%d", len(content or ""))
        self.messages.append(msg)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        """Add a tool result message."""
        logger.debug(
            "conv_tool_result id=%s result_len=%d",
            tool_call_id,
            len(content),
        )
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })

    def to_openai_messages(self) -> list[dict]:
        """Return a copy of all messages for the API call."""
        return list(self.messages)

    def clear(self) -> None:
        """Clear conversation, preserving system prompt if present."""
        old_count = len(self.messages)
        system_msg = (
            self.messages[0]
            if self.messages and self.messages[0]["role"] == "system"
            else None
        )
        self.messages = [system_msg] if system_msg else []
        logger.debug("conv_clear removed=%d", old_count - len(self.messages))
