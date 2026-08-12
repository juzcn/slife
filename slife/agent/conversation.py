"""Conversation history management in OpenAI message format.

Supports multimodal messages (text + images) for vision-capable models.
"""

import logging

from slife.agent.multimodal import include_image_url
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

    @classmethod
    def from_history(
        cls, system_prompt: str, messages: list[dict],
    ) -> "Conversation":
        """Build a conversation seeded from an inherited message history.

        Used by subagents with a cloned context: *messages* are the parent
        agent's conversation (any system message is dropped), and the
        subagent's own system prompt is prepended.  Messages are copied so
        the source conversation is never mutated.
        """
        conv = cls(system_prompt=system_prompt)
        for msg in messages:
            if msg.get("role") == "system":
                continue
            conv.messages.append(dict(msg))
        return conv

    def _ensure_turn_consistent(self, content: str = "") -> int:
        """Restore the conversation to a consistent state.

        Two idempotent invariants for a turn that may have ended early
        (cancelled / errored / max-iteration, or restored from memory):

        1. **No orphaned tool_calls** — every assistant ``tool_call`` must
           have a matching ``tool`` result.  When a request is interrupted
           the conversation may end with an ``assistant(tool_calls=…)`` that
           has no follow-up tool result; the OpenAI API rejects this with a
           400.  Missing results get a synthetic ``Error: request cancelled
           by user`` result inserted right after the owning assistant
           message.
        2. **Alternating roles** — a conversation ending on a
           ``user``/``tool`` message (a tool result is a ``user`` role on
           the Anthropic wire, which rejects two consecutive users) gets a
           closing assistant message so roles keep alternating.

        Repair runs first: a dangling call as the last message (role
        ``"assistant"``) becomes a ``"tool"`` role after repair, so the
        closing-assistant check below then fires correctly.

        Returns the number of synthetic tool results inserted.
        """
        repaired = 0
        # Walk backwards: for each assistant message with tool_calls, check
        # that the following messages (already scanned) provide results for
        # all of its tool_call_ids.  Walking backwards lets a deeper assistant
        # message's results be consumed before an earlier one is checked.
        i = len(self.messages) - 1
        pending_ids: list[str] = []
        while i >= 0:
            msg = self.messages[i]
            role = msg.get("role", "")
            if role == "assistant" and msg.get("tool_calls"):
                expected = {tc["id"] for tc in msg["tool_calls"]}
                matched = set()
                for pid in list(pending_ids):
                    if pid in expected:
                        matched.add(pid)
                        pending_ids.remove(pid)
                for tc_id in expected - matched:
                    # Routine self-heal — keep it out of the terminal (console
                    # is WARNING+); INFO still lands in the session log file.
                    logger.info("conv_orphan_repair tool_call_id=%s", tc_id)
                    # Insert synthetic error tool result right after the
                    # assistant message (before whatever comes next).
                    self.messages.insert(
                        i + 1,
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": "(Tool execution interrupted)",
                        },
                    )
                    repaired += 1
            elif role == "tool":
                pending_ids.append(msg.get("tool_call_id", ""))
            i -= 1

        if self.messages and self.messages[-1]["role"] in ("user", "tool"):
            self.add_assistant_message(content=content or "(Turn interrupted)")
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
        # Turn consistency is enforced at the single save point
        # (save_to_memory, which runs unconditionally after every turn) and on
        # TUI restore — so by the time a new user message is appended the
        # conversation is already well-formed.
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

