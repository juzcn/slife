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
