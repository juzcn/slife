"""Live message history — the agent's turn-ordered message array (OpenAI message format).

Supports multimodal messages (text + images) for vision-capable models.
"""

import json
import logging
from pathlib import Path

from slife.logfmt import sanitize_secrets

logger = logging.getLogger(__name__)


# ── Token estimation ─────────────────────────────────────────────────────
# Every place that turns stored text into a token figure — count_tokens,
# extract_turns / extract_oldest_turns, the trim's stop condition, and the
# restore and recall budgets — shares ONE implementation, so the trim decision,
# its ``tokens_freed`` figure and those budgets can never disagree.
#
# The figure comes from tiktoken, the real BPE the OpenAI models use, rather
# than a character heuristic.  A heuristic was tried first and had to guess
# (CJK glyphs at ~1 token, Latin at ~3 chars/token); it was tunable but never
# right, and getting it wrong meant the window could sit genuinely over the
# ceiling.  tiktoken measures instead of guessing and handles CJK natively.

#: The BPE encoding.  ``o200k_base`` is OpenAI's current one (GPT-4o and
#: later) and is markedly better on CJK than the older ``cl100k_base``.  For a
#: non-OpenAI model it stands in for *that* model's tokenizer — the closest
#: locally available approximation, and exact whenever the model is OpenAI's.
_ENCODING_NAME = "o200k_base"
_encoding = None

#: Where tiktoken keeps its downloaded vocabularies.  Pinned explicitly
#: because tiktoken's own default is a *temp* directory — a cleaned temp dir
#: silently turns the next estimate back into a network fetch, and a fetch
#: with no reachable network blocks indefinitely rather than failing.
#: ``~/.cache`` is stable, outside the repo, and conventional.
_TIKTOKEN_CACHE_DIR = Path.home() / ".cache" / "tiktoken"

#: The cached vocabulary file (tiktoken names it after the sha1 of the blob URL
#: it downloads from) and its exact size.  The size is checked only for a file
#: that is *present*, and that asymmetry is the point: tiktoken does not
#: validate what it reads, so a truncated vocab is worse than a missing one —
#: it looks present, the fetch is skipped, and the encoding is silently wrong.
#: Update both if ``_ENCODING_NAME`` changes.
_VOCAB_FILE = _TIKTOKEN_CACHE_DIR / "fb374d419588a4632f3f557e76b4b70aebbca790"
_VOCAB_BYTES = 3613922


class TokenizerUnavailable(RuntimeError):
    """No token figure can be measured — the BPE vocabulary is unusable.

    An *environment* failure (a vocabulary present but not whole, or a fetch
    that landed short), never an input outcome.  Its own type rather than a bare
    ``RuntimeError`` so a caller that answers other failures with a
    default can tell this one apart and fail closed instead: an estimate
    that silently becomes a default is indistinguishable from a real one
    downstream, and the values built on it — trim decisions, budgets,
    budgets' selections — then read as measured.  Subclasses
    ``RuntimeError`` so a handler already treating this as a runtime
    failure still catches it.
    """


def _vocab_size():
    """The cached vocabulary's size in bytes, or ``None`` when it is absent."""
    try:
        return _VOCAB_FILE.stat().st_size
    except OSError:
        return None


def _get_encoding():
    """Return the BPE encoding, letting tiktoken fetch the vocabulary if needed.

    Lazy on purpose: ``tiktoken.get_encoding`` fetches the vocabulary on its
    first call, and this module is imported by plugin processes whose startup
    must stay handshake-fast.  Deferring pushes that fetch to the first
    estimate (after the handshake) instead of import time.

    A **missing** vocabulary is fetched — :data:`_TIKTOKEN_CACHE_DIR` is pinned
    so the download lands somewhere stable and every later process reads it from
    disk.  The installers pre-fetch it, so the ordinary path never reaches the
    network, which is worth keeping: tiktoken reads the vocab over HTTP with no
    timeout of its own, so on a host where that transfer stalls (a TUN/fake-ip
    proxy throttles it to a crawl — measured at ~6 KB/s, and the vocab is
    3.6 MB) it hangs the caller forever instead of erroring.

    A **present but wrong-sized** vocabulary is refused instead, and the
    asymmetry is deliberate: tiktoken does not validate what it reads, so a
    truncated file looks present, skips the fetch, and silently mis-counts every
    figure built on it.  Deleting the file restores the fetch.
    """
    global _encoding
    if _encoding is None:
        import os

        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(_TIKTOKEN_CACHE_DIR))
        size = _vocab_size()
        if size is not None and size != _VOCAB_BYTES:
            raise TokenizerUnavailable(
                f"tiktoken vocabulary for {_ENCODING_NAME} is truncated at "
                f"{_VOCAB_FILE} ({size} bytes, expected {_VOCAB_BYTES}) — a "
                f"partial vocabulary is read as a whole one and would "
                f"mis-count; delete the file and it will be fetched again",
            )
        import tiktoken

        _encoding = tiktoken.get_encoding(_ENCODING_NAME)
        # A fetch that stopped short leaves exactly the file the check above
        # guards against, and by now tiktoken has already loaded it — so the
        # check is repeated rather than assumed.  An *absent* file here is not a
        # failure: it means tiktoken found the vocabulary where it did not look.
        size = _vocab_size()
        if size is not None and size != _VOCAB_BYTES:
            _encoding = None
            raise TokenizerUnavailable(
                f"tiktoken vocabulary for {_ENCODING_NAME} is truncated at "
                f"{_VOCAB_FILE} ({size} bytes, expected {_VOCAB_BYTES}) — the "
                f"download did not complete; delete the file and retry",
            )
    return _encoding


def estimate_text_tokens(text: str) -> int:
    """Token count of *text*, measured with the BPE encoding.

    ``disallowed_special=()`` matters: without it tiktoken *raises* on text
    containing a special token's literal spelling (``<|endoftext|>``), which
    any tool result or pasted document may contain.
    """
    if not text:
        return 0
    return len(_get_encoding().encode(text, disallowed_special=()))


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


def estimate_turn_tokens(turn: dict) -> int:
    """Estimate the *incremental* token cost of one stored turn row.

    Counts the user message plus the stored assistant/tool messages using the
    same per-script estimator as :meth:`MessageHistory.count_tokens` (narrow
    ~3 chars/token, CJK/wide ~1 token per char) — a budget that under-counts a
    Chinese-heavy session lets the next request overflow the context window.
    Returns at least 1 so a zero-content turn still counts.

    Lives here beside its two primitives rather than in the restore UI: it is
    a pure turn→tokens read, and callers outside the TUI (a plugin sizing a
    recall selection) need it without importing Textual.
    """
    user = turn.get("user_message", "") or ""
    messages = turn.get("messages", "[]")
    if isinstance(messages, str):
        try:
            messages = json.loads(messages)
        except Exception:
            messages = "[]"
    if isinstance(messages, list):
        body = sum(estimate_message_tokens(m) for m in messages)
    else:
        body = estimate_text_tokens(str(messages))
    return max(estimate_text_tokens(user) + body, 1)


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
#: One envelope — ``[A2A:…]`` — prefixes every inbound A2A message (task,
#: auto-pushed result, conversation, broadcast); ``type`` in the JSON says
#: which.  The peer rides under the key ``from`` — deliberately NOT
#: ``agent_name``, which in the system prompt
#: names the agent's *own* identity; a receiver can then never misread
#: the marker as telling it who it is.  ``type`` tells what the message is
#: (``task_request`` = answer it, ``task_response`` = a pushed result,
#: ``cancel_task`` = the sender withdrew it, ``message`` = a conversation,
#: ``broadcast`` = an event); ``task_id`` rides for tasks, results and
#: withdrawals.  Machine-facing — the TUI shows the ``A2A(<name>)> ``
#: bubble prefix and ``unwrap_info_envelope`` strips them for display.
A2A_PREFIX = "[A2A:"
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


def messages_from_turns(
    turns: list[dict],
    *,
    system_message: dict | None = None,
) -> list[dict]:
    """Build a message list from stored turn rows — the ONE turn→messages
    builder, shared by session restore and the per-turn rebuild.

    Sharing it is a requirement, not tidiness: a rebuilt turn must render
    byte-identically to the same turn restored, or the difference costs a
    prompt-cache miss on every rebuild.

    *turns* is **oldest-first**.  ``messages[0]`` is copied from
    *system_message* (never re-rendered), then each turn contributes its user
    message — carrying the ``[INFO: {…}]`` footnote — followed by its stored
    ``messages`` slice.

    A turn's image blocks are deliberately absent: they are live-session state
    (:meth:`inject_images_to_last_user`) and are never persisted, so a rebuilt
    turn carries the ``attach_image`` call and its result — which name every
    source — and the model re-attaches from that when it needs the pixels.
    """
    built: list[dict] = []
    if system_message:
        built.append(dict(system_message))

    for turn in turns:
        user_text = turn.get("user_message", "")
        stored = turn.get("messages", "[]")
        turn_msgs: list[dict] = (
            json.loads(stored) if isinstance(stored, str) else stored
        )
        # Every turn carries the footnote, autonomous ones included — it is
        # what makes a turn addressable, and the keep-list addresses turns by
        # exactly this id (see ``AgentService._annotate_saved_turn``).
        header = turn_header(turn)

        rowid = turn.get("rowid")
        user_msg: dict = {
            "role": "user",
            "content": user_text + (" " + header if header else ""),
        }
        # The structural turn id rides the message so the loop can map an
        # in-context turn back to its diary row.  Runtime-only: it is popped
        # before the wire and never part of a stored ``messages`` slice.
        if rowid is not None:
            user_msg["_turn_id"] = rowid
        built.append(user_msg)
        built.extend(turn_msgs)

    return built


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


def a2a_marker(
    agent_name: str, task_id: str | None = None, type: str = "task_request",
) -> str:
    """Content prefix for ANY inbound A2A message — one envelope, a type field.

    ``[A2A:{"from": …, "task_id": …, "type": …}] `` — ``from`` names the
    *sending* peer (never the receiver).  ``type`` tells what it is:
    ``task_request`` (an inbound task to answer, via
    ``a2a_send_message(message_type="task_response", task_id=…)`` — it may
    take many turns), ``task_response`` (an auto-delivered task result),
    ``cancel_task`` (the peer withdrew that task — its completion bridge died
    with it, so a ``task_response`` for it is refused: answer with a plain
    message or stay silent), ``message`` (a bare conversation),
    ``broadcast`` (a fire-and-forget event, informational).  ``task_id``
    rides for tasks, results and withdrawals.  The key is
    ``from``, not ``agent_name`` — that word in the system prompt is the
    agent's own identity, so ``from`` keeps the marker unmistakably
    directional.  The marker is machine-facing; ``unwrap_info_envelope``
    drops it for display (the TUI shows the ``A2A(<name>)> `` bubble prefix
    instead).
    """
    # ``from`` — the peer's name — deliberately not ``agent_name``: that word
    # is the agent's self-identity in the system prompt, and reusing it here
    # made receivers misread the sender as themselves.
    payload: dict[str, str] = {"from": agent_name, "type": type}
    if task_id is not None:
        payload["task_id"] = task_id
    return f"{A2A_PREFIX}{json.dumps(payload, ensure_ascii=False)}] "


#: The A2A ``type`` values :func:`a2a_message_type` may return — the inbound
#: message kinds, each a distinct display label.
A2A_TYPES = ("task_request", "task_response", "cancel_task", "message", "broadcast")


def a2a_message_type(text: str) -> str | None:
    """The wire ``type`` of an inbound A2A message, read from its marker.

    The marker is the one envelope that classifies every inbound A2A message
    (:func:`a2a_marker`), and it is what the receiver's stored turn carries —
    so the TUI's turn-end line reads the type from here instead of threading
    a parallel field through the inbox.  Returns one of :data:`A2A_TYPES`, or
    ``None`` when *text* carries no (or a malformed) A2A marker, or a type
    outside the set — the caller then falls back to a type-less label.
    """
    if not text.startswith(A2A_PREFIX):
        return None
    end = text.find("]", len(A2A_PREFIX))
    if end == -1:
        return None
    try:
        payload = json.loads(text[len(A2A_PREFIX):end])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    mtype = payload.get("type")
    return mtype if mtype in A2A_TYPES else None


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
    - a leading ``[A2A:…]`` envelope is dropped entirely — the channel is
      already shown by the ``A2A(<name>)> `` bubble prefix.

    Only the *trailing* INFO envelope is unwrapped (``rfind``) — an
    annotation is always a suffix, and a user message may legitimately
    contain the literal ``[INFO:`` in prose.  Only *leading* Wechat, Subagent
    and A2A (plus legacy WECHAT) markers — exactly our injected prefixes —
    are stripped.  Returns *text* unchanged when no marker is present.
    """
    display, _span = _unwrap_with_footnote(text)
    return display


def info_footnote_span(text: str) -> tuple[int, int] | None:
    """Display-side half-open span of the unwrapped ``[INFO: …]`` payload.

    A caller that styles the footnote (the TUI's dim-italic suffix) needs the
    span in *display* coordinates: the raw-text ``rfind`` index is shifted by
    any leading channel marker :func:`unwrap_info_envelope` strips.  Returns
    None when no INFO envelope is present.
    """
    _display, span = _unwrap_with_footnote(text)
    return span


def _unwrap_with_footnote(text: str) -> tuple[str, tuple[int, int] | None]:
    """Shared engine behind :func:`unwrap_info_envelope` and
    :func:`info_footnote_span`: the display string plus the half-open span
    of the unwrapped INFO payload in that string.
    """
    if text.startswith(WECHAT_MARKER):
        text = text[len(WECHAT_MARKER):]
    for prefix in (WECHAT_PREFIX, SUBAGENT_PREFIX, A2A_PREFIX):
        if text.startswith(prefix):
            end = text.find("]", len(prefix))
            if end != -1:
                text = text[end + 1:].lstrip()
    start = text.rfind(INFO_PREFIX)
    if start == -1:
        return text, None
    payload = text[start + len(INFO_PREFIX):]
    if payload.endswith("]"):
        payload = payload[:-1]
    return text[:start] + payload, (start, start + len(payload))


#: The closing assistant message for a turn that ended before the model
#: answered — ``(Turn interrupted, reason: esc)``.  Parenthesized like every
#: other synthetic harness line: this is the harness speaking, not the model.
#:
#: The reason is a SHORT TOKEN, never raw exception text — the line lands in
#: the LLM's context and in the diary, and an error string can carry secrets:
#:
#:   ``esc``               the user pressed Esc / the turn was cancelled
#:   ``parent``            a worker's parent cancelled the task it was running
#:   ``shutdown``          the app is quitting — the process is being torn down
#:   ``max_iterations``    the configured iteration cap was hit
#:   ``error (400 invalid_request_error: model not found)``  the turn died on
#:                         an exception — the HTTP status and the provider's
#:                         error code when the SDK exposes them, else the
#:                         exception's class name, then the message (scrubbed
#:                         by the same gate as any user text, one line,
#:                         bounded — ``inbox._error_reason``).  A
#:                         content-filter reject never reaches this line:
#:                         that turn is rolled back, not saved.
#:
#: No caller passes one yet — the repair knows only *that* the turn ended
#: early, not why — so every repaired turn currently reads ``---``.  The slot
#: is the contract; filling it means threading the loop's terminal state down
#: to the save point.
_REASON_NOT_RECORDED = "---"


def interrupted_note(stop_reason: str = "") -> str:
    """The standardized closing line for a turn that ended early.

    Only *why* it stopped — no advice.  The reader is the model itself: a 429
    or an ``esc`` already carries what to do next, and this line stays in the
    context for the rest of the session, so anything it can derive is ballast.
    """
    return f"(Turn interrupted, reason: {stop_reason or _REASON_NOT_RECORDED})"


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

    def rebuild_messages(
        self,
        turns: list[dict],
    ) -> int:
        """Replace the context with a rebuild from *turns* (oldest-first).

        The per-turn rebuild: `messages[0]` — the system prompt — is preserved
        byte-identically (copied, never re-rendered, so the cached prefix
        survives), and everything after it is regenerated from the stored
        turns by :func:`messages_from_turns`, the same builder session restore
        uses.  Sharing that builder is what makes a rebuilt turn render
        identically to a restored one.

        In place, and never by rebinding the object: the loop, the TUI handler
        and ``save_to_memory`` all hold this instance.

        *turns* is sorted by rowid defensively.  The list order is a contract
        (restore reads the last entry as the newest, the renderer pairs
        position with message order), so a caller passing a scrambled list
        would otherwise render a garbled conversation silently.

        Returns the new message count.
        """
        ordered = sorted(turns, key=lambda t: t.get("rowid") or 0)
        if [t.get("rowid") for t in ordered] != [t.get("rowid") for t in turns]:
            logger.warning(
                "rebuild_reordered turns=%d — caller passed a non-chronological "
                "selection", len(turns),
            )

        sys_msg = (
            self.messages[0]
            if self.messages and self.messages[0].get("role") == "system"
            else None
        )
        self.messages = messages_from_turns(ordered, system_message=sys_msg)
        # The one invariant enforcer — a rebuild splices an arbitrary turn set,
        # so it gets the same guarantee on load that a restored history does.
        self._ensure_turn_consistent()
        return len(self.messages)

    def _ensure_turn_consistent(self, stop_reason: str = "") -> int:
        """Restore the history to a consistent state.

        Two idempotent invariants for a turn that may have ended early
        (cancelled / errored / max-iteration, or restored from memory):

        1. **No orphaned tool_calls** — every assistant ``tool_call`` must
           have a matching ``tool`` result.  When a request is interrupted
           the history may end with an ``assistant(tool_calls=…)`` that
           has no follow-up tool result; the OpenAI API rejects this with a
           400.  Each missing result gets a synthetic
           ``(Tool execution interrupted)`` inserted right after the owning
           assistant message.
        2. **Alternating roles** — a history ending on a
           ``user``/``tool`` message (a tool result is a ``user`` role on
           the Anthropic wire, which rejects two consecutive users) gets a
           closing assistant message so roles keep alternating.

        Repair runs first: a dangling call as the last message (role
        ``"assistant"``) becomes a ``"tool"`` role after repair, so the
        closing-assistant check below then fires correctly.

        *stop_reason* is WHY the turn ended early, in the short-token
        vocabulary :func:`interrupted_note` documents.  Nothing passes one
        today: this repair is reached from the save point and from load, and
        neither is told why the turn ended — so the closing line always reads
        ``reason: ---``.  The parameter is the slot for a caller that has the
        loop's terminal state in hand.

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
            self.add_assistant_message(content=interrupted_note(stop_reason))
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

    @staticmethod
    def strip_turn_ids(messages: list[dict]) -> list[dict]:
        """Return a copy of *messages* with the runtime turn ids removed.

        ``_turn_id`` is how the loop maps an in-context turn back to its
        diary row — the trim needs the real ids to drop them from the
        persisted live-context list.  It rides the message that *opens* a
        turn, and the persisted slice deliberately excludes that message
        (``save_to_memory`` stores ``all_messages[user_idx + 1:]`` and keeps
        the user text in its own column), so this is a persist-boundary
        invariant rather than a live leak being plugged: it keeps the
        ``messages`` column free of runtime keys if that slice ever widens.

        It never reaches the LLM wire either — :meth:`to_openai_messages`
        pops it on the way out.

        Only the user message that opens a turn carries one, so a message
        without the key is passed through untouched.
        """
        cleaned = []
        for m in messages:
            if "_turn_id" in m:
                m = {k: v for k, v in m.items() if k != "_turn_id"}
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

    def strip_images(self) -> int:
        """Remove every injected image block; return how many were removed.

        Called when the provider *rejected* a request that carried
        attachments (:func:`slife.agent.inbox._is_bad_request`).  A block
        lives in the session only — there is no column — so it rides every
        later request and is re-rejected there: one failed attach turned into
        a session that dropped every turn, from every source, until a
        restart.  A rejected attachment is not kept.

        A content list left holding only text parts collapses back to a plain
        string, concatenated exactly as :func:`messages_from_turns` renders a
        text-only turn (the injected footnote part carries its own leading
        space), so an evicted turn re-renders byte-identically and costs no
        prompt-cache miss.
        """
        removed = 0
        for msg in self.messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            kept = [
                p for p in content
                if not (isinstance(p, dict) and p.get("type") == "image_url")
            ]
            if len(kept) == len(content):
                continue
            removed += len(content) - len(kept)
            if not kept:
                msg["content"] = ""
            elif all(
                isinstance(p, dict) and p.get("type") == "text" for p in kept
            ):
                msg["content"] = "".join(str(p.get("text", "")) for p in kept)
            else:
                msg["content"] = kept
        if removed:
            logger.debug("conv_strip_images removed=%d", removed)
        return removed

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
            m.pop("_turn_id", None)  # internal diary-row mapping
            cleaned.append(m)

        return cleaned

    def _last_user_index(self) -> int | None:
        """Index of the last user message — the current turn's start.

        Shared by every turn-boundary operation (pop_last_turn,
        extract_oldest_turns); None when the history has no user message.
        """
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i]["role"] == "user":
                return i
        return None

    def pop_last_turn(self) -> int:
        """Remove the last user turn and all subsequent messages.

        A "turn" starts with a user message and includes all assistant
        and tool messages that follow, up to the next user message or
        end of the list.  Used to rollback a failed turn so the
        history isn't poisoned for the next attempt.

        Returns:
            Number of messages removed.
        """
        last_user_idx = self._last_user_index()
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
          - ``turn_id`` (int | None) — the turn's diary rowid when known
        """
        turns: list[dict] = []
        current_turn: list[dict] = []
        current_user_msg = ""

        def _turn_dict(msgs: list[dict], user_text: str) -> dict:
            first = msgs[0] if msgs else {}
            return {
                "user_message": user_text,
                "messages": list(msgs),
                "estimated_tokens": sum(
                    estimate_message_tokens(m) for m in msgs
                ),
                # The turn's diary rowid when known — runtime-only, stamped
                # on the opening user message at save and on restore.  The
                # trim reads it to drop the evicted turns from the persisted
                # live-context list.
                "turn_id": first.get("_turn_id"),
            }

        for msg in messages:
            role = msg.get("role", "")
            if role == "user" and current_turn:
                turns.append(_turn_dict(current_turn, current_user_msg))
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
            turns.append(_turn_dict(current_turn, current_user_msg))

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
        last_user_idx = self._last_user_index()
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

