"""Tests for agent conversation history (slife.agent.conversation)."""

import pytest

from slife.agent.conversation import Conversation


class TestConversationInit:
    """Tests for Conversation.__init__()."""

    def test_empty_conversation(self):
        """Conversation starts empty without system prompt."""
        conv = Conversation()
        assert conv.messages == []

    def test_with_system_prompt(self):
        """System prompt becomes the first message."""
        conv = Conversation(system_prompt="You are a helpful assistant.")
        assert len(conv.messages) == 1
        assert conv.messages[0]["role"] == "system"
        assert conv.messages[0]["content"] == "You are a helpful assistant."

    def test_with_none_system_prompt(self):
        """None system prompt creates empty messages list."""
        conv = Conversation(system_prompt=None)
        assert conv.messages == []

    def test_with_empty_string_system_prompt(self):
        """Empty string system prompt is added (truthy check — empty string is falsy)."""
        conv = Conversation(system_prompt="")
        assert conv.messages == []  # empty string is falsy


class TestAddUserMessage:
    """Tests for Conversation.add_user_message()."""

    def test_plain_text(self):
        """Plain text user message."""
        conv = Conversation()
        conv.add_user_message("Hello, world!")
        assert len(conv.messages) == 1
        assert conv.messages[0]["role"] == "user"
        assert conv.messages[0]["content"] == "Hello, world!"

    def test_with_images(self, temp_image_file):
        """User message with image attachments creates multimodal content."""
        conv = Conversation()
        conv.add_user_message("Describe this image", images=[str(temp_image_file)])
        assert len(conv.messages) == 1
        msg = conv.messages[0]
        assert msg["role"] == "user"
        assert isinstance(msg["content"], list)
        # First part should be text
        assert msg["content"][0]["type"] == "text"
        assert "Describe this image" in msg["content"][0]["text"]
        # Second part should be image_url
        assert msg["content"][1]["type"] == "image_url"

    def test_with_multiple_images(self, temp_image_file):
        """Multiple images create multiple image_url blocks."""
        conv = Conversation()
        conv.add_user_message("Compare these", images=[str(temp_image_file), str(temp_image_file)])
        assert len(conv.messages) == 1
        content = conv.messages[0]["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image_url"
        assert content[2]["type"] == "image_url"

    def test_empty_images_list(self):
        """Empty images list treated as no images."""
        conv = Conversation()
        conv.add_user_message("Hello", images=[])
        assert len(conv.messages) == 1
        assert conv.messages[0]["content"] == "Hello"

    def test_none_images(self):
        """None images treated as no images."""
        conv = Conversation()
        conv.add_user_message("Hello", images=None)
        assert len(conv.messages) == 1
        assert conv.messages[0]["content"] == "Hello"

    def test_message_appended_to_end(self):
        """New messages are appended, not prepended."""
        conv = Conversation(system_prompt="System")
        conv.add_user_message("First")
        conv.add_user_message("Second")
        # system + user1 + user2 = 3 messages
        assert len(conv.messages) == 3
        assert conv.messages[1]["content"] == "First"
        assert conv.messages[2]["content"] == "Second"


class TestAddAssistantMessage:
    """Tests for Conversation.add_assistant_message()."""

    def test_plain_text(self):
        """Assistant message with just text."""
        conv = Conversation()
        conv.add_assistant_message("Here is the answer.")
        msg = conv.messages[0]
        assert msg["role"] == "assistant"
        assert msg["content"] == "Here is the answer."
        assert "tool_calls" not in msg

    def test_with_tool_calls(self):
        """Assistant message with tool calls."""
        conv = Conversation()
        tool_calls = [
            {
                "id": "call_123",
                "type": "function",
                "function": {"name": "test_tool", "arguments": '{"key":"val"}'},
            }
        ]
        conv.add_assistant_message("Using tools...", tool_calls=tool_calls)
        msg = conv.messages[0]
        assert msg["role"] == "assistant"
        assert msg["content"] == "Using tools..."
        assert msg["tool_calls"] == tool_calls

    def test_none_content(self):
        """None content becomes empty string."""
        conv = Conversation()
        conv.add_assistant_message(None)
        assert conv.messages[0]["content"] == ""

    def test_none_tool_calls(self):
        """None tool_calls are not added to message."""
        conv = Conversation()
        conv.add_assistant_message("text", tool_calls=None)
        assert "tool_calls" not in conv.messages[0]


class TestAddToolResult:
    """Tests for Conversation.add_tool_result()."""

    def test_tool_result(self):
        """Tool result message has correct format."""
        conv = Conversation()
        conv.add_tool_result("call_abc", "Tool output here")
        msg = conv.messages[0]
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "call_abc"
        assert msg["content"] == "Tool output here"

    def test_multiple_tool_results(self):
        """Multiple tool results are added correctly."""
        conv = Conversation()
        conv.add_tool_result("id1", "result1")
        conv.add_tool_result("id2", "result2")
        assert len(conv.messages) == 2
        assert conv.messages[0]["tool_call_id"] == "id1"
        assert conv.messages[1]["tool_call_id"] == "id2"


class TestToOpenaiMessages:
    """Tests for Conversation.to_openai_messages()."""

    def test_returns_copy(self):
        """Returns a copy, not the internal list reference."""
        conv = Conversation(system_prompt="System")
        msgs = conv.to_openai_messages()
        msgs.append({"role": "user", "content": "extra"})
        assert len(conv.messages) == 1  # internal list unchanged

    def test_full_conversation_sequence(self):
        """Full conversation: system, user, assistant, tool, assistant."""
        conv = Conversation(system_prompt="You are helpful.")
        conv.add_user_message("Search for cats")
        conv.add_assistant_message(None, tool_calls=[{
            "id": "c1", "type": "function",
            "function": {"name": "search", "arguments": '{"q":"cats"}'}
        }])
        conv.add_tool_result("c1", "Found 10 cats")
        conv.add_assistant_message("I found 10 cats!")

        msgs = conv.to_openai_messages()
        assert len(msgs) == 5
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert msgs[2]["role"] == "assistant"
        assert msgs[3]["role"] == "tool"
        assert msgs[4]["role"] == "assistant"


class TestClear:
    """Tests for Conversation.clear()."""

    def test_clear_preserves_system_prompt(self):
        """Clear keeps the system prompt message."""
        conv = Conversation(system_prompt="Keep me")
        conv.add_user_message("Hello")
        conv.add_assistant_message("Hi")
        assert len(conv.messages) == 3

        conv.clear()
        assert len(conv.messages) == 1
        assert conv.messages[0]["role"] == "system"
        assert conv.messages[0]["content"] == "Keep me"

    def test_clear_without_system_prompt(self):
        """Clear on conversation without system prompt yields empty."""
        conv = Conversation()
        conv.add_user_message("Hello")
        conv.add_assistant_message("Hi")
        assert len(conv.messages) == 2

        conv.clear()
        assert conv.messages == []

    def test_clear_when_already_empty(self):
        """Clear on empty conversation is safe."""
        conv = Conversation()
        conv.clear()
        assert conv.messages == []

    def test_clear_multiple_times(self):
        """Clear can be called multiple times."""
        conv = Conversation(system_prompt="System")
        conv.add_user_message("msg")
        conv.clear()
        conv.clear()
        assert len(conv.messages) == 1
