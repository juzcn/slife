"""Tests for USER.md — the per-agent standing user preferences file.

Covers the plugin's ``__user_pref_edit`` data layer,
the system-prompt render (the ``**User Preferences**`` section appended by
both identity templates), and the native ``user_pref_edit`` tool that
delegates to the plugin and refreshes the session prompt on a write.
"""

import pytest; pytestmark = pytest.mark.unit

import json
from unittest.mock import AsyncMock

from slife.agent.system_prompt import USER_PREFS_MAX_CHARS
from slife.config import Config, ModelConfig


# ── memfiles internal read/write data layer ────────────────────────────


class TestUserPrefStoreInternal:
    """The plugin is USER.md's only host: it puts text back and nothing more.
    Nothing here parses or merges the file, which is the point."""

    @staticmethod
    def _dir(tmp_path, monkeypatch, existing: str = ""):
        import slife.plugins.memfiles.server as plugin

        memfiles = tmp_path / "agent.files"
        memfiles.mkdir()
        if existing:
            (memfiles / "USER.md").write_text(existing, encoding="utf-8")
        monkeypatch.setattr(plugin, "get_memfiles_dir", lambda: memfiles)
        return plugin, memfiles

    @pytest.mark.asyncio
    async def test_write_replaces_the_whole_file(self, tmp_path, monkeypatch):
        """The thing append could never do: make text that was there go away."""
        plugin, memfiles = self._dir(tmp_path, monkeypatch, "1. stale line\n")
        out = json.loads(
            await getattr(plugin, "__user_pref_edit")("1. corrected\n")
        )
        assert out["chars"] == len("1. corrected\n")
        assert (memfiles / "USER.md").read_text(encoding="utf-8") == "1. corrected\n"

    @pytest.mark.asyncio
    async def test_write_creates_a_missing_file(self, tmp_path, monkeypatch):
        plugin, memfiles = self._dir(tmp_path, monkeypatch)
        await getattr(plugin, "__user_pref_edit")("1. **A** — one\n")
        assert (memfiles / "USER.md").read_text(encoding="utf-8") == "1. **A** — one\n"


# ── System prompt render ───────────────────────────────────────────────


def _cfg(agent_name: str = "testbot") -> Config:
    return Config(
        models=[ModelConfig(
            ref="test/test-model", provider="test", api_model="test-model",
            display_name="Test Model", api_key="sk-test",
            context_window=131072, supports_vision=False,
        )],
        active_model_ref="test/test-model",
        tools=[],
        agent_name=agent_name,
    )


class TestUserPreferencesRender:
    def test_absent_file_still_states_the_section(self, monkeypatch):
        """The header and the line that names the tools are unconditional, so
        an agent that has never been given a preference still knows the
        preferences exist and which tools read and replace them.  Only the
        user's own bytes are conditional."""
        from slife.agent.system_prompt import build

        monkeypatch.setattr(
            "slife.paths.get_memfiles_dir",
            lambda agent_name: __import__("pathlib").Path("nope") / f"{agent_name}.files",
        )
        result = build(_cfg())
        assert result.endswith(
            "Below are the user's standing preferences, held across sessions — "
            "use `user_pref_edit` to edit them.\n"
            "**User Preferences**\n"
            "(Empty)"
        )

    def test_section_appended_with_title_stripped(self, tmp_path, monkeypatch):
        from slife.agent.system_prompt import build

        files = tmp_path / "testbot.files"
        files.mkdir()
        files.joinpath("USER.md").write_text(
            "# User Preferences\n\n1. **Search** — use Baidu for Chinese news.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("slife.paths.get_memfiles_dir", lambda agent_name: files)

        result = build(_cfg())
        assert result.endswith(
            "Below are the user's standing preferences, held across sessions — "
            "use `user_pref_edit` to edit them.\n"
            "**User Preferences**\n"
            "1. **Search** — use Baidu for Chinese news."
        )
        assert "# User Preferences" not in result
        assert "(Empty)" not in result

    def test_identical_section_both_roles(self, tmp_path, monkeypatch):
        from slife.agent.system_prompt import build

        files = tmp_path / "testbot.files"
        files.mkdir()
        files.joinpath("USER.md").write_text(
            "1. **Language** — reply in English.\n", encoding="utf-8"
        )
        monkeypatch.setattr("slife.paths.get_memfiles_dir", lambda agent_name: files)

        main = build(_cfg(), is_subagent=False)
        sub = build(_cfg(), is_subagent=True)
        assert main[main.index("**User Preferences**"):] == \
            sub[sub.index("**User Preferences**"):]


# ── Native user_pref_edit tool ────────────────────────────────────────


class TestUserPrefEditTool:
    def _tool(self, reply, refresh_calls):
        from slife.tools.context import ToolContext
        from slife.tools.user_prefs import UserPrefEditTool

        client = AsyncMock()
        client.call_tool.return_value = json.dumps(reply)
        tool = UserPrefEditTool()
        tool._ctx = ToolContext(
            memfiles_client=client,
            refresh_system_prompt=lambda: refresh_calls.append(1),
        )
        return tool

    @pytest.mark.asyncio
    async def test_delegates_and_refreshes_the_prompt(self):
        refresh_calls = []
        tool = self._tool({"path": "x/USER.md", "chars": 12}, refresh_calls)
        out = json.loads(await tool.execute(content="1. **A** — x\n"))
        assert out["chars"] == 12
        assert refresh_calls == [1]
        tool._ctx.memfiles_client.call_tool.assert_awaited_once_with(
            "__user_pref_edit", {"content": "1. **A** — x\n"}
        )

    @pytest.mark.asyncio
    async def test_empty_content_rejected_without_writing(self):
        """An omitted argument must not clear the file — the write is whole."""
        refresh_calls = []
        tool = self._tool({}, refresh_calls)
        out = await tool.execute(content="   ")
        assert "content is required" in out
        assert refresh_calls == []
        tool._ctx.memfiles_client.call_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_content_over_the_prompt_cap_is_refused_by_name(self):
        """The prompt would silently cut it; the write refuses and says the
        number instead."""
        refresh_calls = []
        tool = self._tool({}, refresh_calls)
        out = await tool.execute(content="x" * (USER_PREFS_MAX_CHARS + 1))
        assert out.startswith("Error:")
        assert str(USER_PREFS_MAX_CHARS) in out
        assert refresh_calls == []
        tool._ctx.memfiles_client.call_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_offline_client_reports_error(self):
        from slife.tools.context import ToolContext
        from slife.tools.user_prefs import UserPrefEditTool

        tool = UserPrefEditTool()
        tool._ctx = ToolContext(memfiles_client=None)
        assert "not connected" in await tool.execute(content="1. a\n")
