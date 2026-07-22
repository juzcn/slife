"""Tests for Slife.agent.system_prompt."""

from slife.agent.system_prompt import build


class TestBuild:
    def test_starts_with_slife(self):
        assert build().startswith("You are Slife — agent slife")

    def test_starts_with_custom_agent_id(self):
        """When only agent_id is given, the alias part is omitted."""
        result = build(agent_id="mybot")
        assert result.startswith("You are Slife — agent mybot")

    def test_with_agent_name_includes_alias(self):
        """When agent_name is given, it appears as an aka alias."""
        result = build(agent_id="mybot", agent_name="My Bot")
        assert result.startswith("You are Slife — agent mybot")
        assert ', aka "My Bot"' in result

    def test_agent_name_not_included_when_empty(self):
        """When agent_name is empty, no aka clause appears."""
        result = build(agent_id="mybot")
        assert "aka" not in result

    def test_config_reference(self):
        """Prompt mentions config location and security rules —
        tool usage is covered by individual tool schemas."""
        result = build()
        assert "slife.json5" in result
        assert "interactive-only" in result
        assert "credstore" in result
        assert "Never ask for or accept secrets in chat" in result

    def test_mcp_not_hardcoded(self):
        """MCP servers should NOT be listed in the system prompt —
        the LLM discovers them at runtime via mcp__mcp_list_servers
        and mcp__mcp_list_tools.  Hardcoding server names in the
        prompt would mislead the LLM into calling server names as
        tool names (e.g. duckduckgo-search) instead of using the
        namespaced forms (e.g. duckduckgo-search__search)."""
        result = build()
        assert "duckduckgo-search" not in result
        assert "filesystem, fetch" not in result
        assert "anyapi-mcp-server" not in result

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
