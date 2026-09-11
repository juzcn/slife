"""Live message history — the agent's turn-ordered message array (OpenAI message format).

Supports multimodal messages (text + images) for vision-capable models.
"""

import json
import logging
import unicodedata

from slife.logfmt import sanitize_secrets

logger = logging.getLogger(__name__)


# ── Token estimation ─────────────────────────────────────────────────────
# A single per-script estimator shared by every place that turns stored text
# into a token figure (count_tokens, extract_turns / extract_oldest_turns,
# and the restore sizing heuristic).  One formula keeps the trim stop
# condition, the ``tokens_freed`` figure and the restore budget from
# disagreeing.

#: Scripts whose glyphs are ~1 token each (a BytePair tokenizer spends
#: roughly one token per Han character).  Determined by Unicode
#: east-asian-width — covers CJK ideographs plus full-width forms.
_WIDE_CHARS = ("W", "F")


def estimate_text_tokens(text: str) -> int:
    """Estimate the token cost of *text* by per-script char weights.

    Narrow (Latin/digit) text runs ~3 chars/token; wide (CJK, full-width)
    text runs closer to 1 token per char.  The old blanket ``chars // 3``
    counted every char alike and undercounted a Chinese-heavy session by
    ~2-3x — and because this estimate is the stop-condition for context
    trimming and the restore budget, undercounting let the window sit
    genuinely over the ceiling (trimmed to the floor yet still overflowing
    the next request).  Weighing wide chars at 1 token errs high, which is
    the safe direction for a ceiling/overflow guard.
    """
    wide = 0
    narrow = 0
    for ch in text:
        if unicodedata.east_asian_width(ch) in _WIDE_CHARS:
            wide += 1
        else:
            narrow += 1
    return narrow // 3 + wide


def estimate_message_tokens(msg: dict) -> int:
    """Estimate the token cost of one stored message (content + tool calls).

    Multimodal content sums its text parts (the base64 image data URI is
    never counted — a flat per-image estimate is used instead) and adds a
    flat per-image figure, mirroring :meth:`MessageHistory.count_tokens`.
    """
    content = msg.get("content") or ""
    total = 0
    if isinstance(content, list):
        for part in content:
            ptype = part.get("type")
            if ptype == "text":
                total += estimate_text_tokens(part.get("text", ""))
            elif ptype == "image_url":
                total += 200  # rough per-image token estimate
    else:
        total += estimate_text_tokens(str(content))
    for tc in msg.get("tool_calls") or []:
        args = tc.get("function", {}).get("arguments", "")
        total += estimate_text_tokens(str(args))
    return total

# Machine-injected annotations appended to a message share one envelope,
# ``[INFO: <payload>]``.  The payload is either a JSON object — the turn
# footnote (see ``turn_header``) — or a prose notice — the trim note (see
# ``trim_note``).  The restore turn header is the only annotation
# concatenated into the restored user-message text (see
# ``slife.ui.restore._turn_header``) so the LLM can tell which turn (rowid)
# a restored message belongs to and when it happened.  The envelope is
# machine-facing; the TUI renders the payload alone (see
# ``unwrap_info_envelope``).
# Heartbeat is NOT an annotation: `[Heartbeat]` is a stored turn identity
# (old diary rows start with it), so it stays a distinct sentinel.
#: Envelope of machine-injected annotations (``[INFO: …]``).  Shared by the
#: restore path, the save path, and the TUI ``UserMessage`` styler.
INFO_PREFIX = "[INFO: "
#: Channel marker for an incoming WeChat peer message: ``[Wechat:…]`` with a
#: JSON payload carrying what ``wechat_send_message`` needs to reply
#: (``peer_wechat_id``, ``context_token``), so the LLM can attribute the
#: message to its sender and reply without a separate status lookup.  The
#: keys mirror the tool's arguments.  It is machine-facing — the TUI shows
#: the ``Wechat>`` bubble prefix and ``unwrap_info_envelope`` strips the
#: marker for display (the model still sees the full content).
WECHAT_PREFIX = "[Wechat:"
#: Legacy marker for pre-JSON WeChat rows (old ``[WECHAT] `` prose prefix).
#: Kept so restored history written before :data:`WECHAT_PREFIX` still
#: renders without the marker; new messages use the JSON marker.
WECHAT_MARKER = "[WECHAT] "
#: Channel marker for an auto-pushed subagent completion: ``[Subagent:…]``
#: with a JSON payload naming the worker and its task id, so the LLM can tell
#: which subagent's which task pushed the result (the channel itself never
#: enters the context by default).  Like ``[WECHAT]`` it is machine-facing —
#: the TUI already shows the ``Subagent(<name>)> `` bubble prefix, so
#: ``unwrap_info_envelope`` strips the marker for display.
SUBAGENT_PREFIX = "[Subagent:"
#: Channel markers for A2A mesh messages pushed into the agent's context.
#: ``[A2A:…]`` prefixes an inbound peer message/task (the receiving agent
#: reads who sent it and — for a task — its task id); ``[A2A-PUSH:…]``
#: prefixes an auto-pushed async result.  Keys mirror the a2a tool
#: arguments (``agent_name`` / ``task_id``); the absence or presence of
#: ``task_id`` is what tells a message from a task.  Machine-facing — the
#: TUI shows the ``A2A(<name>)> `` bubble prefix and
#: ``unwrap_info_envelope`` strips them for display.
A2A_PREFIX = "[A2A:"
A2A_PUSH_PREFIX = "[A2A-PUSH:"
#: Runtime-only trim note: ``[INFO: <N> oldest turns have been removed from
#: context]``.  Appended by the loop after a trim — NEVER persisted: a
#: restored session is already the trimmed state, so a "past session was
#: truncated" note is meaningless.  Stripped before every diary save.  It is
#: told apart from the JSON turn footnote by its payload head — a digit; the
#: turn footnote's JSON starts with ``{`` (see ``_trim_note_in``).


def _format_turn_dt(value) -> str:
    """ISO stored timestamp → 'YYYY-MM-DD HH:MM' (minute precision)."""
    if not value:
        return ""
    return str(value)[:16].replace("T", " ")


def turn_header(turn: dict) -> str:
    """Compact turn identity: ``[INFO: {"turn_id": N, "begin": …, "end": …}]``.

    Reads the turn's internal ``rowid`` (the key the store's restore rows
    carry) and emits it as the LLM-facing ``turn_id``.  Only the id, begin
    time and end time — the history carries the content.  The end collapses
    to a time alone when it falls on the begin's day
    (``{"turn_id": 27, "begin": "2026-08-10 14:03", "end": "14:05"}``).
    Returns ``""`` when there is no id and no timestamps, so the message
    stays plain.
    """
    rowid = turn.get("rowid")
    start = _format_turn_dt(turn.get("created_at"))
    end = _format_turn_dt(turn.get("completed_at"))
    if start and end and start[:10] == end[:10]:
        # Same day → end time only; otherwise full end datetime.
        end = end[11:]
    payload: dict[str, object] = {}
    if rowid is not None:
        payload["turn_id"] = rowid
    if start:
        payload["begin"] = start
    if end:
        payload["end"] = end
    if not payload:
        return ""
    return f"{INFO_PREFIX}{json.dumps(payload, ensure_ascii=False)}]"


def trim_note(count: int) -> str:
    """Build the runtime trim marker ``[INFO: N oldest turns have been
    removed from context]``."""
    return f"{INFO_PREFIX}{count} oldest turns have been removed from context]"


def _trim_note_in(content: str) -> int | None:
    """Index of the trailing trim note inside *content*, or ``None``.

    Within the shared ``[INFO: …]`` envelope the trim note is the payload
    that starts with a digit (``"3 oldest turns …"``); the turn footnote is
    JSON and starts with ``{``.
    """
    start = content.rfind(INFO_PREFIX)
    if start == -1:
        return None
    payload = content[start + len(INFO_PREFIX):].lstrip()
    return start if payload[:1].isdigit() else None


def wechat_marker(peer_wechat_id: str, context_token: str | None = None) -> str:
    """Content prefix for an incoming WeChat peer message.

    ``[Wechat:{"peer_wechat_id": …, "context_token": …}] `` — the JSON keys
    mirror the ``wechat_send_message`` arguments so the model can reply to
    the right peer and thread without a separate status lookup.  The marker
    is machine-facing; ``unwrap_info_envelope`` drops it for display (the
    TUI shows the ``Wechat> `` bubble prefix instead).  A ``None`` context
    token is omitted from the payload.
    """
    payload: dict[str, str] = {"peer_wechat_id": peer_wechat_id}
    if context_token is not None:
        payload["context_token"] = context_token
    return f"{WECHAT_PREFIX}{json.dumps(payload, ensure_ascii=False)}] "


def a2a_marker(agent_name: str, task_id: str | None = None) -> str:
    """Content prefix for an inbound A2A peer message/task.

    ``[A2A:{"agent_name": …, "task_id": …}] `` — the JSON names the sending
    peer; ``task_id`` is present only for a task (a stateless message omits
    it), and that presence is how the LLM tells the two apart.  The marker
    is machine-facing; ``unwrap_info_envelope`` drops it for display (the
    TUI shows the ``A2A(<name>)> `` bubble prefix instead).
    """
    return _a2a_payload_marker(A2A_PREFIX, agent_name, task_id)


def a2a_push_marker(agent_name: str, task_id: str | None = None) -> str:
    """Content prefix for an auto-pushed A2A async result.

    Same shape as :func:`a2a_marker` under the ``[A2A-PUSH:…]`` envelope —
    ``task_id`` rides only for a task result, so the pushed message names
    which peer's which task it belongs to.
    """
    return _a2a_payload_marker(A2A_PUSH_PREFIX, agent_name, task_id)


def _a2a_payload_marker(prefix: str, agent_name: str, task_id: str | None) -> str:
    """Shared builder — the two A2A envelopes differ only in their prefix."""
    payload: dict[str, str] = {"agent_name": agent_name}
    if task_id is not None:
        payload["task_id"] = task_id
    return f"{prefix}{json.dumps(payload, ensure_ascii=False)}] "


def subagent_marker(subagent_name: str, task_id: str | None = None) -> str:
    """Content prefix for an auto-pushed subagent completion.

    ``[Subagent:{"subagent_name": …, "task_id": …}] `` — the JSON names the
    worker and its task id so the LLM can attribute the pushed result.  The
    keys mirror the LLM-facing subagent tool arguments (``subagent_name`` /
    ``task_id``).  The marker is machine-facing; ``unwrap_info_envelope``
    drops it for display (the TUI shows the ``Subagent(<name>)> `` bubble
    prefix instead).  A ``None`` task id is omitted from the payload.
    """
    payload: dict[str, str] = {"subagent_name": subagent_name}
    if task_id is not None:
        payload["task_id"] = task_id
    return f"{SUBAGENT_PREFIX}{json.dumps(payload, ensure_ascii=False)}] "


def unwrap_info_envelope(text: str) -> str:
    """Display form of a machine-injected marker in message text.

    The markers are machine-facing — the LLM reads them to tell an injected
    annotation apart from user text.  For the human they are noise, so the
    display form drops them:

    - a trailing ``[INFO: …]`` envelope renders as its payload alone (the
      JSON turn footnote as ``{"turn_id": N, …}``, the prose trim note as
      ``N oldest turns have been removed from context``);
    - a leading ``[Wechat:…]`` envelope is dropped entirely — the channel is
      already shown by the ``Wechat>`` bubble prefix (the legacy prose
      ``[WECHAT] `` marker on older stored rows is dropped too);
    - a leading ``[Subagent:…]`` envelope is dropped entirely — the channel
      is already shown by the ``Subagent(<name>)> `` bubble prefix;
    - leading ``[A2A:…]`` / ``[A2A-PUSH:…]`` envelopes are dropped entirely
      — the channel is already shown by the ``A2A(<name>)> `` bubble prefix.

    Only the *trailing* INFO envelope is unwrapped (``rfind``) — an
    annotation is always a suffix, and a user message may legitimately
    contain the literal ``[INFO:`` in prose.  Only *leading* Wechat, Subagent
    and A2A (plus legacy WECHAT) markers — exactly our injected prefixes —
    are stripped.  Returns *text* unchanged when no marker is present.
    """
    if text.startswith(WECHAT_MARKER):
        text = text[len(WECHAT_MARKER):]
    for prefix in (WECHAT_PREFIX, SUBAGENT_PREFIX, A2A_PREFIX, A2A_PUSH_PREFIX):
        if text.startswith(prefix):
            end = text.find("]", len(prefix))
            if end != -1:
                text = text[end + 1:].lstrip()
    start = text.rfind(INFO_PREFIX)
    if start == -1:
        return text
    payload = text[start + len(INFO_PREFIX):]
    if payload.endswith("]"):
        payload = payload[:-1]
    return text[:start] + payload


class MessageHistory:
    """Manages the message list for an LLM history.

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
    ) -> "MessageHistory":
        """Build a history seeded from an inherited message history.

        Used by subagents with a cloned context: *messages* are the parent
        agent's history (any system message is dropped), and the
        subagent's own system prompt is prepended.  Messages are copied so
        the source history is never mutated.
        """
        conv = cls(system_prompt=system_prompt)
        for msg in messages:
            if msg.get("role") == "system":
                continue
            conv.messages.append(dict(msg))
        return conv

    def _ensure_turn_consistent(self, content: str = "") -> int:
        """Restore the history to a consistent state.

        Two idempotent invariants for a turn that may have ended early
        (cancelled / errored / max-iteration, or restored from memory):

        1. **No orphaned tool_calls** — every assistant ``tool_call`` must
           have a matching ``tool`` result.  When a request is interrupted
           the history may end with an ``assistant(tool_calls=…)`` that
           has no follow-up tool result; the OpenAI API rejects this with a
           400.  Missing results get a synthetic ``Error: request cancelled
           by user`` result inserted right after the owning assistant
           message.
        2. **Alternating roles** — a history ending on a
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

        Images are NOT encoded here.  Attachments ride the ``attach_image``
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
        # history is already well-formed.
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
        """Append a runtime-only trim note (``trim_note``) to the last
        assistant message.

        Tells the LLM how many of its oldest turns were just cut from the
        context by a trim (tool results the model may still reference are
        gone).  Unlike the turn footnotes on user messages, this note
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
        marker = trim_note(count)
        content = msg.get("content") or ""
        msg["content"] = f"{content} {marker}".strip() if content else marker
        logger.debug("conv_trim_marker count=%d msg_idx=%d", count, idx)

    @staticmethod
    def strip_trim_markers(messages: list[dict]) -> list[dict]:
        """Return a copy of *messages* with the trim note removed.

        The note is runtime-only: this keeps it out of the diary.  The
        live history keeps it (the LLM needs to know the current cut); only
        what is persisted is cleaned.  Works on the list passed in — callers
        pass the sliced turn messages.  Only the assistant-message prose
        trim note is touched: the JSON turn footnote on user messages shares
        the ``[INFO: `` envelope but is not a trim note.
        """
        cleaned = []
        for m in messages:
            if m.get("role") == "assistant" and m.get("content"):
                content = m["content"]
                if isinstance(content, str):
                    start = _trim_note_in(content)
                    if start is not None:
                        m = dict(m)
                        m["content"] = content[:start].rstrip()
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

        Used by ``attach_image`` so the LLM sees images as vision
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
        """Return messages for the per-backend API mappers.

        Converts the internal ``thinking`` field to ``reasoning_content``
        which is the wire-format field DeepSeek / Qwen require when
        thinking mode is enabled.  The API returns a 400 error if
        reasoning_content is missing from *any* assistant message in the
        history — including synthetic harness messages that never
        carried reasoning.

        The internal ``is_error`` tool flag is NOT stripped here — it rides
        to the per-backend mappers so the Anthropic backend can emit the
        native ``tool_result.is_error``.  The OpenAI/Responses backends build
        their own wire dicts (the OpenAI builder strips ``is_error``), so the
        flag never reaches an API as an unknown field.
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
                # harness messages (_turn_prompt) that never carried
                # reasoning.
                m["reasoning_content"] = ""
            m.pop("images", None)  # internal attachment tracking
            cleaned.append(m)

        return cleaned

    def pop_last_turn(self) -> int:
        """Remove the last user turn and all subsequent messages.

        A "turn" starts with a user message and includes all assistant
        and tool messages that follow, up to the next user message or
        end of the list.  Used to rollback a failed turn so the
        history isn't poisoned for the next attempt.

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
        """Clear history, preserving system prompt if present."""
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
        results are preserved so the history stays well-formed.

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

        Uses the per-script heuristic in :func:`estimate_text_tokens`
        (narrow text ~3 chars/token, CJK/wide ~1 token per char).  This is a
        trim stop-condition, so it must never *under*-estimate a
        Chinese-heavy session — the ceiling/floor mechanism keeps 20%+ safety
        margins on top.
        """
        total = sum(estimate_message_tokens(m) for m in self.messages)
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
                        estimate_message_tokens(m) for m in current_turn
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
                    estimate_message_tokens(m) for m in current_turn
                ),
            })

        return turns

    def extract_oldest_turns(
        self,
        target: int,
    ) -> tuple[list[dict], int]:
        """Extract and delete oldest complete turns from the history.

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

            slice_msgs = self.messages[turn_start:turn_end]
            removed_messages.extend(slice_msgs)
            # Running total — subtract the removed slice's own estimate rather
            # than re-counting the whole remaining history each iteration
            # (the estimator is additive, so this stays exact).
            slice_tokens = sum(
                estimate_message_tokens(m) for m in slice_msgs
            )
            del self.messages[turn_start:turn_end]
            # Adjust last_user_idx — the slice we just deleted shifted
            # everything after it down.
            removed_count = turn_end - turn_start
            last_user_idx -= removed_count
            current -= slice_tokens

        if not removed_messages:
            return [], 0

        turns = MessageHistory.extract_turns(removed_messages)
        # Same estimator as count_tokens — the logged freed amount must match
        # what the running total subtracted.
        tokens_freed = sum(
            estimate_message_tokens(m) for m in removed_messages
        )
        return turns, max(tokens_freed, 1)

