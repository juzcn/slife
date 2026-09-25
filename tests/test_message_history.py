"""Tests for slife.agent.message_history — live message-history management."""

import pytest; pytestmark = pytest.mark.unit


import json

import pytest

from slife.agent.message_history import (
    MessageHistory,
    a2a_marker,
    a2a_message_type,
    info_footnote_span,
    messages_from_turns,
    subagent_marker,
    unwrap_info_envelope,
    wechat_marker,
)


# ── A2A marker type ───────────────────────────────────────────────────


class TestA2AMessageType:
    """a2a_message_type — the wire type read back off a message's marker.

    The TUI's turn-end line names what arrived (task_request / task_response
    / cancel_task / message / broadcast) from here, so the reader has to agree
    with a2a_marker round-trip and degrade to None on anything else.
    """

    def test_reads_every_type(self):
        for mtype in (
            "task_request", "task_response", "cancel_task",
            "message", "broadcast",
        ):
            text = f"{a2a_marker('Jack', 'cid-1', type=mtype)}do X"
            assert a2a_message_type(text) == mtype

    def test_none_without_an_a2a_marker(self):
        assert a2a_message_type("plain text") is None
        assert a2a_message_type("[Wechat:{}] hi") is None
        assert a2a_message_type("") is None

    def test_none_on_malformed_or_unknown_payload(self):
        # Unterminated envelope, bad JSON, non-object JSON, and a type
        # outside the set all degrade to None (the caller shows a
        # type-less label rather than rendering wire junk).
        assert a2a_message_type('[A2A:{"from": "Jack"') is None
        assert a2a_message_type("[A2A:not-json] do X") is None
        assert a2a_message_type('[A2A:["Jack"]] do X') is None
        assert a2a_message_type('[A2A:{"from": "J", "type": "ping"}] x') is None

    def test_literal_marker_in_body_is_not_read(self):
        # Only the LEADING envelope classifies the message — a peer's text
        # quoting [A2A:…] must not be mistaken for its own type.
        assert a2a_message_type('do X [A2A:{"type": "broadcast"}]') is None


# ── Display unwrapping ────────────────────────────────────────────────


class TestUnwrapInfoEnvelope:
    """Tests for unwrap_info_envelope — the TUI display form of markers."""

    def test_no_marker_passes_through(self):
        assert unwrap_info_envelope("plain text") == "plain text"

    def test_strips_leading_wechat_marker(self):
        assert unwrap_info_envelope("[WECHAT] 你好") == "你好"

    def test_wechat_literal_in_body_left_alone(self):
        # Only the injected leading marker is stripped — a literal
        # "keep looking at [WECHAT]" in the message body is user text.
        assert unwrap_info_envelope("[WECHAT] keep [WECHAT] in text") == "keep [WECHAT] in text"

    def test_unwraps_trailing_info_envelope(self):
        assert unwrap_info_envelope(
            'hello [INFO: {"turn_id": 5}]'
        ) == 'hello {"turn_id": 5}'

    def test_wechat_marker_then_info_envelope(self):
        assert unwrap_info_envelope(
            '[WECHAT] hello [INFO: {"turn_id": 5}]'
        ) == 'hello {"turn_id": 5}'

    def test_strips_leading_wechat_json_marker(self):
        # The [Wechat:…] envelope names the peer+thread for the LLM; the
        # TUI drops it (the channel already shows as Wechat>).
        assert unwrap_info_envelope(
            '[Wechat:{"peer_wechat_id": "wx_1", "context_token": "c1"}] 你好'
        ) == "你好"
        # The legacy prose marker on old stored rows is stripped too.
        assert unwrap_info_envelope("[WECHAT] old row") == "old row"
        # Strips exactly what the builder emits.
        assert unwrap_info_envelope(wechat_marker("wx_1", "c1") + "hi") == "hi"

    def test_wechat_marker_builds_json_payload(self):
        # Keys mirror the wechat_send_message arguments (peer/context).
        assert wechat_marker("wx_1", "c1") == (
            '[Wechat:{"peer_wechat_id": "wx_1", "context_token": "c1"}] '
        )
        assert wechat_marker("wx_1") == (
            '[Wechat:{"peer_wechat_id": "wx_1"}] '
        )

    def test_wechat_json_marker_then_info_envelope(self):
        assert unwrap_info_envelope(
            '[Wechat:{"peer_wechat_id": "wx_1"}] hello [INFO: {"turn_id": 5}]'
        ) == 'hello {"turn_id": 5}'

    def test_info_footnote_span_is_display_relative(self):
        """The footnote span is measured against the DISPLAY string, so a
        leading channel envelope (stripped by unwrap) can't push the raw-text
        index past the end of the rendered text."""
        text = '[A2A:{"from": "peer-1", "type": "task_response", "task_id": "c"}] done [INFO: {"turn_id": 5}]'
        display = unwrap_info_envelope(text)
        span = info_footnote_span(text)
        assert display == 'done {"turn_id": 5}'
        assert span == (display.index('{"turn_id": 5}'),
                        display.index('{"turn_id": 5}') + len('{"turn_id": 5}'))
        # Styling with this span stays inside the display bounds — the old
        # raw-text index would have overshot after the stripped envelope.
        start, end = span
        assert start >= 0 and end <= len(display)
        # No INFO envelope → no span.
        assert info_footnote_span("plain text") is None
        assert info_footnote_span('[Wechat:{"peer_wechat_id": "wx_1"}] hi') is None

    def test_a2a_marker_carries_type(self):
        # One envelope — [A2A:{from, task_id?, type}] — a type field tells
        # what the message is; `from` names the SENDING peer (not agent_name).
        assert a2a_marker("Jack", "cid-1") == (
            '[A2A:{"from": "Jack", "type": "task_request", "task_id": "cid-1"}] '
        )
        assert a2a_marker("peer-1", "c-x", type="task_response") == (
            '[A2A:{"from": "peer-1", "type": "task_response", "task_id": "c-x"}] '
        )
        assert a2a_marker("Jack", type="message") == (
            '[A2A:{"from": "Jack", "type": "message"}] '
        )
        # A broadcast event names only the publishing peer + its type.
        assert a2a_marker("peer-9", type="broadcast") == (
            '[A2A:{"from": "peer-9", "type": "broadcast"}] '
        )

    def test_strips_leading_a2a_markers(self):
        # Every A2A envelope is the single [A2A:…] prefix, dropped for display
        # (the channel already shows as A2A(<name>)>).
        assert unwrap_info_envelope(
            '[A2A:{"from": "Jack", "type": "task_request", "task_id": "cid-1"}] do X'
        ) == "do X"
        assert unwrap_info_envelope(
            '[A2A:{"from": "Jack", "type": "task_response", "task_id": "cid-1"}] the answer'
        ) == "the answer"
        assert unwrap_info_envelope(
            '[A2A:{"from": "Jack", "type": "broadcast"}] all hands on deck'
        ) == "all hands on deck"
        # A literal [A2A: bracket in the body survives.
        assert unwrap_info_envelope(
            '[A2A:{"from": "Jack"}] see [A2A: literally]'
        ) == "see [A2A: literally]"

    def test_strips_leading_subagent_marker(self):
        # The [Subagent:…] envelope names the worker+task for the LLM; the
        # TUI drops it (the channel already shows as Subagent(<name>)>).
        assert unwrap_info_envelope(
            '[Subagent:{"subagent_name": "researcher", "task_id": "t-3"}] done'
        ) == "done"

    def test_subagent_marker_builds_json_payload(self):
        # Keys mirror the LLM-facing tool arguments (subagent_name / task_id).
        assert subagent_marker("researcher", "t-3") == (
            '[Subagent:{"subagent_name": "researcher", "task_id": "t-3"}] '
        )
        assert subagent_marker("researcher") == (
            '[Subagent:{"subagent_name": "researcher"}] '
        )

    def test_subagent_literal_in_body_left_alone(self):
        # Only the injected leading envelope is stripped.
        assert unwrap_info_envelope(
            '[Subagent:{"subagent_name": "researcher", "task_id": "t-3"}] '
            "read [Subagent: literally]"
        ) == "read [Subagent: literally]"

    def test_restored_subagent_turn_renders_clean(self):
        # A restored subagent turn carries the marker AND the trailing INFO
        # footnote — neither reaches the human; the bubble reads clean.
        assert unwrap_info_envelope(
            '[Subagent:{"subagent_name": "researcher", "task_id": "t-3"}] '
            "the result [INFO: {\"turn_id\": 5}]"
        ) == "the result {\"turn_id\": 5}"


# ── Construction ─────────────────────────────────────────────────────


class TestMessageHistoryConstruction:
    """Tests for MessageHistory.__init__."""

    def test_empty_history(self):
        """MessageHistory starts with no messages when no system prompt."""
        conv = MessageHistory()
        assert conv.messages == []

    def test_with_system_prompt(self):
        """System prompt creates initial system message."""
        conv = MessageHistory(system_prompt="You are helpful.")
        assert len(conv.messages) == 1
        assert conv.messages[0]["role"] == "system"
        assert conv.messages[0]["content"] == "You are helpful."

    def test_from_history_seeds_messages(self):
        """from_history prepends a fresh system prompt and skips inherited system."""
        conv = MessageHistory.from_history(
            "SUB_SYS",
            [
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "system", "content": "skip me"},
            ],
        )
        assert [m["role"] for m in conv.messages] == ["system", "user", "assistant"]
        assert conv.messages[0]["content"] == "SUB_SYS"
        # inherited system message is dropped; source not mutated
        assert conv.messages[1]["content"] == "a"

    def test_from_history_repairs_a_snapshot_taken_mid_turn(self):
        """The clone is repaired — a snapshot taken mid-turn is not API-valid.

        A subagent's clone is taken *inside* the tool call that spawned it, so
        the parent's last message is the ``assistant(tool_calls=…)`` whose
        results do not exist yet.  Sent as-is, every provider rejects it
        ("tool_calls must be followed by tool messages") and a worker — which
        fails fast, with no retry — would reject every cloned task.
        """
        source = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "spawn_subagent", "arguments": "{}"}},
            ]},
        ]
        conv = MessageHistory.from_history("SUB_SYS", source)

        assert [m["role"] for m in conv.messages] == [
            "system", "user", "assistant", "tool", "assistant",
        ]
        assert conv.messages[3]["tool_call_id"] == "c1"
        assert conv.messages[3]["is_error"] is True
        # On the wire every tool_call has its result — the shape a provider
        # validates (this is the assertion that would have caught the clone).
        wire = conv.to_openai_messages()
        called = [tc["id"] for m in wire for tc in (m.get("tool_calls") or [])]
        answered = [m["tool_call_id"] for m in wire if m.get("role") == "tool"]
        assert called and set(called) <= set(answered)
        # The snapshot's own dicts are untouched — the parent's history must not
        # grow a synthetic tool result from someone else's clone.
        assert source[1]["tool_calls"][0]["id"] == "c1"
        assert len(source) == 2

    def test_from_history_leaves_a_complete_history_alone(self):
        """A clone of a settled history is copied, not rewritten."""
        source = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        conv = MessageHistory.from_history("SUB_SYS", source)
        assert [m["role"] for m in conv.messages] == ["system", "user", "assistant"]
        assert conv.messages[2]["content"] == "b"

    def test_none_system_prompt(self):
        """None system prompt results in empty list."""
        conv = MessageHistory(system_prompt=None)
        assert conv.messages == []


# ── add_user_message ─────────────────────────────────────────────────


class TestAddUserMessage:
    """Tests for MessageHistory.add_user_message."""

    def test_plain_text(self):
        """Plain text message without images."""
        conv = MessageHistory()
        conv.add_user_message("Hello!")
        assert len(conv.messages) == 1
        assert conv.messages[0]["role"] == "user"
        assert conv.messages[0]["content"] == "Hello!"

    def test_verbatim_text_only(self):
        """add_user_message is text-only — the user's text is stored
        verbatim.  Images arrive via attach_image's
        inject_images_to_last_user, never encoded here."""
        conv = MessageHistory()
        conv.add_user_message("图片中有什么 @D:\\Downloads\\奇点.png")
        assert len(conv.messages) == 1
        assert conv.messages[0]["role"] == "user"
        assert conv.messages[0]["content"] == "图片中有什么 @D:\\Downloads\\奇点.png"

    def test_inject_appends_block_to_last_user(self):
        """inject_images_to_last_user turns the verbatim text message into a
        multimodal list and appends the image block — the text survives."""
        conv = MessageHistory()
        conv.add_user_message("look at this")
        conv.inject_images_to_last_user([
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ])
        assert conv.messages[0]["content"] == [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]

    def test_sanitizes_api_keys(self):
        """User input with API key patterns is sanitized before storage."""
        conv = MessageHistory()
        conv.add_user_message("My key is sk-ant-api03-abc123def456ghi789jkl")
        assert "sk-ant-api03-abc123def456ghi789jkl" not in conv.messages[0]["content"]
        assert "<MASKED>" in conv.messages[0]["content"]

    def test_normal_input_passes_through(self):
        """Normal user input without secrets is unchanged."""
        conv = MessageHistory()
        conv.add_user_message("What is the weather today?")
        assert conv.messages[0]["content"] == "What is the weather today?"

    def test_input_sanitization_idempotent(self):
        """Double sanitization produces the same result."""
        conv = MessageHistory()
        conv.add_user_message("api_key=sk-test-key-xxxxyyyyzzzz11112222")
        first = conv.messages[0]["content"]
        # Reset and add already-sanitized content
        conv2 = MessageHistory()
        conv2.add_user_message(first)
        assert conv2.messages[0]["content"] == first


# ── add_assistant_message ────────────────────────────────────────────


class TestStripImages:
    """strip_images — an attachment the provider rejected is not kept.

    A block lives in the session only, so nothing else removes it: left in
    place it rides every later request and is re-rejected there.
    """

    def test_restores_the_text_only_string(self):
        """The list collapses back to exactly what the same turn renders
        without images, so an evicted turn costs no prompt-cache miss."""
        conv = MessageHistory()
        conv.add_user_message("look at this")
        conv.inject_images_to_last_user([
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ])
        assert conv.strip_images() == 1
        assert conv.messages[0]["content"] == "look at this"

    def test_keeps_text_parts_around_the_block(self):
        """The injected footnote part carries its own leading space, so
        concatenating the text parts reproduces the text-only render."""
        conv = MessageHistory()
        conv.add_user_message("what is this")
        conv.inject_images_to_last_user([
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            {"type": "text", "text": " [2026-09-23 17:17]"},
        ])
        assert conv.strip_images() == 1
        assert conv.messages[0]["content"] == "what is this [2026-09-23 17:17]"

    def test_removes_from_every_message(self):
        """A rebuild re-attaches blocks per turn, so more than one turn can
        carry them — this is not only the last user message."""
        conv = MessageHistory()
        block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        conv.add_user_message("one")
        conv.inject_images_to_last_user([block])
        conv.add_assistant_message("seen")
        conv.add_user_message("two")
        conv.inject_images_to_last_user([block, block])
        assert conv.strip_images() == 3
        assert conv.messages[0]["content"] == "one"
        assert conv.messages[2]["content"] == "two"

    def test_noop_without_images(self):
        conv = MessageHistory()
        conv.add_user_message("plain")
        conv.add_assistant_message("hi")
        assert conv.strip_images() == 0
        assert conv.messages[0]["content"] == "plain"


class TestMessagesFromTurns:
    """messages_from_turns — the shared turn→messages builder.

    A rebuilt turn carries no image blocks.  They are live-session state
    (``inject_images_to_last_user``) and are never persisted, so a recalled
    turn comes back as its text plus whatever the stored slice holds — the
    ``attach_image`` call and its result, which name every source the model
    needs to re-attach by itself.  Pinned here so the block branch cannot
    creep back in.
    """

    def test_image_turn_rebuilds_to_text_plus_its_stored_slice(self):
        turn = {
            "rowid": 7,
            "created_at": "2026-09-23 10:00:00",
            "completed_at": "2026-09-23 10:01:00",
            "user_message": "what is in @shot.png",
            "messages": json.dumps([
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "_harness_attach_image_1",
                        "type": "function",
                        "function": {
                            "name": "attach_image",
                            "arguments": json.dumps({"sources": ["shot.png"]}),
                        },
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": "_harness_attach_image_1",
                    "content": "Image included: shot.png",
                },
                {"role": "assistant", "content": "a red square"},
            ]),
        }

        messages = messages_from_turns([turn])

        # Plain text with the footnote — never a multimodal content list,
        # whatever the live turn carried.
        assert messages[0]["role"] == "user"
        assert isinstance(messages[0]["content"], str)
        assert messages[0]["content"].startswith("what is in @shot.png ")
        assert "[INFO: " in messages[0]["content"]
        # The turn's identity still rides the message for the trim.
        assert messages[0]["_turn_id"] == 7
        # And the source survives, in the call and in its result.
        call = messages[1]["tool_calls"][0]["function"]
        assert call["name"] == "attach_image"
        assert "shot.png" in call["arguments"]
        assert messages[2]["content"] == "Image included: shot.png"
        assert messages[3]["content"] == "a red square"


class TestAddAssistantMessage:
    """Tests for MessageHistory.add_assistant_message."""

    def test_content_only(self):
        conv = MessageHistory()
        conv.add_assistant_message("I'm fine, thanks!")
        assert conv.messages[0]["role"] == "assistant"
        assert conv.messages[0]["content"] == "I'm fine, thanks!"
        assert "tool_calls" not in conv.messages[0]

    def test_content_none_replaced_with_empty_string(self):
        """None content is replaced with empty string."""
        conv = MessageHistory()
        conv.add_assistant_message(None)
        assert conv.messages[0]["content"] == ""

    def test_with_tool_calls(self):
        conv = MessageHistory()
        tool_calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"query":"hi"}'},
            }
        ]
        conv.add_assistant_message("Let me search.", tool_calls=tool_calls)
        assert conv.messages[0]["tool_calls"] == tool_calls

    def test_none_content_with_tool_calls(self):
        conv = MessageHistory()
        conv.add_assistant_message(None, tool_calls=[{"id": "x"}])
        assert conv.messages[0]["content"] == ""
        assert conv.messages[0]["tool_calls"] == [{"id": "x"}]


# ── add_tool_result ──────────────────────────────────────────────────


class TestAddToolResult:
    """Tests for MessageHistory.add_tool_result."""

    def test_adds_tool_result(self):
        conv = MessageHistory()
        conv.add_tool_result("call_abc", "Search results here.")
        assert conv.messages[0]["role"] == "tool"
        assert conv.messages[0]["tool_call_id"] == "call_abc"
        assert conv.messages[0]["content"] == "Search results here."

    def test_is_error_defaults_to_false(self):
        conv = MessageHistory()
        conv.add_tool_result("call_abc", "ok output")
        assert conv.messages[0]["is_error"] is False

    def test_is_error_stored_when_true(self):
        """The error flag is persisted so restore can render it."""
        conv = MessageHistory()
        conv.add_tool_result("call_abc", "Error: boom.", is_error=True)
        assert conv.messages[0]["is_error"] is True


# ── to_openai_messages ───────────────────────────────────────────────


class TestToOpenAIMessages:
    """Tests for MessageHistory.to_openai_messages."""

    def test_returns_copy(self):
        """Returns a copy, not the internal list."""
        conv = MessageHistory(system_prompt="You are helpful.")
        msgs = conv.to_openai_messages()
        msgs.append({"role": "user", "content": "extra"})
        assert len(conv.messages) == 1  # Original unchanged

    def test_full_message_flow(self):
        """Complete history flow produces correct message order."""
        conv = MessageHistory(system_prompt="Be concise.")
        conv.add_user_message("What is 2+2?")
        conv.add_assistant_message("4")
        conv.add_user_message("And 3+3?")
        conv.add_assistant_message("6")

        msgs = conv.to_openai_messages()
        assert len(msgs) == 5
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert msgs[2]["role"] == "assistant"
        assert msgs[3]["role"] == "user"
        assert msgs[4]["role"] == "assistant"

    def test_tool_call_flow(self):
        """Assistant+tool result flow is correctly ordered."""
        conv = MessageHistory()
        conv.add_user_message("Search for cats")
        conv.add_assistant_message(
            None,
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": '{"query":"cats"}'}}]
        )
        conv.add_tool_result("c1", "Cat results...")
        conv.add_assistant_message("Here are the results.")

        msgs = conv.to_openai_messages()
        assert len(msgs) == 4
        roles = [m["role"] for m in msgs]
        assert roles == ["user", "assistant", "tool", "assistant"]

    def test_is_error_rides_to_backend_mappers_not_openai_wire(self):
        """is_error is internal — it rides through to_openai_messages so the
        Anthropic backend can map it to the native tool_result.is_error, but
        the OpenAI HTTP builder strips it (never sent to the API)."""
        from slife.agent.llm_backends.openai import OpenAIBackend

        conv = MessageHistory()
        conv.add_user_message("run it")
        conv.add_assistant_message(
            None,
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
        )
        conv.add_tool_result("c1", "Error: failed.", is_error=True)
        conv.add_assistant_message("done")

        msgs = conv.to_openai_messages()
        tool_msg = next(m for m in msgs if m["role"] == "tool")
        # The flag rides to the per-backend mappers…
        assert tool_msg["is_error"] is True
        assert tool_msg["content"] == "Error: failed."

        # …but the OpenAI builder strips it before the request is sent.
        wire = OpenAIBackend._normalize_messages(msgs)
        wire_tool = next(m for m in wire if m["role"] == "tool")
        assert "is_error" not in wire_tool
        assert wire_tool["content"] == "Error: failed."


# ── clear ─────────────────────────────────────────────────────────────


class TestClear:
    """Tests for MessageHistory.clear."""

    def test_clear_preserves_system_prompt(self):
        conv = MessageHistory(system_prompt="You are helpful.")
        conv.add_user_message("hello")
        conv.add_assistant_message("hi")

        conv.clear()
        assert len(conv.messages) == 1
        assert conv.messages[0]["role"] == "system"
        assert conv.messages[0]["content"] == "You are helpful."

    def test_clear_without_system_prompt(self):
        conv = MessageHistory()
        conv.add_user_message("hello")
        conv.add_assistant_message("hi")

        conv.clear()
        assert conv.messages == []

    def test_clear_multiple_cycles(self):
        """Clear multiple times, still preserves system prompt."""
        conv = MessageHistory(system_prompt="S")
        conv.add_user_message("a")
        conv.clear()
        conv.add_user_message("b")
        conv.clear()
        assert len(conv.messages) == 1
        assert conv.messages[0]["content"] == "S"


# ── _ensure_turn_consistent (orphan repair + role closing) ─────────────


class TestRepairOrphanedToolCalls:
    """Tests for MessageHistory._ensure_turn_consistent (repair + role closing).

    `add_user_message` no longer repairs — consistency is enforced at the
    single save point (`save_to_memory`) and on TUI restore, so these tests
    exercise `_ensure_turn_consistent` directly.
    """

    def test_no_orphans_when_complete(self):
        """No repair needed when tool calls have matching results."""
        conv = MessageHistory()
        conv.add_user_message("search")
        conv.add_assistant_message(
            None,
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "search", "arguments": "{}"}}]
        )
        conv.add_tool_result("c1", "results")
        # Ensure still inserts a closing assistant after the trailing tool
        # result so roles keep alternating (a tool result is a user on the wire).
        conv._ensure_turn_consistent()
        # user, assistant(call), tool(result), assistant(closing)
        assert len(conv.messages) == 4
        assert conv.messages[-1]["role"] == "assistant"
        # No synthetic tool error injected
        assert not any(
            m["role"] == "tool" and "interrupted" in str(m.get("content", "")).lower()
            for m in conv.messages
        )

    def test_repairs_single_orphan(self):
        """A synthetic error result is added for an orphaned tool call."""
        conv = MessageHistory()
        conv.add_user_message("search")
        conv.add_assistant_message(
            None,
            tool_calls=[{"id": "orphan1", "type": "function", "function": {"name": "search", "arguments": "{}"}}]
        )
        # No tool result added — orphaned tool call
        conv._ensure_turn_consistent()

        # user, assistant(orphan), synthetic tool, assistant(closing)
        assert len(conv.messages) == 4
        tool_msgs = [m for m in conv.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "orphan1"
        assert "interrupted" in tool_msgs[0]["content"].lower()
        # An interrupted execution is an error — restore must not render
        # it as a successful "done".
        assert tool_msgs[0]["is_error"] is True
        # closing assistant keeps the wire alternating
        assert conv.messages[-1]["role"] == "assistant"

    def test_repairs_multiple_orphans(self):
        """Multiple orphaned tool calls each get a synthetic error."""
        conv = MessageHistory()
        conv.add_user_message("search")
        conv.add_assistant_message(
            None,
            tool_calls=[
                {"id": "o1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                {"id": "o2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
            ]
        )
        # No tool results for either — both orphaned
        conv._ensure_turn_consistent()

        orphans = [m for m in conv.messages if m["role"] == "tool"]
        assert len(orphans) == 2
        ids = {m["tool_call_id"] for m in orphans}
        assert ids == {"o1", "o2"}

    def test_partial_orphans(self):
        """Only missing tool results get repaired."""
        conv = MessageHistory()
        conv.add_user_message("search")
        conv.add_assistant_message(
            None,
            tool_calls=[
                {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
            ]
        )
        conv.add_tool_result("c1", "result for c1")
        # c2 is orphaned
        conv._ensure_turn_consistent()

        tool_msgs = [m for m in conv.messages if m["role"] == "tool"]
        # Should have c1's real result plus c2's synthetic error
        assert len(tool_msgs) == 2
        real = [m for m in tool_msgs if "result for c1" in str(m.get("content", ""))]
        synthetic = [m for m in tool_msgs if "interrupted" in str(m.get("content", "")).lower()]
        assert len(real) == 1
        assert len(synthetic) == 1

    def test_multiple_assistant_messages_with_orphans(self):
        """Walk backwards through multiple orphan scenarios."""
        conv = MessageHistory()
        conv.add_user_message("q1")
        conv.add_assistant_message(
            None,
            tool_calls=[{"id": "a1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]
        )
        # Orphan a1 (add_user_message no longer auto-repairs)
        conv.add_user_message("q2")
        conv.add_assistant_message(
            None,
            tool_calls=[{"id": "a2", "type": "function", "function": {"name": "y", "arguments": "{}"}}]
        )
        # Orphan a2
        conv.add_user_message("q3")
        conv._ensure_turn_consistent()

        synthetic = [m for m in conv.messages if m["role"] == "tool"]
        assert len(synthetic) == 2
        assert {m["tool_call_id"] for m in synthetic} == {"a1", "a2"}


# ── append_trim_marker (runtime-only trim note) ───────────────────────


class TestTrimMarker:
    """append_trim_marker appends a runtime-only note to the last assistant
    message — never persisted, never a separate message."""

    def test_appends_to_last_assistant(self):
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")
        conv.add_assistant_message("reply")
        conv.append_trim_marker(3)
        assert conv.messages[-1]["content"] == (
            "reply [INFO: 3 oldest turns have been removed from context]"
        )

    def test_empty_content_becomes_marker(self):
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")
        conv.add_assistant_message("")
        conv.append_trim_marker(2)
        assert conv.messages[-1]["content"] == (
            "[INFO: 2 oldest turns have been removed from context]"
        )

    def test_walks_back_to_last_assistant(self):
        """A trailing tool result does not block the note — it lands on the
        assistant message that owns the turn."""
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")
        conv.add_assistant_message(
            None,
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
        )
        conv.add_tool_result("c1", "ok")
        conv.append_trim_marker(1)
        assert conv.messages[-2]["content"] == (
            "[INFO: 1 oldest turns have been removed from context]"
        )

    def test_noop_without_assistant(self):
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")
        conv.append_trim_marker(1)
        # No assistant present → nothing appended, no crash.
        assert conv.messages[-1]["role"] == "user"


class TestStripTrimMarkers:
    """strip_trim_markers keeps the runtime trim note out of the diary."""

    def test_strips_marker_from_assistant_content(self):
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")
        conv.add_assistant_message("reply [INFO: 3 oldest turns have been removed from context]")
        cleaned = MessageHistory.strip_trim_markers(conv.messages)
        # The note is removed from the returned copy...
        assert cleaned[-1]["content"] == "reply"
        # ...and the live history keeps it.
        assert conv.messages[-1]["content"] == (
            "reply [INFO: 3 oldest turns have been removed from context]"
        )

    def test_marker_only_message_becomes_empty(self):
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")
        conv.add_assistant_message("[INFO: 2 oldest turns have been removed from context]")
        cleaned = MessageHistory.strip_trim_markers(conv.messages)
        assert cleaned[-1]["content"] == ""

    def test_non_assistant_untouched(self):
        conv = MessageHistory(system_prompt="SYS")
        # User content is never touched — the turn footnote shares the
        # [INFO: ] envelope but lives on user messages.
        conv.add_user_message("hi [INFO: 1 oldest turns have been removed from context]")
        conv.add_assistant_message("reply")
        cleaned = MessageHistory.strip_trim_markers(conv.messages)
        assert cleaned[1]["content"] == "hi [INFO: 1 oldest turns have been removed from context]"
        assert cleaned[-1]["content"] == "reply"


class TestRuntimeTurnIds:
    """``_turn_id`` maps an in-context turn back to its diary row.  It is
    runtime-only: stripped before the turn is persisted and popped before
    the wire."""

    def test_strip_turn_ids_removes_only_that_key(self):
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")
        conv.messages[-1]["_turn_id"] = 7
        conv.add_assistant_message("reply")
        conv.messages[-1]["thinking"] = "hmm"

        cleaned = MessageHistory.strip_turn_ids(conv.messages)

        assert "_turn_id" not in cleaned[1]
        assert cleaned[1]["content"] == "hi"
        # Every other key survives — the assistant message is untouched.
        assert cleaned[2]["thinking"] == "hmm"
        # The live history keeps its id (the trim reads it later).
        assert conv.messages[1]["_turn_id"] == 7

    def test_to_openai_messages_drops_turn_id(self):
        """The OpenAI backend copies message dicts verbatim (it only pops
        ``is_error`` by hand), so the funnel must drop the id or it rides
        into the request body as an unknown field."""
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")
        conv.messages[-1]["_turn_id"] = 7
        conv.add_assistant_message("reply")

        wire = conv.to_openai_messages()

        assert all("_turn_id" not in m for m in wire)
        assert wire[0]["content"] == "SYS"
        assert wire[1]["content"] == "hi"

    def test_extract_turns_surfaces_the_turn_id(self):
        """The trim learns which turns it evicted from the turn dict."""
        conv = MessageHistory(system_prompt="SYS")
        for i in (11, 22):
            conv.add_user_message(f"第{i}轮：一段比较长的用户输入内容，用来撑大估算。" * 3)
            conv.messages[-1]["_turn_id"] = i
            conv.add_assistant_message("回复" * 40)

        turns = MessageHistory.extract_turns(conv.messages[1:])  # skip system

        assert [t["turn_id"] for t in turns] == [11, 22]
        # A turn with no stamp reports None rather than a bogus id.
        assert MessageHistory.extract_turns([{"role": "user", "content": "x"}])[0]["turn_id"] is None


# ── add_assistant_message with thinking ───────────────────────────────


class TestAddAssistantThinking:
    """Tests for thinking field in assistant messages."""

    def test_thinking_stored_in_message(self):
        conv = MessageHistory()
        conv.add_assistant_message("answer", thinking="Let me think...")
        assert conv.messages[0]["thinking"] == "Let me think..."
        assert conv.messages[0]["content"] == "answer"

    def test_thinking_renamed_for_api(self):
        """Thinking field is renamed to reasoning_content in to_openai_messages."""
        conv = MessageHistory()
        conv.add_assistant_message("answer", thinking="internal reasoning")
        msgs = conv.to_openai_messages()
        assert "thinking" not in msgs[0]
        assert msgs[0]["reasoning_content"] == "internal reasoning"

    def test_images_stripped_for_api(self):
        """Images field is stripped in to_openai_messages."""
        conv = MessageHistory(system_prompt="test")
        # Manually add an images field to check stripping
        conv.messages[0]["images"] = ["/tmp/img.png"]
        msgs = conv.to_openai_messages()
        assert "images" not in msgs[0]


class TestThinkingEnabledRoundtrip:
    """Tests for reasoning_content roundtrip when thinking_enabled=True."""

    def test_empty_reasoning_for_messages_without_thinking(self):
        """Assistant msgs without thinking get reasoning_content="" when thinking on."""
        conv = MessageHistory()
        conv.add_assistant_message("answer")
        msgs = conv.to_openai_messages(thinking_enabled=True)
        assert msgs[0]["reasoning_content"] == ""

    def test_thinking_still_renamed_when_present(self):
        """Messages with thinking still get the real reasoning_content."""
        conv = MessageHistory()
        conv.add_assistant_message("answer", thinking="real reasoning")
        msgs = conv.to_openai_messages(thinking_enabled=True)
        assert msgs[0]["reasoning_content"] == "real reasoning"

    def test_disabled_mode_no_empty_reasoning(self):
        """When thinking_enabled=False, messages without thinking get no field."""
        conv = MessageHistory()
        conv.add_assistant_message("answer")
        msgs = conv.to_openai_messages(thinking_enabled=False)
        assert "reasoning_content" not in msgs[0]

    def test_synthetic_trim_context_gets_empty_reasoning(self):
        """_trim_context harness messages get empty reasoning_content."""
        conv = MessageHistory(system_prompt="test")
        conv.add_user_message("hello")
        conv.add_assistant_message("reply")
        conv.messages.insert(1, {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "_trim_abc", "type": "function",
                            "function": {"name": "_trim_context", "arguments": "{}"}}],
        })
        msgs = conv.to_openai_messages(thinking_enabled=True)
        trim_msg = msgs[1]
        assert trim_msg["reasoning_content"] == ""

    def test_synthetic_turn_prompt_gets_empty_reasoning(self):
        """_turn_prompt harness messages get empty reasoning_content."""
        conv = MessageHistory(system_prompt="test")
        conv.add_user_message("hello")
        conv.messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "_ctx_abc12345", "type": "function",
                            "function": {"name": "_turn_prompt", "arguments": "{}"}}],
        })
        msgs = conv.to_openai_messages(thinking_enabled=True)
        ctx_msg = msgs[-1]
        assert ctx_msg["reasoning_content"] == ""

    def test_user_and_system_roles_unaffected(self):
        """Only assistant messages get reasoning_content; user/system are untouched."""
        conv = MessageHistory(system_prompt="Be helpful.")
        conv.add_user_message("hi")
        conv.add_assistant_message("hey", thinking="thinking...")
        msgs = conv.to_openai_messages(thinking_enabled=True)
        assert "reasoning_content" not in msgs[0]  # system
        assert "reasoning_content" not in msgs[1]  # user
        assert msgs[2]["reasoning_content"] == "thinking..."  # assistant


# ── count_tokens ─────────────────────────────────────────────────────


class TestCountTokens:
    """Tests for MessageHistory.count_tokens()."""

    def test_empty_returns_at_least_one(self):
        conv = MessageHistory()
        assert conv.count_tokens() >= 1

    def test_increases_with_content(self):
        conv = MessageHistory()
        conv.add_user_message("hello world " * 50)
        count = conv.count_tokens()
        assert count > 10

    def test_tool_calls_add_tokens(self):
        conv = MessageHistory()
        conv.add_assistant_message(
            None,
            tool_calls=[{
                "id": "c1",
                "type": "function",
                "function": {"name": "search", "arguments": '{"query": "hello" * 100}'}
            }]
        )
        count = conv.count_tokens()
        assert count > 5  # tool call arguments contribute


class TestTokenEstimate:
    """The estimator is tiktoken's BPE, so assertions are on *properties* the
    trim depends on, not on exact counts — those move with the encoding and
    pinning them would just make every tiktoken bump a test failure.

    The property that matters is the A11 regression: CJK must never be
    undercounted, or the trim's stop condition lets the window sit genuinely
    over the ceiling."""

    def test_empty_is_zero(self):
        from slife.agent.message_history import estimate_text_tokens
        assert estimate_text_tokens("") == 0

    def test_cjk_costs_more_per_char_than_latin(self):
        from slife.agent.message_history import estimate_text_tokens
        cjk = "这是一段比较长的中文用户输入内容，用来测试分词器估算。"
        latin = "the quick brown fox jumps over the lazy dog"
        cjk_per_char = estimate_text_tokens(cjk) / len(cjk)
        latin_per_char = estimate_text_tokens(latin) / len(latin)
        # CJK is information-dense, so it spends more tokens per character.
        # The old chars//3 heuristic inverted this and undercounted Chinese.
        assert cjk_per_char > latin_per_char

    def test_wide_chars_never_undercounted(self):
        from slife.agent.message_history import estimate_text_tokens
        # A Han char is at least a whole token — the old blanket estimate
        # reported ~0.33 and undercounted Chinese-heavy sessions ~2-3x.
        assert estimate_text_tokens("汉" * 100) >= 100
        assert estimate_text_tokens("漢字全角ＡＢＣ") >= 7

    def test_mixed_text_costs_at_least_each_part(self):
        from slife.agent.message_history import estimate_text_tokens
        mixed = estimate_text_tokens("abcdef" + "汉" * 6)
        assert mixed >= estimate_text_tokens("汉" * 6)
        assert mixed >= 6

    def test_count_tokens_cjk_not_undercounted(self):
        conv = MessageHistory()
        conv.add_user_message("汉" * 90)
        conv.add_assistant_message("中" * 90)
        # The old chars//3 heuristic would report ~60 for 180 Han chars.
        assert conv.count_tokens() >= 180

    def test_tokens_freed_uses_same_estimator(self):
        from slife.agent.message_history import estimate_text_tokens
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("汉" * 90)
        conv.add_assistant_message("中" * 90)
        conv.add_user_message("keep")
        conv.add_assistant_message("this")
        _, freed = conv.extract_oldest_turns(target=estimate_text_tokens("keep"))
        # Removing the 180-char CJK turn freed ~180, matching count_tokens.
        assert freed >= 180


