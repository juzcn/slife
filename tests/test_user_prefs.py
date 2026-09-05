"""Tests for USER.md — the per-agent standing user preferences file.

Covers the pure read-merge-append logic (``memfiles.user_prefs``), the
internal ``__user_pref_append`` data-layer tool served by the memfiles
plugin, the system-prompt render (the ``**User Preferences**`` section
appended by both identity templates), and the native ``add_user_pref``
tool that delegates to the plugin and refreshes the session prompt.
"""

import pytest; pytestmark = pytest.mark.unit

import json
from unittest.mock import AsyncMock

from slife.config import Config, ModelConfig
from slife.plugins.memfiles import user_prefs as up
from slife.plugins.memfiles.user_prefs import append_preference


# ── Pure merge logic ────────────────────────────────────────────────────


class TestAppendPreference:
    def test_first_item_on_empty_file(self):
        text, info = append_preference("", "**A** — one")
        assert info["appended"] and not info["duplicate"]
        assert text == "1. **A** — one"
        assert info["item"] == "1. **A** — one"
        assert info["items"] == 1

    def test_continues_numbering(self):
        text, info = append_preference(
            "# User Preferences\n\n1. **A** — one\n", "**B** — two"
        )
        assert info["appended"] and info["item"] == "2. **B** — two"
        assert "# User Preferences" in text and "1. **A** — one" in text
        assert text.index("2. **B** — two") > text.index("1. **A** — one")

    def test_keeps_bullet_style(self):
        _, info = append_preference("- alpha\n", "beta")
        assert info["item"] == "- beta"

    def test_empty_preference_rejected(self):
        _, info = append_preference("1. a\n", "   ")
        assert info["error"] and not info["appended"]

    def test_duplicate_normalized_noop(self):
        current = "1. **Search** — for Chinese news use Baidu.\n"
        _, info = append_preference(current, "**SEARCH** — For Chinese news use Baidu")
        assert info["duplicate"] and not info["appended"]

    def test_substantial_containment_is_duplicate(self):
        _, info = append_preference(
            "1. Use Baidu for Chinese domestic news searches.\n",
            "use baidu for chinese domestic news searches when researching",
        )
        assert info["duplicate"]

    def test_structure_preserved_verbatim(self):
        original = "# User Preferences\n\n1. **A** — x\n\nTrailing note kept\n"
        text, info = append_preference(original, "**B** — y")
        assert info["appended"]
        assert text.startswith("# User Preferences")
        assert "Trailing note kept" in text
        # untouched bytes: the title line and the trailing note are unchanged
        assert "# User Preferences" in text
        assert text.index("2. **B** — y") < text.index("Trailing note kept")

    def test_prose_only_file_gets_blank_separator(self):
        text, _ = append_preference("# User Preferences\n\nhello\n", "**C** — z")
        assert "\n\n1. **C** — z" in text

    def test_blank_separator_not_duplicated(self):
        text, _ = append_preference("hello\n\n", "hi")
        assert text.count("\n\n") == 1 and text.endswith("1. hi")


# ── memfiles internal __user_pref_append data layer ────────────────────


class TestUserPrefAppendInternal:
    @pytest.mark.asyncio
    async def test_writes_new_file(self, tmp_path, monkeypatch):
        import slife.plugins.memfiles.server as plugin

        memfiles = tmp_path / "agent.files"
        memfiles.mkdir()
        monkeypatch.setattr(plugin, "get_memfiles_dir", lambda: memfiles)

        append = getattr(plugin, "__user_pref_append")
        out = json.loads(await append("**A** — one"))
        assert out["appended"] and out["items"] == 1
        assert (memfiles / "USER.md").read_text(encoding="utf-8") == "1. **A** — one"

    @pytest.mark.asyncio
    async def test_dedupe_and_append_existing(self, tmp_path, monkeypatch):
        import slife.plugins.memfiles.server as plugin

        memfiles = tmp_path / "agent.files"
        memfiles.mkdir()
        (memfiles / "USER.md").write_text(
            "1. **A** — one\n", encoding="utf-8"
        )
        monkeypatch.setattr(plugin, "get_memfiles_dir", lambda: memfiles)

        append = getattr(plugin, "__user_pref_append")
        dup = json.loads(await append("**A** — ONE"))
        assert dup["duplicate"] and not dup["appended"]
        live = json.loads(await append("**B** — two"))
        assert live["appended"] and live["items"] == 2
        assert (memfiles / "USER.md").read_text(
            encoding="utf-8"
        ) == "1. **A** — one\n2. **B** — two"


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
    def test_absent_file_renders_no_section(self, monkeypatch):
        from slife.agent.system_prompt import build

        monkeypatch.setattr(
            "slife.paths.get_memfiles_dir",
            lambda agent_name: __import__("pathlib").Path("nope") / f"{agent_name}.files",
        )
        result = build(_cfg())
        assert "**User Preferences**" not in result

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
            "**User Preferences**\n1. **Search** — use Baidu for Chinese news."
        )
        assert "# User Preferences" not in result

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


# ── Native add_user_pref tool ──────────────────────────────────────────


class TestAddUserPrefTool:
    def _tool(self, reply, refresh_calls):
        from slife.tools.context import ToolContext
        from slife.tools.user_prefs import AddUserPrefTool

        client = AsyncMock()
        client.call_tool.return_value = json.dumps(reply)
        tool = AddUserPrefTool()
        tool._ctx = ToolContext(
            memfiles_client=client,
            refresh_system_prompt=lambda: refresh_calls.append(1),
        )
        return tool

    @pytest.mark.asyncio
    async def test_delegates_and_refreshes_on_append(self):
        refresh_calls = []
        tool = self._tool(
            {"appended": True, "duplicate": False, "item": "1. **A** — x",
             "items": 1, "chars": 12, "path": "x/USER.md"},
            refresh_calls,
        )
        out = json.loads(await tool.execute(preference="**A** — x"))
        assert out["appended"]
        assert refresh_calls == [1]
        tool._ctx.memfiles_client.call_tool.assert_awaited_once_with(
            "__user_pref_append", {"preference": "**A** — x"}
        )

    @pytest.mark.asyncio
    async def test_duplicate_skips_refresh(self):
        refresh_calls = []
        tool = self._tool(
            {"appended": False, "duplicate": True, "items": 1, "chars": 12},
            refresh_calls,
        )
        out = json.loads(await tool.execute(preference="**A** — x"))
        assert out["duplicate"]
        assert refresh_calls == []

    @pytest.mark.asyncio
    async def test_offline_client_reports_error(self):
        from slife.tools.context import ToolContext
        from slife.tools.user_prefs import AddUserPrefTool

        tool = AddUserPrefTool()
        tool._ctx = ToolContext(memfiles_client=None)
        out = await tool.execute(preference="**A** — x")
        assert "not connected" in out

    @pytest.mark.asyncio
    async def test_missing_preference_rejected(self):
        refresh_calls = []
        tool = self._tool({}, refresh_calls)
        out = await tool.execute(preference="   ")
        assert "preference is required" in out
        assert refresh_calls == []