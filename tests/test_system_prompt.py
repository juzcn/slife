"""Tests for Slife.agent.system_prompt."""

import sys
from datetime import datetime
import pytest; pytestmark = pytest.mark.unit

from unittest.mock import patch

from slife.config import Config, ModelConfig


@pytest.fixture
def cfg():
    """Minimal config for prompt rendering."""
    return Config(
        models=[ModelConfig(
            ref="test/test-model",
            provider="test",
            api_model="test-model",
            display_name="Test Model",
            api_key="sk-test",
            context_window=131072,
            supports_vision=False,
        )],
        active_model_ref="test/test-model",
        tools=[],
        agent_id="testbot",
    )


class TestBuild:
    def test_starts_with_runtime_context(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert result.startswith("You are Agent")

    def test_has_required_sections(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "1. Environment" in result
        assert "2. Conversation history" in result
        assert "3. Persistent memory" in result
        assert "4. Images & multimodal" in result
        assert "5. Credential resolution chain" in result
        assert "6. Tools & skills" in result

    def test_agent_name_displayed(self, cfg):
        from slife.agent.system_prompt import build
        cfg.a2a_config.agent_name = "MyBot"
        result = build(cfg)
        assert "You are Agent MyBot" in result

    def test_falls_back_to_agent_id(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "You are Agent testbot" in result

    def test_context_window_strategy(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "Context floor: 20% / ceiling: 80%" in result  # defaults
        assert "_sys_trim" in result
        assert "memory_search" in result

    def test_vision_disabled(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "Vision support: disabled" in result

    def test_vision_enabled(self, cfg):
        from slife.agent.system_prompt import build
        cfg.active_model.supports_vision = True
        result = build(cfg)
        assert "Vision support: enabled" in result

    def test_credstore_chain(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "os.environ" in result
        assert "credential backend" in result

    def test_skills_dir_in_prompt(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "Skills:" in result
        assert "skill_use" in result

    def test_data_dirs_in_prompt(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "Data root:" in result
        assert "Config file:" in result
        assert "Logs:" in result
        assert "Database:" in result
        assert "Skills:" in result
        assert "Image cache:" in result

    def test_mcp_tool_prefix(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "server_name__" in result

    def test_no_personality_language(self, cfg):
        """No 'helpful assistant' or tone instructions.  (The opening 'You
        are Agent …' is intentional identity framing, not personality.)
        """
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "helpful assistant" not in result.lower()
        assert "Always reply" not in result

    def test_no_tool_descriptions(self, cfg):
        """System prompt describes mechanisms, not how to use tools."""
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "configuration" not in result.lower()
        assert "config ->" not in result.lower()
        assert "resolve shell" not in result.lower()
        assert "check if secret exists" not in result.lower()

    def test_no_slash_commands(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        # Check that no line is a slash command (leading / followed by word)
        for line in result.split("\n"):
            line = line.strip()
            assert not line.startswith("/skill ")
            assert not line.startswith("/config ")
            assert not line.startswith("/help ")
            assert not line.startswith("/clear ")

    def test_mcp_not_hardcoded(self, cfg):
        """No specific MCP server names — LLM discovers at runtime."""
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "duckduckgo-search" not in result
        assert "filesystem" not in result

    def test_subagent_delegation_section_present(self, cfg):
        """Section 7 tells the main agent how to delegate to local workers."""
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "Subagents (local worker delegation)" in result
        assert "spawn_subagent" in result
        assert "subagent_send_task" in result
        assert "subagent_send_task_async" in result
        assert "subagent_get_task_result" in result
        assert "subagent_list_tasks" in result
        # Subagents are workers, not A2A peers — mesh tools live in section 9.
        assert "A2A = one tool family, two transports" not in result
        assert "conversation is not saved" in result
        assert "cannot interact" in result
        assert "it only sends" in result

    def test_subagent_identity_appended_when_is_subagent(self, cfg, monkeypatch):
        """is_subagent=True appends the subagent identity template."""
        from slife.agent.system_prompt import build
        monkeypatch.setenv("SLIFE_SUBAGENT_NAME", "sub-7")
        monkeypatch.setenv("SLIFE_SUBAGENT_CREATED_AT", "2026-01-05T10:00:00+08:00")
        result = build(cfg, is_subagent=True)
        assert "By your spawn_subagent action, we enter into subagent mode" in result
        assert "You are NOW a subagent worker" in result
        assert "with the same capabilities" in result
        assert "may SEND messages" in result
        assert "all replies and management belong to" in result
        assert "conversation is not persisted" in result

    def test_subagent_identity_includes_name(self, cfg, monkeypatch):
        """SLIFE_SUBAGENT_NAME is rendered into the subagent identity."""
        from slife.agent.system_prompt import build
        monkeypatch.setenv("SLIFE_SUBAGENT_NAME", "sub-7")
        monkeypatch.setenv("SLIFE_SUBAGENT_CREATED_AT", "2026-01-05T10:00:00+08:00")
        result = build(cfg, is_subagent=True)
        assert "sub-7, created at 2026-01-05T10:00:00+08:00" in result

    def test_subagent_identity_forbids_persona(self, cfg, monkeypatch):
        """A subagent has no independent identity — it speaks as the parent
        agent, never introducing itself as a named persona to remote peers."""
        from slife.agent.system_prompt import build
        monkeypatch.setenv("SLIFE_SUBAGENT_NAME", "sub-7")
        monkeypatch.setenv("SLIFE_SUBAGENT_CREATED_AT", "2026-01-05T10:00:00+08:00")
        result = build(cfg, is_subagent=True)
        assert "NO independent identity" in result
        assert "no personality" in result
        assert "NEVER introduce yourself by name" in result

    def test_subagent_context_pure_by_default(self, cfg, monkeypatch):
        """Context defaults to clean (pure) when SLIFE_SUBAGENT_CONTEXT unset."""
        from slife.agent.system_prompt import build
        monkeypatch.setenv("SLIFE_SUBAGENT_NAME", "sub-7")
        monkeypatch.delenv("SLIFE_SUBAGENT_CONTEXT", raising=False)
        result = build(cfg, is_subagent=True)
        assert "Context: clean (pure)" in result
        assert "cloned from" not in result

    def test_subagent_context_cloned(self, cfg, monkeypatch):
        """SLIFE_SUBAGENT_CONTEXT=cloned renders the cloned-context identity."""
        from slife.agent.system_prompt import build
        monkeypatch.setenv("SLIFE_SUBAGENT_NAME", "sub-7")
        monkeypatch.setenv("SLIFE_SUBAGENT_CONTEXT", "cloned")
        result = build(cfg, is_subagent=True)
        assert "Context: cloned from" in result

    def test_a2a_section_when_configured(self, cfg):
        """A2A section visible when a2a is configured."""
        from slife.agent.system_prompt import build
        cfg.a2a_config.enabled = True
        cfg.a2a_config.transport = "mqtt"
        cfg.a2a_config.broker_host = "mqtt.example.com"
        cfg.a2a_config.broker_port = 1883
        result = build(cfg)
        assert "8. Data directories" in result
        assert "10. Multi-agent communication (A2A)" in result
        assert "mqtt.example.com:1883" in result

    def test_a2a_section_hidden_when_disabled(self, cfg):
        """A2A section hidden when a2a is not enabled."""
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "Multi-agent communication" not in result

    def test_environment_facts_present(self, cfg):
        """Platform facts are rendered, not left as template variables."""
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "{{" not in result  # no unrendered Jinja2
        assert "Agent testbot" in result  # agent name in the opening line
        assert "Platform type:" in result
        # Model / working directory / shell are reported by the dynamic
        # context_status.j2 (_sys_note), not duplicated in the static prompt.

    def test_memory_start_time_from_diary(self, cfg, tmp_path, monkeypatch):
        """Opening states when the agent's persisted memory began — the
        earliest turn in the SQLite diary."""
        import sqlite3

        from slife.agent.system_prompt import build

        db = tmp_path / "mem.db"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE diary (created_at TEXT)")
        con.execute(
            "INSERT INTO diary (created_at) VALUES ('2026-01-05T10:00:00+08:00')"
        )
        con.commit()
        con.close()
        monkeypatch.setattr("slife.paths.get_db_path", lambda agent_id: db)

        result = build(cfg)
        assert (
            "Your memory (conversation history) begins at 2026-01-05T10:00:00+08:00"
            in result
        )

    def test_no_memory_start_when_no_diary(self, cfg, tmp_path, monkeypatch):
        """Fresh agent with no diary → framed as first arrival, not a time."""
        from slife.agent.system_prompt import build

        monkeypatch.setattr(
            "slife.paths.get_db_path", lambda agent_id: tmp_path / "missing.db"
        )
        result = build(cfg)
        assert "begins at" not in result
        assert "This is your first time in this world." in result
        assert "You have no memory at all." in result

    def test_arch_in_prompt(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "AMD64" in result or "x86_64" in result or "ARM64" in result.upper()

    def test_package_manager_uv(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "Package manager: uv" in result

    def test_system_info_format(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "OS:" in result


# ── Helper functions ────────────────────────────────────────────────────

class TestHelpers:
    """Direct tests for system_prompt helper functions."""

    def test_credstore_backend_runtime_error(self):
        """_credstore_backend returns '未知' when get_backend_name raises."""
        from slife.agent.system_prompt import _credstore_backend
        with patch("credstore.get_backend_name", side_effect=RuntimeError("boom")):
            assert _credstore_backend() == "unknown"

    def test_os_name_windows(self):
        from slife.agent.system_prompt import _os_name
        with patch("platform.system", return_value="Windows"):
            assert _os_name() == "Windows"

    def test_os_name_linux(self):
        from slife.agent.system_prompt import _os_name
        with patch("platform.system", return_value="Linux"):
            assert _os_name() == "Linux"

    def test_os_name_macos(self):
        from slife.agent.system_prompt import _os_name
        with patch("platform.system", return_value="Darwin"):
            assert _os_name() == "macOS"

    def test_os_name_fallback(self):
        from slife.agent.system_prompt import _os_name
        with patch("platform.system", return_value="FreeBSD"):
            assert _os_name() == "FreeBSD"

    def test_current_shell_windows_powershell(self, monkeypatch):
        from slife.agent.system_prompt import _current_shell
        monkeypatch.setattr("os.name", "nt")
        monkeypatch.setenv("PSModulePath", r"C:\Modules")
        assert _current_shell() == "powershell"

    def test_current_shell_windows_cmd(self, monkeypatch):
        from slife.agent.system_prompt import _current_shell
        monkeypatch.setattr("os.name", "nt")
        monkeypatch.delenv("PSModulePath", raising=False)
        assert _current_shell() == "cmd"

    def test_current_shell_posix(self, monkeypatch):
        from slife.agent.system_prompt import _current_shell
        monkeypatch.setattr("os.name", "posix")
        monkeypatch.setenv("SHELL", "/bin/bash")
        assert _current_shell() == "/bin/bash"

    def test_current_shell_posix_default(self, monkeypatch):
        from slife.agent.system_prompt import _current_shell
        monkeypatch.setattr("os.name", "posix")
        monkeypatch.delenv("SHELL", raising=False)
        assert _current_shell() == "sh"

    def test_platform_type_headless_env(self, monkeypatch):
        from slife.agent.system_prompt import _platform_type
        monkeypatch.setenv("SLIFE_SUBAGENT_NAME", "worker-1")
        assert _platform_type() == "headless"

    def test_platform_type_headless_no_tty(self, monkeypatch):
        from slife.agent.system_prompt import _platform_type
        monkeypatch.delenv("SLIFE_SUBAGENT_NAME", raising=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        assert _platform_type() == "headless"

    def test_platform_type_native(self, monkeypatch):
        from slife.agent.system_prompt import _platform_type
        monkeypatch.delenv("SLIFE_SUBAGENT_NAME", raising=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        if sys.platform == "win32":
            assert _platform_type() == "native"

    def test_platform_type_wsl(self, monkeypatch):
        from slife.agent.system_prompt import _platform_type
        monkeypatch.delenv("SLIFE_SUBAGENT_NAME", raising=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(sys, "platform", "linux")
        with patch("os.path.exists", return_value=True):
            assert _platform_type() == "wsl"


# ── Context footer presence events ───────────────────────────────────────

class TestContextStatusPresence:
    """build_context_status renders pending A2A presence events."""

    def _events(self):
        return [
            (1723183402.0, "⚡ desk-02 (采采) online [idle]"),
            (1723183547.0, "✗ desk-03 offline"),
            (1723183561.0, "⏱ desk-04 timed out"),
        ]

    def test_renders_section_when_events_present(self):
        from slife.agent.system_prompt import build_context_status
        result = build_context_status(presence_events=self._events())
        assert "▸ Recent peer online/offline" in result
        assert "⚡ desk-02 (采采) online [idle]" in result
        assert "✗ desk-03 offline" in result
        assert "⏱ desk-04 timed out" in result

    def test_timestamp_matches_footer_time_format(self):
        """Event timestamps use the same %Y-%m-%d %H:%M:%S as current time."""
        from slife.agent.system_prompt import build_context_status
        epoch = 1723183402.0
        expected = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
        result = build_context_status(presence_events=[(epoch, "⚡ desk-02 online [idle]")])
        assert f"- {expected} ⚡ desk-02 online [idle]" in result

    def test_no_section_when_no_events(self):
        from slife.agent.system_prompt import build_context_status
        result = build_context_status(presence_events=None)
        assert "peer online/offline" not in result
        result = build_context_status(presence_events=[])
        assert "peer online/offline" not in result

    def test_no_subagent_name_by_default(self):
        """The context footer has no subagent line — subagent identity lives
        in subagent.j2 (the subagent's own system prompt), not here."""
        from slife.agent.system_prompt import build_context_status
        result = build_context_status()
        assert "Subagent:" not in result

    def test_multiple_events_kept_in_order(self):
        from slife.agent.system_prompt import build_context_status
        result = build_context_status(presence_events=self._events())
        online_idx = result.index("desk-02 (采采) online")
        offline_idx = result.index("desk-03 offline")
        timeout_idx = result.index("desk-04 timed out")
        assert online_idx < offline_idx < timeout_idx


class TestFormatPresenceLine:
    """format_presence_line renders TUI-identical text and filters noise."""

    def _card(self, **kw):
        from slife.a2a.card import AgentCard
        defaults = dict(agent_id="desk-02", display_name="", status="idle")
        defaults.update(kw)
        return AgentCard(**defaults)

    def test_online_with_display_name(self):
        from slife.a2a.card import format_presence_line
        card = self._card(agent_id="desk-02", display_name="采采", status="busy")
        assert format_presence_line(card, "online") == "⚡ 采采 (desk-02) online [busy]"

    def test_online_without_display_name(self):
        from slife.a2a.card import format_presence_line
        card = self._card(agent_id="desk-02")
        assert format_presence_line(card, "online") == "⚡ desk-02 online [idle]"

    def test_online_same_display_and_id_no_duplicate(self):
        from slife.a2a.card import format_presence_line
        card = self._card(agent_id="desk-02", display_name="desk-02")
        assert format_presence_line(card, "online") == "⚡ desk-02 online [idle]"

    def test_offline(self):
        from slife.a2a.card import format_presence_line
        assert format_presence_line(self._card(), "offline") == "✗ desk-02 offline"

    def test_timeout(self):
        from slife.a2a.card import format_presence_line
        assert format_presence_line(self._card(), "timeout") == "⏱ desk-02 timed out"

    def test_status_change_filtered(self):
        """Heartbeat-driven status_change is not a user-visible transition."""
        from slife.a2a.card import format_presence_line
        assert format_presence_line(self._card(), "status_change") is None
