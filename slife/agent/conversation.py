"""Conversation history management in OpenAI message format.

Supports multimodal messages (text + images) for vision-capable models.
"""

import json
import logging
import uuid

from slife.agent.multimodal import include_image_url
from slife.logfmt import sanitize_secrets

logger = logging.getLogger(__name__)

# Module-level reference so tools (e.g. clear_context) can access the
# active conversation without a circular dependency.  Set by AgentService
# at initialisation time.
_current_conversation: "Conversation | None" = None


def get_conversation() -> "Conversation | None":
    """Return the active Conversation, or None if not yet initialised."""
    return _current_conversation


def set_conversation(conv: "Conversation") -> None:
    """Set the active Conversation (called by AgentService)."""
    global _current_conversation
    _current_conversation = conv


class Conversation:
    """Manages the message list for an LLM conversation.

    Messages follow the OpenAI format with roles:
    system, user (text or multimodal), assistant, tool.
    """

    def __init__(self, system_prompt: str | None = None):
        self.messages: list[dict] = []
        self._base_system_prompt: str = system_prompt or ""
        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})
            logger.debug("conv_init sys_prompt_len=%d", len(system_prompt))

    def update_context_footer(self, footer: str) -> None:
        """Replace the system message with base + dynamic context footer.

        The base system prompt (built at startup) stays immutable.
        The footer is re-rendered before each API call with current
        CWD, shell, time, and token usage.
        """
        if not self._base_system_prompt:
            return
        full = self._base_system_prompt + "\n\n" + footer
        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0]["content"] = full
        else:
            self.messages.insert(0, {"role": "system", "content": full})

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

        Local image files are read and base64-encoded into data URIs;
        remote URLs (``https://``) are passed through as-is.  Images that
        cannot be read are dropped with a visible note appended to the
        message so the LLM knows the attachment was lost.

        User input is sanitized to mask any API keys / tokens before the
        message enters the LLM context or persistent storage.

        Args:
            content: The user's text input.
            images: Optional list of image file paths or URLs to attach.
        """
        # Ensure the conversation is well-formed before adding a user
        # message.  If a previous request was cancelled during tool
        # execution, there may be orphaned tool_calls without results.
        self._repair_orphaned_tool_calls()

        content = sanitize_secrets(content)

        if images:
            parts: list[dict] = [{"type": "text", "text": content}]
            dropped: list[str] = []
            for img in images:
                block = include_image_url(img)
                if block is not None:
                    parts.append(block)
                else:
                    dropped.append(str(img))
            if dropped:
                note = (
                    "\n\n[System note: the following image file(s) could not be "
                    "read and were NOT sent to the model: "
                    + ", ".join(dropped)
                    + "]"
                )
                parts.append({"type": "text", "text": note})
                logger.warning(
                    "conv_user_dropped_images count=%d files=%s",
                    len(dropped), dropped,
                )
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
            # Sanitize arguments in every tool call — the LLM may
            # accidentally pass secrets (e.g. API keys in python_exec
            # code).  These arguments re-enter the LLM context on the
            # next turn, so they must be masked.
            sanitized_calls = []
            for tc in tool_calls:
                tc_copy = dict(tc)
                fn = tc_copy.get("function", {})
                if "arguments" in fn:
                    fn["arguments"] = sanitize_secrets(str(fn["arguments"]))
                sanitized_calls.append(tc_copy)
            msg["tool_calls"] = sanitized_calls
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

    def inject_images_to_last_user(
        self, image_blocks: list[dict],
    ) -> None:
        """Append pre-built image blocks to the last user message.

        Used by ``include_image`` so the LLM sees images as vision
        content blocks on the next turn, not just as text.

        Each block must be a dict with ``"type": "image_url"``.
        """
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i]["role"] == "user":
                content = self.messages[i]["content"]
                if not isinstance(content, list):
                    # Convert plain text to multimodal
                    self.messages[i]["content"] = [
                        {"type": "text", "text": content},
                    ]
                self.messages[i]["content"].extend(image_blocks)
                logger.debug(
                    "conv_inject_images count=%d", len(image_blocks),
                )
                break

    def to_openai_messages(
        self, thinking_enabled: bool = False,
    ) -> list[dict]:
        """Return messages for the API call.

        Converts the internal ``thinking`` field to ``reasoning_content``
        which is the wire-format field DeepSeek / Qwen require when
        thinking mode is enabled.  The API returns a 400 error if
        reasoning_content is missing from *any* assistant message in the
        conversation — including synthetic harness messages that never
        carried reasoning.
        """
        cleaned = []
        for msg in self.messages:
            m = dict(msg)
            # Thinking mode: reasoning_content must be preserved across
            # turns.  Stored internally as 'thinking', renamed here.
            thinking = m.pop("thinking", None)
            if thinking:
                m["reasoning_content"] = thinking
            elif thinking_enabled and m.get("role") == "assistant":
                # DeepSeek/Qwen require reasoning_content on EVERY
                # assistant message when thinking is on, even synthetic
                # harness messages (_sys_note, _sys_trim) that
                # never carried reasoning.
                m["reasoning_content"] = ""
            m.pop("images", None)  # internal attachment tracking
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

    def clear_history(self) -> int:
        """Clear all old turns, preserving system prompt and the current turn.

        The "current turn" starts with the last user message and includes
        all following assistant and tool messages.  This is safe to call
        from within a tool — the assistant(tool_calls) and pending tool
        results are preserved so the conversation stays well-formed.

        Returns:
            Number of messages removed.
        """
        if not self.messages:
            return 0

        # Find the last user message (start of the current turn)
        last_user_idx: int | None = None
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i]["role"] == "user":
                last_user_idx = i
                break

        if last_user_idx is None:
            return 0

        # System prompt is always at index 0 if present
        system_msg = (
            self.messages[0]
            if self.messages and self.messages[0]["role"] == "system"
            else None
        )

        # Keep: system prompt (if present) + everything from the last user
        # message onwards (the current turn).
        kept: list[dict] = []
        if system_msg is not None and last_user_idx > 0:
            kept.append(system_msg)
        kept.extend(self.messages[last_user_idx:])

        old_count = len(self.messages)
        self.messages = kept
        removed = old_count - len(self.messages)

        if removed > 0:
            logger.debug(
                "conv_clear_history removed=%d remaining=%d (system+current turn)",
                removed, len(self.messages),
            )
        return removed

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

    # ── Extract turns helpers ─────────────────────────────────────

    @staticmethod
    def extract_turns(messages: list[dict]) -> list[dict]:
        """Group a flat message list into per-turn summaries.

        A turn starts with a user message and includes all following
        assistant and tool messages until the next user message.

        Returns a list of dicts, each with:
          - ``user_message`` (str) — the user's text
          - ``messages`` (list[dict]) — all messages in the turn
          - ``estimated_tokens`` (int) — rough token count
        """
        turns: list[dict] = []
        current_turn: list[dict] = []
        current_user_msg = ""

        for msg in messages:
            role = msg.get("role", "")
            if role == "user" and current_turn:
                turns.append({
                    "user_message": current_user_msg,
                    "messages": list(current_turn),
                    "estimated_tokens": sum(
                        len(str(m.get("content", ""))) // 3
                        for m in current_turn
                    ),
                })
                current_turn = []
                current_user_msg = ""

            if role == "user" and not current_user_msg:
                content = msg.get("content", "")
                if isinstance(content, list):
                    current_user_msg = "".join(
                        p.get("text", "")
                        for p in content
                        if p.get("type") == "text"
                    )
                else:
                    current_user_msg = str(content) if content else ""

            current_turn.append(msg)

        if current_turn:
            turns.append({
                "user_message": current_user_msg,
                "messages": list(current_turn),
                "estimated_tokens": sum(
                    len(str(m.get("content", ""))) // 3
                    for m in current_turn
                ),
            })

        return turns

    def extract_oldest_turns(
        self,
        target: int,
    ) -> tuple[list[dict], int]:
        """Extract and delete oldest complete turns from the conversation.

        Walks forward from after the system prompt, removing complete
        user→… turns until the estimated token count drops to or below
        *target*.

        The **current turn** (the last user message and everything after
        it) is always preserved — it represents the on-going request that
        the agent is still processing.

        Returns (turns, tokens_freed) where *turns* is the
        :meth:`extract_turns`-format list and *tokens_freed* is the
        approximate number of tokens freed.
        """
        sys_end = 1 if (
            self.messages and self.messages[0]["role"] == "system"
        ) else 0

        # Find the last user message — the current turn starts here and
        # must never be removed.
        last_user_idx: int | None = None
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i]["role"] == "user":
                last_user_idx = i
                break

        if last_user_idx is None:
            return [], 0

        current = self.count_tokens()
        removed_messages: list[dict] = []

        while current > target and sys_end < last_user_idx:
            # Find the next user message (start of a turn)
            turn_start = None
            for i in range(sys_end, last_user_idx):
                if self.messages[i]["role"] == "user":
                    turn_start = i
                    break

            if turn_start is None:
                break  # no complete old turns left

            # Find the end of this turn (next user message, or the
            # current turn boundary)
            turn_end = last_user_idx
            for i in range(turn_start + 1, last_user_idx):
                if self.messages[i]["role"] == "user":
                    turn_end = i
                    break

            removed_messages.extend(self.messages[turn_start:turn_end])
            del self.messages[turn_start:turn_end]
            # Adjust last_user_idx — the slice we just deleted shifted
            # everything after it down.
            removed_count = turn_end - turn_start
            last_user_idx -= removed_count
            current = self.count_tokens()

        if not removed_messages:
            return [], 0

        turns = Conversation.extract_turns(removed_messages)
        tokens_freed = sum(
            len(str(m.get("content", ""))) // 3 for m in removed_messages
        )
        return turns, max(tokens_freed, 1)

    def insert_trim_notification(
        self,
        tool_call_id: str,
        turns_removed: int,
        tokens_freed: int,
        turns_summary: str,
        memory_saved: bool = True,
    ) -> None:
        """Insert a synthetic ``_sys_trim`` tool-call + result pair.

        The pair is inserted right after the system prompt (or at
        position 0 when there is no system prompt), so the LLM sees the
        trim as a chronological event before the remaining conversation.
        """
        assistant_msg: dict = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "_sys_trim",
                        "arguments": json.dumps(
                            {
                                "turns_removed": turns_removed,
                                "tokens_freed": tokens_freed,
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        }

        if memory_saved:
            result_content = (
                f"已裁剪 {turns_removed} 个最旧轮次（约 {tokens_freed} tokens），"
                f"内容已存入记忆库，如需回顾请用 memory_search。\n"
                f"\n{turns_summary}"
            )
        else:
            result_content = (
                f"已裁剪 {turns_removed} 个最旧轮次（约 {tokens_freed} tokens），"
                f"内容已丢弃。\n"
                f"\n{turns_summary}"
            )

        tool_msg: dict = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result_content,
        }

        insert_pos = 1 if (
            self.messages and self.messages[0]["role"] == "system"
        ) else 0
        self.messages.insert(insert_pos, tool_msg)
        self.messages.insert(insert_pos, assistant_msg)

        logger.debug(
            "conv_insert_trim_notification id=%s turns=%d tokens=%d",
            tool_call_id,
            turns_removed,
            tokens_freed,
        )

    def insert_context_status(self, content: str) -> None:
        """Insert a synthetic ``_sys_note`` tool-call + result pair.

        Placed right after the last user message so the LLM sees current
        time, token usage, and any changed model/CWD/shell at the start
        of each turn.  Like ``_sys_trim``, this is a harness
        notification — not in the tool schema, ``_`` prefix marks it as
        internal.

        Old ``_sys_note`` pairs are removed first to keep the
        conversation clean.
        """
        self._remove_synthetic_tool("_sys_note")

        tool_call_id = f"_ctx_{uuid.uuid4().hex[:8]}"
        assistant_msg: dict = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "_sys_note",
                        "arguments": "{}",
                    },
                }
            ],
        }
        tool_msg: dict = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        }

        # Insert right after the last user message.
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                self.messages.insert(i + 1, tool_msg)
                self.messages.insert(i + 1, assistant_msg)
                break

        logger.debug("conv_insert_context_status id=%s", tool_call_id)

    def _remove_synthetic_tool(self, name: str) -> None:
        """Remove all synthetic tool-call + result pairs with *name*."""
        i = 0
        while i < len(self.messages):
            msg = self.messages[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if tc.get("function", {}).get("name") == name:
                        if i + 1 < len(self.messages):
                            del self.messages[i:i + 2]
                        else:
                            del self.messages[i]
                        break
                else:
                    i += 1
            else:
                i += 1
