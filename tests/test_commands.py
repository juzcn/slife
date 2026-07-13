"""Tests for slash-command registry and completion logic."""

import pytest

from slife.ui.commands import (
    COMMANDS,
    SlashCommand,
    match_commands,
    complete_file_path,
)


class TestSlashCommand:
    def test_command_attrs(self):
        cmd = SlashCommand(
            name="/file",
            description="Attach an image",
            usage="/file <path>",
        )
        assert cmd.name == "/file"
        assert cmd.description == "Attach an image"
        assert cmd.usage == "/file <path>"


class TestMatchCommands:
    def test_no_slash_returns_empty(self):
        assert match_commands("hello") == []
        assert match_commands("") == []

    def test_just_slash_returns_all(self):
        result = match_commands("/")
        assert len(result) >= 1
        names = {c.name for c in result}
        assert "/file" in names

    def test_partial_match(self):
        result = match_commands("/f")
        assert len(result) >= 1
        assert all(c.name.startswith("/f") for c in result)

    def test_full_match(self):
        result = match_commands("/file")
        names = {c.name for c in result}
        assert "/file" in names

    def test_no_match(self):
        assert match_commands("/nonexistent") == []

    def test_case_insensitive(self):
        result = match_commands("/FILE")
        names = {c.name for c in result}
        assert "/file" in names


class TestCompleteFilePath:
    def test_empty_partial_returns_cwd_entries(self, tmp_path):
        import os
        orig = os.getcwd()
        try:
            os.chdir(tmp_path)
            (tmp_path / "readme.md").touch()
            (tmp_path / "images").mkdir()
            paths = complete_file_path("")
            assert any("readme.md" in p for p in paths)
            assert any("images/" in p for p in paths)
        finally:
            os.chdir(orig)

    def test_partial_match(self, tmp_path):
        import os
        orig = os.getcwd()
        try:
            os.chdir(tmp_path)
            (tmp_path / "cat.png").touch()
            (tmp_path / "dog.png").touch()
            (tmp_path / "readme.md").touch()
            paths = complete_file_path("c")
            assert any("cat.png" in p for p in paths)
            assert not any("dog.png" in p for p in paths)
        finally:
            os.chdir(orig)

    def test_no_match_returns_empty(self):
        paths = complete_file_path("xyznonexistent123")
        assert paths == []

    def test_max_20_results(self, tmp_path):
        import os
        orig = os.getcwd()
        try:
            os.chdir(tmp_path)
            for i in range(30):
                (tmp_path / f"file_{i:02d}.txt").touch()
            paths = complete_file_path("file_")
            assert len(paths) <= 20
        finally:
            os.chdir(orig)


class TestRegistryExtensibility:
    """Verify the command registry can be extended."""

    def test_commands_is_list(self):
        assert isinstance(COMMANDS, list)
        assert all(isinstance(c, SlashCommand) for c in COMMANDS)

    def test_add_command(self):
        # Verify new commands can be appended and will be matched
        try:
            new_cmd = SlashCommand(
                name="/test",
                description="Test command",
                usage="/test <arg>",
            )
            COMMANDS.append(new_cmd)
            result = match_commands("/test")
            assert len(result) == 1
            assert result[0].name == "/test"
        finally:
            COMMANDS.remove(new_cmd)
