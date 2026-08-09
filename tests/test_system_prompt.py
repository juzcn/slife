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
        assert result.startswith("Slife 环境信息")

    def test_has_required_sections(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "1. 环境" in result
        assert "2. 会话历史" in result
        assert "3. 永久记忆" in result
        assert "4. 图像与多模态" in result
        assert "5. 凭证解析链" in result
        assert "6. 工具与技能" in result

    def test_agent_name_displayed(self, cfg):
        from slife.agent.system_prompt import build
        cfg.a2a_config.agent_name = "MyBot"
        result = build(cfg)
        assert "Agent: MyBot" in result

    def test_falls_back_to_agent_id(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "Agent: testbot" in result

    def test_context_window_strategy(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "20% 至 80%" in result  # defaults
        assert "_sys_trim" in result
        assert "memory_search" in result

    def test_vision_disabled(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "视觉支持: 未启用" in result

    def test_vision_enabled(self, cfg):
        from slife.agent.system_prompt import build
        cfg.active_model.supports_vision = True
        result = build(cfg)
        assert "视觉支持: 已启用" in result

    def test_credstore_chain(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "os.environ" in result
        assert "凭证后端" in result

    def test_skills_dir_in_prompt(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "技能目录:" in result
        assert "skill_use" in result

    def test_data_dirs_in_prompt(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "数据根目录:" in result
        assert "配置文件:" in result
        assert "日志目录:" in result
        assert "数据库:" in result
        assert "技能目录:" in result
        assert "图片缓存:" in result

    def test_mcp_tool_prefix(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "server_name__" in result

    def test_no_personality_language(self, cfg):
        """No 'you are', 'helpful assistant', or tone instructions."""
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "you are" not in result.lower()
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

    def test_subagent_section_always_visible(self, cfg):
        """Subagent section always present — main/subagents both need to know."""
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "子代理特性" in result
        assert "不连接记忆服务器" in result

    def test_a2a_section_when_configured(self, cfg):
        """A2A section visible when a2a is configured."""
        from slife.agent.system_prompt import build
        cfg.a2a_config.enabled = True
        cfg.a2a_config.transport = "mqtt"
        cfg.a2a_config.broker_host = "mqtt.example.com"
        cfg.a2a_config.broker_port = 1883
        result = build(cfg)
        assert "8. 数据目录" in result
        assert "9. 多代理通信 (A2A)" in result
        assert "mqtt.example.com:1883" in result

    def test_a2a_section_hidden_when_disabled(self, cfg):
        """A2A section hidden when a2a is not enabled."""
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "多代理通信" not in result

    def test_environment_facts_present(self, cfg):
        """Platform facts are rendered, not left as template variables."""
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "{{" not in result  # no unrendered Jinja2
        assert "Agent:" in result
        assert "模型:" in result
        # 工作目录 / 当前时间 are now in the dynamic context_status.j2,
        # not the static system prompt.

    def test_hostname_in_prompt(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "主机:" in result
        assert "{{ hostname }}" not in result

    def test_arch_in_prompt(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "AMD64" in result or "x86_64" in result or "ARM64" in result.upper()

    def test_package_manager_uv(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "包管理工具: uv" in result

    def test_system_info_format(self, cfg):
        from slife.agent.system_prompt import build
        result = build(cfg)
        assert "系统信息:" in result


# ── Helper functions ────────────────────────────────────────────────────

class TestHelpers:
    """Direct tests for system_prompt helper functions."""

    def test_credstore_backend_runtime_error(self):
        """_credstore_backend returns '未知' when get_backend_name raises."""
        from slife.agent.system_prompt import _credstore_backend
        with patch("credstore.get_backend_name", side_effect=RuntimeError("boom")):
            assert _credstore_backend() == "未知"

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
        assert "▸ 最近 peer 上线/下线" in result
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
        assert "peer 上线/下线" not in result
        result = build_context_status(presence_events=[])
        assert "peer 上线/下线" not in result

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
