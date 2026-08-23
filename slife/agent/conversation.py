"""Conversation history management in OpenAI message format.

Supports multimodal messages (text + images) for vision-capable models.
"""

import logging

from slife.logfmt import sanitize_secrets

logger = logging.getLogger(__name__)

# Machine-injected annotations inside a user message share one `[<Kind>: …]`
# shape.  The restore turn header is the only one — concatenated as a
# trailing footnote into the restored user-message text (see
# ``slife.ui.restore._turn_header``) so the LLM can tell which turn (rowid) a
# restored message belongs to and when it happened.  Deliberately NOT
# excluded from the TUI — the human reads it too.
# Heartbeat is NOT an annotation: `[Heartbeat]` is a stored turn identity
# (old diary rows start with it), so it stays a distinct sentinel.
#: Kind tag of the turn footnote (``[Turn: N · start → end]``).  Shared by
#: the restore path, the save path, and the TUI ``UserMessage`` styler.
TURN_HEADER_PREFIX = "[Turn: "
#: Runtime-only marker of a context trim (``[TrimContext: N]``).  Appended
#: to the last assistant message by the loop after a trim — NEVER persisted:
#: a restored session is already the trimmed state, so a "past session was
#: truncated" note is meaningless.  Stripped before every diary save.
TRIM_MARKER_PREFIX = "[TrimContext: "


def _format_turn_dt(value) -> str:
    """ISO stored timestamp → 'YYYY-MM-DD HH:MM' (minute precision)."""
    if not value:
        return ""
    return str(value)[:16].replace("T", " ")


def turn_header(turn: dict) -> str:
    """Compact turn identity: ``[Turn: N · start → end]``.

    Only the id, start time and end time — the conversation carries the
    content.  Returns ``""`` when neither id nor timestamps are known
    (legacy turns), so the message stays plain.
    """
    rowid = turn.get("rowid")
    start = _format_turn_dt(turn.get("created_at"))
    end = _format_turn_dt(turn.get("completed_at"))
    if start and end and end != start:
        # Same day → end time only; otherwise full end datetime.
        if end[:10] == start[:10]:
            end = end[11:]
        span = f"{start} → {end}"
    else:
        span = start or end
    bits = ([str(rowid)] if rowid is not None else []) + ([span] if span else [])
    return f"{TURN_HEADER_PREFIX}{' · '.join(bits)}]" if bits else ""


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
                    # Routine self-heal — a recoverable anomaly, so WARNING
                    # in the log.  The console is capped below WARNING, so
                    # this never reaches the terminal (TUI surfacing is done
                    # by the business layer explicitly).
                    logger.warning("conv_orphan_repair tool_call_id=%s", tc_id)
                    # Insert synthetic error tool result right after the
                    # assistant message (before whatever comes next).
                    self.messages.insert(
                        i + 1,
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": "(Tool execution interrupted)",
                            # An interrupted execution is not "done" —
                            # restore must render it as an error, matching
                            # the content the LLM context carries.
                            "is_error": True,
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
        self, content: str,
    ) -> None:
        """Add a user message — the user's text, verbatim.

        Images are NOT encoded here.  Attachments ride the ``include_image``
        tool path (harness-invoked for ``@path`` so no LLM iteration is
        spent attaching): the tool injects the image content block into this
        message in memory via :meth:`inject_images_to_last_user`.  Those
        blocks are live-session-only — never persisted, restore is text-only.

        User input is sanitized to mask any API keys / tokens before the
        message enters the LLM context or persistent storage.
        """
        # Turn consistency is enforced at the single save point
        # (save_to_memory, which runs unconditionally after every turn) and on
        # TUI restore — so by the time a new user message is appended the
        # conversation is already well-formed.
        content = sanitize_secrets(content)
        self.messages.append({"role": "user", "content": content})
        logger.debug("conv_user text=%.80s", content)

    def add_assistant_message(
        self, content: str | None, tool_calls: list | None = None,
        thinking: str | None = None,
    ) -> None:
        """Add an assistant message, optionally with tool calls and thinking.

        The ``thinking`` field stores the model's reasoning process for
        permanent memory.  It is not stripped on the wire:
        :meth:`to_openai_messages` renames it to ``reasoning_content``
        (DeepSeek/Qwen's wire field), so the openai-completions backend
        re-sends it and the model sees its prior reasoning on later turns.
        The anthropic-messages and openai-responses backends drop it during
        format conversion, so there it never re-enters the model's context.
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

    def append_trim_marker(self, count: int) -> None:
        """Append a runtime-only ``[TrimContext: N]`` marker to the last
        assistant message.

        Tells the LLM how many of its oldest turns were just cut from the
        context by a trim (tool results the model may still reference are
        gone).  Unlike the persisted ``[Turn: N]`` footnotes, this marker
        is **not** written to memory — a restored session is already the
        trimmed state, so a "past session was truncated" note is
        meaningless; only the current cut is relevant.  It is therefore
        never present in restored history.

        The last message is an assistant by :meth:`_ensure_turn_consistent`
        (guaranteed when save_to_memory calls this after a trim).
        """
        if not self.messages:
            return
        idx = len(self.messages) - 1
        while idx >= 0 and self.messages[idx].get("role") != "assistant":
            idx -= 1
        if idx < 0:
            return
        msg = self.messages[idx]
        marker = f"[TrimContext: {count}]"
        content = msg.get("content") or ""
        msg["content"] = f"{content} {marker}".strip() if content else marker
        logger.debug("conv_trim_marker count=%d msg_idx=%d", count, idx)

    @staticmethod
    def strip_trim_markers(messages: list[dict]) -> list[dict]:
        """Return a copy of *messages* with ``[TrimContext: N]`` markers removed.

        The marker is runtime-only: this keeps it out of the diary.  The
        live conversation keeps its marker (the LLM needs to know the
        current cut); only what is persisted is cleaned.  Works on the
        list passed in — callers pass the sliced turn messages.
        """
        cleaned = []
        for m in messages:
            if m.get("role") == "assistant" and m.get("content"):
                content = m["content"]
                if isinstance(content, str) and TRIM_MARKER_PREFIX in content:
                    m = dict(m)
                    m["content"] = content.split(TRIM_MARKER_PREFIX)[0].rstrip()
            cleaned.append(m)
        return cleaned

    def add_tool_result(
        self, tool_call_id: str, content: str, is_error: bool = False,
    ) -> None:
        """Add a tool result message.

        ``is_error`` records whether the execution failed (timeout,
        exception, denial).  It is persisted with the turn so session
        restore renders the same error state the live TUI showed —
        re-deriving it from the content text would duplicate the loop's
        detection rule in a second component.  Stripped from the wire
        format by :meth:`to_openai_messages` (not an OpenAI field).
        """
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
            "is_error": is_error,
        })

    def inject_images_to_last_user(
        self, image_blocks: list[dict],
    ) -> None:
        """Append pre-built image blocks to the last user message.

        Used by ``include_image`` so the LLM sees images as vision
        content blocks on the next turn, not just as text.  A single
        call may carry several blocks (one per attached image).

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
                # harness messages (_sys_note) that never carried
                # reasoning.
                m["reasoning_content"] = ""
            m.pop("images", None)  # internal attachment tracking
            m.pop("is_error", None)  # internal error flag, not an OpenAI field
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
            if isinstance(content, list):
                # Multimodal message — sum text parts and give each image a
                # flat per-image estimate.  The base64 data URI itself must not
                # be counted as text, or a single image would dominate the
                # whole window estimate.
                for part in content:
                    ptype = part.get("type")
                    if ptype == "text":
                        total += len(part.get("text", "")) // 3
                    elif ptype == "image_url":
                        total += 200  # rough per-image token estimate
            else:
                total += len(str(content)) // 3  # ~3 chars/token for CJK+code mix
            # Tool calls add significant overhead
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    args = tc.get("function", {}).get("arguments", "")
                    total += len(str(args)) // 3
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

