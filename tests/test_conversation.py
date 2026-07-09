"""Tests for slife.agent.conversation — conversation history management."""

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
