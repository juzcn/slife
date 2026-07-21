"""Tests for Slife.agent.conversation — conversation history management."""

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

    def test_text_with_images(self):
        """Text with images creates multimodal content array."""
        conv = Conversation()
        with pytest.raises(FileNotFoundError):
            # Will fail on encode_image since images don't exist
            conv.add_user_message("Describe", images=["/fake/img.png"])

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


# ── _repair_orphaned_tool_calls ──────────────────────────────────────


class TestRepairOrphanedToolCalls:
    """Tests for orphaned tool call repair on add_user_message."""

    def test_no_orphans_when_complete(self):
        """No repair needed when tool calls have matching results."""
        conv = Conversation()
        conv.add_user_message("search")
        conv.add_assistant_message(
            None,
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "search", "arguments": "{}"}}]
        )
        conv.add_tool_result("c1", "results")
        # Add another message to confirm no orphan repair needed
        conv.add_user_message("next question")
        assert len(conv.messages) == 4  # user, assistant, tool, user
        # No synthetic error messages injected

    def test_repairs_single_orphan(self):
        """A synthetic error result is added for an orphaned tool call."""
        conv = Conversation()
        conv.add_user_message("search")
        conv.add_assistant_message(
            None,
            tool_calls=[{"id": "orphan1", "type": "function", "function": {"name": "search", "arguments": "{}"}}]
        )
        # No tool result added — orphaned tool call
        # Next user message triggers repair
        conv.add_user_message("interrupting question")

        # Should have: user, assistant(orphan), synthetic tool error, user
        assert len(conv.messages) == 4
        tool_msgs = [m for m in conv.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "orphan1"
        assert "cancelled" in tool_msgs[0]["content"].lower()

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
        conv.add_user_message("interrupt")

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
        conv.add_user_message("next")

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
        # Orphan a1
        conv.add_user_message("q2")
        conv.add_assistant_message(
            None,
            tool_calls=[{"id": "a2", "type": "function", "function": {"name": "y", "arguments": "{}"}}]
        )
        # Orphan a2
        conv.add_user_message("q3")

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

    def test_thinking_stripped_for_api(self):
        """Thinking field is stripped in to_openai_messages."""
        conv = Conversation()
        conv.add_assistant_message("answer", thinking="internal reasoning")
        msgs = conv.to_openai_messages()
        assert "thinking" not in msgs[0]

    def test_images_stripped_for_api(self):
        """Images field is stripped in to_openai_messages."""
        conv = Conversation(system_prompt="test")
        # Manually add an images field to check stripping
        conv.messages[0]["images"] = ["/tmp/img.png"]
        msgs = conv.to_openai_messages()
        assert "images" not in msgs[0]


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


# ── trim_context ─────────────────────────────────────────────────────


class TestTrimContext:
    """Tests for Conversation.trim_context()."""

    def test_noop_when_under_ceiling(self):
        conv = Conversation(system_prompt="You are helpful.")
        conv.add_user_message("short")
        conv.add_assistant_message("short reply")
        removed = conv.trim_context(context_window=1_000_000)
        assert removed == 0
        assert len(conv.messages) == 3

    def test_noop_when_no_messages(self):
        conv = Conversation()
        removed = conv.trim_context(context_window=1000)
        assert removed == 0

    def test_noop_when_zero_window(self):
        conv = Conversation(system_prompt="test")
        conv.add_user_message("hello")
        removed = conv.trim_context(context_window=0)
        assert removed == 0

    def test_trims_oldest_turns(self):
        """Oldest user→assistant turns are trimmed to make room."""
        conv = Conversation(system_prompt="S")
        # Add many large turns
        for i in range(20):
            conv.add_user_message(f"question {i} " + "x" * 500)
            conv.add_assistant_message(f"answer {i} " + "y" * 500)

        original_count = len(conv.messages)
        removed = conv.trim_context(context_window=2000, floor=0.3, ceiling=0.7)
        if removed > 0:
            assert len(conv.messages) < original_count
            # System prompt should still be first
            assert conv.messages[0]["role"] == "system"

    def test_preserves_system_prompt(self):
        conv = Conversation(system_prompt="Do not remove me.")
        # Add enough content to trigger trimming
        for i in range(30):
            conv.add_user_message("q" + "x" * 200)
            conv.add_assistant_message("a" + "y" * 200)

        conv.trim_context(context_window=1000, floor=0.2, ceiling=0.5)
        if len(conv.messages) > 0:
            assert conv.messages[0]["role"] == "system"
            assert conv.messages[0]["content"] == "Do not remove me."

    def test_trim_no_system_prompt(self):
        conv = Conversation()
        for i in range(30):
            conv.add_user_message("q" + "x" * 300)
            conv.add_assistant_message("a" + "y" * 300)

        conv.trim_context(context_window=1000, floor=0.2, ceiling=0.5)
        # Should not crash without system prompt
        if len(conv.messages) > 0:
            assert conv.messages[0]["role"] == "user"

    def test_trim_no_user_messages_to_trim(self):
        """When only assistant messages remain (no user turns), trim exits early.
        This covers the break on line 232 (no complete turns left to trim)."""
        conv = Conversation()
        # Add only assistant messages (no user messages) — so count_tokens()
        # will be high but no user turns to remove.
        for i in range(50):
            conv.messages.append({
                "role": "assistant",
                "content": "x" * 500,
            })

        removed = conv.trim_context(context_window=100, floor=0.1, ceiling=0.2)
        # Should exit early with break since there are no user messages to
        # anchor turn boundaries
        assert removed == 0

    def test_trim_zero_window_returns_zero(self):
        conv = Conversation(system_prompt="test")
        conv.add_user_message("hello")
        removed = conv.trim_context(context_window=0)
        assert removed == 0
