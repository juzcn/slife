"""Conversation history management in OpenAI message format.

Supports multimodal messages (text + images) for vision-capable models.
"""

import logging

from slife.agent.multimodal import encode_image
from slife.logfmt import sanitize_secrets

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

        User input is sanitized to mask any API keys / tokens before the
        message enters the LLM context or persistent storage.

        Args:
            content: The user's text input.
            images: Optional list of image file paths to attach.
        """
        # Ensure the conversation is well-formed before adding a user
        # message.  If a previous request was cancelled during tool
        # execution, there may be orphaned tool_calls without results.
        self._repair_orphaned_tool_calls()

        content = sanitize_secrets(content)

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
        self, content: str | None, tool_calls: list | None = None,
        thinking: str | None = None,
    ) -> None:
        """Add an assistant message, optionally with tool calls and thinking.

        The ``thinking`` field stores the model's reasoning process for
        permanent memory, but is stripped before sending to the API
        (not a standard OpenAI message field).
        """
        msg: dict = {"role": "assistant"}
        msg["content"] = content if content is not None else ""
        if thinking:
            msg["thinking"] = thinking
        if tool_calls:
            msg["tool_calls"] = tool_calls
            tc_names = [
                tc.get("function", {}).get("name", "?")
                for tc in tool_calls
            ]
            logger.debug("conv_assistant tool_calls=%s think=%d", tc_names, len(thinking or ""))
        else:
            logger.debug("conv_assistant text_len=%d think=%d", len(content or ""), len(thinking or ""))
        self.messages.append(msg)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        """Add a tool result message."""
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })

    def to_openai_messages(self) -> list[dict]:
        """Return messages for the API call.

        Strips internal fields (thinking) that are not part of the
        standard OpenAI message format.
        """
        cleaned = []
        for msg in self.messages:
            m = dict(msg)
            m.pop("thinking", None)  # internal only — not sent to API
            m.pop("images", None)    # internal attachment tracking
            cleaned.append(m)
        return cleaned

    def pop_last_turn(self) -> int:
        """Remove the last user turn and all subsequent messages.

        A "turn" starts with a user message and includes all assistant
        and tool messages that follow, up to the next user message or
        end of the list.  Used to rollback a failed turn so the
        conversation isn't poisoned for the next attempt.

        Returns:
            Number of messages removed.
        """
        if not self.messages:
            return 0

        # Find the index of the last user message
        last_user_idx = None
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i]["role"] == "user":
                last_user_idx = i
                break

        if last_user_idx is None:
            return 0

        removed = len(self.messages) - last_user_idx
        del self.messages[last_user_idx:]
        logger.debug(
            "conv_pop_last_turn removed=%d remaining=%d",
            removed, len(self.messages),
        )
        return removed

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

    # ── Context window trimming ──────────────────────────────────

    def count_tokens(self) -> int:
        """Estimate total tokens in the current message list.

        Uses a simple character-based heuristic: ~4 chars per token
        for mixed Chinese/English text. Accurate enough for window
        management — the ceiling/floor mechanism has 20% margins
        so small estimation errors are harmless.
        """
        total = 0
        for msg in self.messages:
            content = msg.get("content") or ""
            total += len(content) // 3  # ~3 chars/token for CJK+code mix
            # Tool calls add significant overhead
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    args = tc.get("function", {}).get("arguments", "")
                    total += len(str(args)) // 3
            # Images are token-heavy
            if msg.get("images"):
                total += len(msg["images"]) * 200  # rough per-image estimate
        return max(total, 1)

    def trim_context(
        self,
        context_window: int,
        floor: float = 0.2,
        ceiling: float = 0.8,
    ) -> int:
        """Trim oldest turns when context exceeds ceiling, down to floor.

        Preserves the system prompt. Removes whole turns only — a turn
        starts with a user message and includes all following assistant
        and tool messages until the next user message.

        Returns the number of messages removed.
        """
        if not self.messages or context_window <= 0:
            return 0

        ceiling_tokens = int(context_window * ceiling)
        current = self.count_tokens()

        if current <= ceiling_tokens:
            return 0

        target = int(context_window * floor)

        # Find system prompt boundary
        sys_end = 1 if self.messages[0]["role"] == "system" else 0

        removed_total = 0
        while current > target and sys_end < len(self.messages):
            # Find the next user message (start of a turn)
            turn_start = None
            for i in range(sys_end, len(self.messages)):
                if self.messages[i]["role"] == "user":
                    turn_start = i
                    break

            if turn_start is None:
                break  # no complete turns left to trim

            # Find the end of this turn (next user message or end)
            turn_end = len(self.messages)
            for i in range(turn_start + 1, len(self.messages)):
                if self.messages[i]["role"] == "user":
                    turn_end = i
                    break

            # Remove the entire turn
            count = turn_end - turn_start
            del self.messages[turn_start:turn_end]
            removed_total += count
            current = self.count_tokens()

        if removed_total > 0:
            logger.info(
                "context_trimmed removed=%d turns_tokens=%d window=%d floor=%.0f%%",
                removed_total, current, context_window, floor * 100,
            )

        return removed_total
