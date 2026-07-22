"""Tests for credstore._shell — formatting and profile persistence."""

import os

import pytest


class TestFormatExport:
    """Tests for format_export() — shell export formatting."""

    def test_bash_simple(self):
        from credstore._shell import format_export
        result = format_export("MY_KEY", "my-secret", "bash")
        assert result == "export MY_KEY='my-secret'"

    def test_bash_with_single_quote(self):
        from credstore._shell import format_export
        result = format_export("KEY", "val'ue", "bash")
        assert result == "export KEY='val'\\''ue'"

    def test_powershell(self):
        from credstore._shell import format_export
        result = format_export("MY_KEY", "my-secret", "powershell")
        assert result == "$env:MY_KEY = 'my-secret'"

    def test_powershell_backtick_escape(self):
        from credstore._shell import format_export
        result = format_export("KEY", "abc`def", "powershell")
        assert result == "$env:KEY = 'abc``def'"

    def test_cmd(self):
        from credstore._shell import format_export
        result = format_export("MY_KEY", "my-secret", "cmd")
        assert result == "set MY_KEY=my-secret"

    def test_auto_windows(self, monkeypatch):
        monkeypatch.setattr("os.name", "nt")
        monkeypatch.delenv("PROMPT", raising=False)  # clear cmd.exe indicator
        monkeypatch.setenv("PSModulePath", "C:\\Modules")  # force PowerShell
        from credstore._shell import format_export
        result = format_export("KEY", "val", "auto")
        assert result.startswith("$env:KEY")

    def test_auto_unix(self, monkeypatch):
        monkeypatch.setattr("os.name", "posix")
        from credstore._shell import format_export
        result = format_export("KEY", "val", "auto")
        assert result.startswith("export KEY")


class TestFormatUnset:
    """Tests for format_unset() — shell environment variable removal."""

    def test_bash(self):
        from credstore._shell import format_unset
        assert format_unset("MY_KEY", "bash") == "unset MY_KEY"

    def test_powershell(self):
        from credstore._shell import format_unset
        assert format_unset("MY_KEY", "powershell") == "Remove-Item Env:MY_KEY"

    def test_cmd(self):
        from credstore._shell import format_unset
        assert format_unset("MY_KEY", "cmd") == "set MY_KEY="

    def test_auto_windows(self, monkeypatch):
        monkeypatch.setattr("os.name", "nt")
        monkeypatch.delenv("PROMPT", raising=False)
        monkeypatch.setenv("PSModulePath", "C:\\Modules")
        from credstore._shell import format_unset
        result = format_unset("KEY", "auto")
        assert result == "Remove-Item Env:KEY"

    def test_auto_unix(self, monkeypatch):
        monkeypatch.setattr("os.name", "posix")
        from credstore._shell import format_unset
        result = format_unset("KEY", "auto")
        assert result == "unset KEY"

    def test_unknown_shell_raises(self):
        from credstore._shell import format_unset
        with pytest.raises(ValueError):
            format_unset("KEY", "fish")


class TestProfilePersistence:
    """Tests for profile-based persistence (add_to_profile / remove_from_profile)."""

    def test_get_profile_path_powershell(self, monkeypatch):
        monkeypatch.setitem(os.environ, "PROFILE", "C:\\Users\\me\\Documents\\PowerShell\\profile.ps1")
        monkeypatch.setitem(os.environ, "PSModulePath", "C:\\Modules")  # not cmd.exe
        from credstore._shell import get_profile_path
        p = get_profile_path("powershell")
        assert p is not None
        assert p.name == "Microsoft.PowerShell_profile.ps1" or "profile" in str(p).lower()

    def test_get_profile_path_cmd(self, monkeypatch):
        """Windows uses registry — cmd.exe has no profile file."""
        monkeypatch.setitem(os.environ, "PROMPT", "$P$G")
        from credstore._shell import get_profile_path
        p = get_profile_path("cmd")
        assert p is None  # Windows uses registry, not profile

    def test_get_profile_path_bash(self, monkeypatch):
        monkeypatch.setattr("os.environ", {"HOME": "/home/user"})
        from credstore._shell import get_profile_path
        p = get_profile_path("bash")
        assert p is not None
        assert p.name == ".bashrc"

    def test_add_to_profile_creates_file(self, tmp_path, monkeypatch):
        """add_to_profile creates profile file and appends inject line."""
        monkeypatch.setitem(os.environ, "HOME", str(tmp_path))
        from credstore._shell import add_to_profile
        assert add_to_profile("MY_KEY", "bash") is True
        content = (tmp_path / ".bashrc").read_text()
        assert "# credstore: MY_KEY" in content
        assert "credstore inject MY_KEY" in content

    def test_add_to_profile_overwrites_existing(self, tmp_path, monkeypatch):
        """Second inject of same key overwrites, doesn't duplicate."""
        profile = tmp_path / ".bashrc"
        profile.write_text("# credstore: MY_KEY\neval old-line\n")
        monkeypatch.setitem(os.environ, "HOME", str(tmp_path))
        from credstore._shell import add_to_profile
        assert add_to_profile("MY_KEY", "bash") is True
        content = profile.read_text()
        assert "old-line" not in content
        assert "credstore inject MY_KEY" in content
        assert content.count("# credstore: MY_KEY") == 1

    def test_remove_from_profile_removes_key(self, tmp_path, monkeypatch):
        profile = tmp_path / ".bashrc"
        profile.write_text("export FOO=bar\n# credstore: MY_KEY\neval old-line\n# credstore: OTHER\neval other\n")
        monkeypatch.setitem(os.environ, "HOME", str(tmp_path))
        from credstore._shell import remove_from_profile
        assert remove_from_profile("MY_KEY", "bash") is True
        content = profile.read_text()
        assert "MY_KEY" not in content
        assert "OTHER" in content

    def test_remove_from_profile_not_found(self, tmp_path, monkeypatch):
        profile = tmp_path / ".bashrc"
        profile.write_text("export FOO=bar\n")
        monkeypatch.setitem(os.environ, "HOME", str(tmp_path))
        from credstore._shell import remove_from_profile
        assert remove_from_profile("NONEXISTENT", "bash") is False

    def test_remove_key_lines_cleans_trailing_blanks(self):
        from credstore._shell import _remove_key_lines
        content = "\n# credstore: X\neval x\n\n\n"
        result = _remove_key_lines(content, "X")
        assert not result.endswith("\n\n")

    def test_powershell_profile_line_format(self):
        from credstore._shell import _make_profile_line
        result = _make_profile_line("MY_KEY", "powershell")
        assert "# credstore: MY_KEY" in result
        assert "Invoke-Expression" in result
        assert "2>$null" in result

    def test_cmd_profile_line_format(self):
        """Windows uses registry — _make_profile_line not used for cmd."""
        from credstore._shell import _make_profile_line
        result = _make_profile_line("MY_KEY", "cmd")
        # Falls through to bash format when not powershell
        assert "eval" in result

    def test_bash_profile_line_format(self):
        from credstore._shell import _make_profile_line
        result = _make_profile_line("MY_KEY", "bash")
        assert "# credstore: MY_KEY" in result
        assert "eval" in result
