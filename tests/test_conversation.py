"""Tests for Slife.agent.conversation — conversation history management."""

import pytest; pytestmark = pytest.mark.unit


import pytest

from slife.agent.conversation import Conversation


# ── Construction ─────────────────────────────────────────────────────


class TestConversationConstruction:
    """Tests for Conversation.__init__."""

    def test_empty_conversation(self):
        """Conversation starts with no messages when no system prompt."""
        conv = Conversation()
        assert conv.messages == []

    def test_with_system_prompt(self):
        """System prompt creates initial system message."""
        conv = Conversation(system_prompt="You are helpful.")
        assert len(conv.messages) == 1
        assert conv.messages[0]["role"] == "system"
        assert conv.messages[0]["content"] == "You are helpful."

    def test_from_history_seeds_messages(self):
        """from_history prepends a fresh system prompt and skips inherited system."""
        conv = Conversation.from_history(
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

    def test_none_system_prompt(self):
        """None system prompt results in empty list."""
        conv = Conversation(system_prompt=None)
        assert conv.messages == []


# ── add_user_message ─────────────────────────────────────────────────


class TestAddUserMessage:
    """Tests for Conversation.add_user_message."""

    def test_plain_text(self):
        """Plain text message without images."""
        conv = Conversation()
        conv.add_user_message("Hello!")
        assert len(conv.messages) == 1
        assert conv.messages[0]["role"] == "user"
        assert conv.messages[0]["content"] == "Hello!"

    def test_text_with_images_dropped_notifies_llm(self):
        """Unreadable images are dropped with a visible note for the LLM."""
        conv = Conversation()
        conv.add_user_message("Describe", images=["/fake/img.png"])
        assert conv.messages[0]["role"] == "user"
        parts = conv.messages[0]["content"]
        assert isinstance(parts, list)
        # Original text + dropped-image note = 2 parts
        assert len(parts) == 2
        assert parts[0] == {"type": "text", "text": "Describe"}
        assert "System note" in parts[1]["text"]
        assert "/fake/img.png" in parts[1]["text"]
        assert "NOT sent" in parts[1]["text"]

    def test_image_paths_not_provided(self):
        """images=None is treated as no images."""
        conv = Conversation()
        conv.add_user_message("hello", images=None)
        assert conv.messages[0]["role"] == "user"
        assert conv.messages[0]["content"] == "hello"

    def test_empty_images_list(self):
        """Empty images list treated as no images (falsy)."""
        conv = Conversation()
        conv.add_user_message("hello", images=[])
        assert conv.messages[0]["role"] == "user"
        assert conv.messages[0]["content"] == "hello"

    def test_sanitizes_api_keys(self):
        """User input with API key patterns is sanitized before storage."""
        conv = Conversation()
        conv.add_user_message("My key is sk-ant-api03-abc123def456ghi789jkl")
        assert "sk-ant-api03-abc123def456ghi789jkl" not in conv.messages[0]["content"]
        assert "<MASKED>" in conv.messages[0]["content"]

    def test_normal_input_passes_through(self):
        """Normal user input without secrets is unchanged."""
        conv = Conversation()
        conv.add_user_message("What is the weather today?")
        assert conv.messages[0]["content"] == "What is the weather today?"

    def test_input_sanitization_idempotent(self):
        """Double sanitization produces the same result."""
        conv = Conversation()
        conv.add_user_message("api_key=sk-test-key-xxxxyyyyzzzz11112222")
        first = conv.messages[0]["content"]
        # Reset and add already-sanitized content
        conv2 = Conversation()
        conv2.add_user_message(first)
        assert conv2.messages[0]["content"] == first


# ── add_assistant_message ────────────────────────────────────────────


class TestAddAssistantMessage:
    """Tests for Conversation.add_assistant_message."""

    def test_content_only(self):
        conv = Conversation()
        conv.add_assistant_message("I'm fine, thanks!")
        assert conv.messages[0]["role"] == "assistant"
        assert conv.messages[0]["content"] == "I'm fine, thanks!"
        assert "tool_calls" not in conv.messages[0]

    def test_content_none_replaced_with_empty_string(self):
        """None content is replaced with empty string."""
        conv = Conversation()
        conv.add_assistant_message(None)
        assert conv.messages[0]["content"] == ""

    def test_with_tool_calls(self):
        conv = Conversation()
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
        conv = Conversation()
        conv.add_assistant_message(None, tool_calls=[{"id": "x"}])
        assert conv.messages[0]["content"] == ""
        assert conv.messages[0]["tool_calls"] == [{"id": "x"}]


# ── add_tool_result ──────────────────────────────────────────────────


class TestAddToolResult:
    """Tests for Conversation.add_tool_result."""

    def test_adds_tool_result(self):
        conv = Conversation()
        conv.add_tool_result("call_abc", "Search results here.")
        assert conv.messages[0]["role"] == "tool"
        assert conv.messages[0]["tool_call_id"] == "call_abc"
        assert conv.messages[0]["content"] == "Search results here."


# ── to_openai_messages ───────────────────────────────────────────────


class TestToOpenAIMessages:
    """Tests for Conversation.to_openai_messages."""

    def test_returns_copy(self):
        """Returns a copy, not the internal list."""
        conv = Conversation(system_prompt="You are helpful.")
        msgs = conv.to_openai_messages()
        msgs.append({"role": "user", "content": "extra"})
        assert len(conv.messages) == 1  # Original unchanged

    def test_full_conversation_flow(self):
        """Complete conversation flow produces correct message order."""
        conv = Conversation(system_prompt="Be concise.")
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
        conv = Conversation()
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


# ── clear ─────────────────────────────────────────────────────────────


class TestClear:
    """Tests for Conversation.clear."""

    def test_clear_preserves_system_prompt(self):
        conv = Conversation(system_prompt="You are helpful.")
        conv.add_user_message("hello")
        conv.add_assistant_message("hi")

        conv.clear()
        assert len(conv.messages) == 1
        assert conv.messages[0]["role"] == "system"
        assert conv.messages[0]["content"] == "You are helpful."

    def test_clear_without_system_prompt(self):
        conv = Conversation()
        conv.add_user_message("hello")
        conv.add_assistant_message("hi")

        conv.clear()
        assert conv.messages == []

    def test_clear_multiple_cycles(self):
        """Clear multiple times, still preserves system prompt."""
        conv = Conversation(system_prompt="S")
        conv.add_user_message("a")
        conv.clear()
        conv.add_user_message("b")
        conv.clear()
        assert len(conv.messages) == 1
        assert conv.messages[0]["content"] == "S"


# ── _ensure_turn_consistent (orphan repair + role closing) ─────────────


class TestRepairOrphanedToolCalls:
    """Tests for Conversation._ensure_turn_consistent (repair + role closing).

    `add_user_message` no longer repairs — consistency is enforced at the
    single save point (`save_to_memory`) and on TUI restore, so these tests
    exercise `_ensure_turn_consistent` directly.
    """

    def test_no_orphans_when_complete(self):
        """No repair needed when tool calls have matching results."""
        conv = Conversation()
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
            m["role"] == "tool" and "cancelled" in str(m.get("content", "")).lower()
            for m in conv.messages
        )

    def test_repairs_single_orphan(self):
        """A synthetic error result is added for an orphaned tool call."""
        conv = Conversation()
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
        assert "cancelled" in tool_msgs[0]["content"].lower()
        # closing assistant keeps the wire alternating
        assert conv.messages[-1]["role"] == "assistant"

    def test_repairs_multiple_orphans(self):
        """Multiple orphaned tool calls each get a synthetic error."""
        conv = Conversation()
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
        conv = Conversation()
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
        synthetic = [m for m in tool_msgs if "cancelled" in str(m.get("content", "")).lower()]
        assert len(real) == 1
        assert len(synthetic) == 1

    def test_multiple_assistant_messages_with_orphans(self):
        """Walk backwards through multiple orphan scenarios."""
        conv = Conversation()
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


# ── add_assistant_message with thinking ───────────────────────────────


class TestAddAssistantThinking:
    """Tests for thinking field in assistant messages."""

    def test_thinking_stored_in_message(self):
        conv = Conversation()
        conv.add_assistant_message("answer", thinking="Let me think...")
        assert conv.messages[0]["thinking"] == "Let me think..."
        assert conv.messages[0]["content"] == "answer"

    def test_thinking_renamed_for_api(self):
        """Thinking field is renamed to reasoning_content in to_openai_messages."""
        conv = Conversation()
        conv.add_assistant_message("answer", thinking="internal reasoning")
        msgs = conv.to_openai_messages()
        assert "thinking" not in msgs[0]
        assert msgs[0]["reasoning_content"] == "internal reasoning"

    def test_images_stripped_for_api(self):
        """Images field is stripped in to_openai_messages."""
        conv = Conversation(system_prompt="test")
        # Manually add an images field to check stripping
        conv.messages[0]["images"] = ["/tmp/img.png"]
        msgs = conv.to_openai_messages()
        assert "images" not in msgs[0]


class TestThinkingEnabledRoundtrip:
    """Tests for reasoning_content roundtrip when thinking_enabled=True."""

    def test_empty_reasoning_for_messages_without_thinking(self):
        """Assistant msgs without thinking get reasoning_content="" when thinking on."""
        conv = Conversation()
        conv.add_assistant_message("answer")
        msgs = conv.to_openai_messages(thinking_enabled=True)
        assert msgs[0]["reasoning_content"] == ""

    def test_thinking_still_renamed_when_present(self):
        """Messages with thinking still get the real reasoning_content."""
        conv = Conversation()
        conv.add_assistant_message("answer", thinking="real reasoning")
        msgs = conv.to_openai_messages(thinking_enabled=True)
        assert msgs[0]["reasoning_content"] == "real reasoning"

    def test_disabled_mode_no_empty_reasoning(self):
        """When thinking_enabled=False, messages without thinking get no field."""
        conv = Conversation()
        conv.add_assistant_message("answer")
        msgs = conv.to_openai_messages(thinking_enabled=False)
        assert "reasoning_content" not in msgs[0]

    def test_synthetic_trim_context_gets_empty_reasoning(self):
        """_trim_context harness messages get empty reasoning_content."""
        conv = Conversation(system_prompt="test")
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

    def test_synthetic_context_status_gets_empty_reasoning(self):
        """_context_status harness messages get empty reasoning_content."""
        conv = Conversation(system_prompt="test")
        conv.add_user_message("hello")
        conv.messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "_ctx_abc12345", "type": "function",
                            "function": {"name": "_context_status", "arguments": "{}"}}],
        })
        msgs = conv.to_openai_messages(thinking_enabled=True)
        ctx_msg = msgs[-1]
        assert ctx_msg["reasoning_content"] == ""

    def test_user_and_system_roles_unaffected(self):
        """Only assistant messages get reasoning_content; user/system are untouched."""
        conv = Conversation(system_prompt="Be helpful.")
        conv.add_user_message("hi")
        conv.add_assistant_message("hey", thinking="thinking...")
        msgs = conv.to_openai_messages(thinking_enabled=True)
        assert "reasoning_content" not in msgs[0]  # system
        assert "reasoning_content" not in msgs[1]  # user
        assert msgs[2]["reasoning_content"] == "thinking..."  # assistant


# ── count_tokens ─────────────────────────────────────────────────────


class TestCountTokens:
    """Tests for Conversation.count_tokens()."""

    def test_empty_returns_at_least_one(self):
        conv = Conversation()
        assert conv.count_tokens() >= 1

    def test_increases_with_content(self):
        conv = Conversation()
        conv.add_user_message("hello world " * 50)
        count = conv.count_tokens()
        assert count > 10

    def test_tool_calls_add_tokens(self):
        conv = Conversation()
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

    def test_images_add_tokens(self):
        conv = Conversation(system_prompt="test")
        conv.messages[0]["images"] = ["/tmp/img1.png", "/tmp/img2.png"]
        count = conv.count_tokens()
        assert count > 200  # ~200 tokens per image


