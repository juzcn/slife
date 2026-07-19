"""Tests for Slife.agent.system_prompt."""

from slife.agent.system_prompt import build


class TestBuild:
    def test_starts_with_slife(self):
        assert build().startswith("You are Slife — agent slife")

    def test_config_reference(self):
        """Prompt mentions config location and security rules —
        tool usage is covered by individual tool schemas."""
        result = build()
        assert "slife.json5" in result
        assert "interactive-only" in result
        assert "credstore" in result
        assert "Never ask for or accept secrets in chat" in result

    def test_mcp_reference(self):
        """Prompt mentions pre-configured MCP servers and anyapi-mcp-server —
        this is startup configuration knowledge not found in any tool schema."""
        result = build()
        assert "anyapi-mcp-server" in result
        assert "filesystem, fetch, duckduckgo-search" in result

    def test_no_shell_leak(self):
        """Prompt should not leak shell-specific syntax."""
        result = build()
        assert "cmd.exe" not in result
        assert "bash" not in result

    def test_no_tool_descriptions(self):
        """System prompt should not describe what individual tools do —
        that belongs in tool schemas (schema over prompt)."""
        result = build()
        # Config tool one-liners removed — schemas cover purpose + params
        assert "resolve shell" not in result
        assert "set non-secret" not in result
        assert "remove from slife.json5 only" not in result
        assert "check if secret exists" not in result
        # System health — schema says "Call this at conversation start"
        assert "embedding failures" not in result
        # Platform — LLM common knowledge
        assert "before platform-specific shell" not in result
        # Memory modes — memory_search schema has 24-line description
        assert "grep / fts5 / hybrid / time" not in result
