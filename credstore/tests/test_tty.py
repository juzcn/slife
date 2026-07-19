"""Tests for credstore._tty — masked terminal input."""

import sys
from unittest.mock import MagicMock, patch

import pytest


class TestMaskedInputDispatcher:
    """Tests for masked_input platform dispatch."""

    def test_dispatches_to_windows_on_win32(self):
        with patch.object(sys, "platform", "win32"):
            with patch("credstore._tty._masked_input_windows", return_value="secret") as mock_win:
                result = masked_input("Password: ")
                mock_win.assert_called_once()
                assert result == "secret"

    def test_dispatches_to_unix_on_linux(self):
        with patch.object(sys, "platform", "linux"):
            with patch("credstore._tty._masked_input_unix", return_value="secret") as mock_unix:
                result = masked_input("Password: ")
                mock_unix.assert_called_once()
                assert result == "secret"

    def test_dispatches_to_unix_on_darwin(self):
        with patch.object(sys, "platform", "darwin"):
            with patch("credstore._tty._masked_input_unix", return_value="secret") as mock_unix:
                result = masked_input("Password: ")
                mock_unix.assert_called_once()
                assert result == "secret"

    def test_writes_prompt_to_stdout(self, capsys):
        with patch.object(sys, "platform", "win32"):
            with patch("credstore._tty._masked_input_windows", return_value=""):
                masked_input("Enter: ")
        captured = capsys.readouterr()
        assert "Enter: " in captured.out


# Import the function under test normally
from credstore._tty import _masked_input_windows, _masked_input_unix, masked_input


class TestMaskedInputWindows:
    """Tests for _masked_input_windows using sys.modules patching."""

    @pytest.fixture(autouse=True)
    def _setup_msvcrt_mock(self):
        """Inject a mock msvcrt into sys.modules for local import."""
        mock_msvcrt = MagicMock()
        mock_msvcrt.kbhit.return_value = False
        with patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
            self.mock_msvcrt = mock_msvcrt
            yield

    def test_enter_ends_input(self):
        self.mock_msvcrt.getwch.side_effect = ["a", "b", "\r"]
        result = _masked_input_windows()
        assert result == "ab"

    def test_newline_ends_input(self):
        self.mock_msvcrt.getwch.side_effect = ["x", "\n"]
        result = _masked_input_windows()
        assert result == "x"

    def test_ctrl_c_raises_keyboard_interrupt(self):
        self.mock_msvcrt.getwch.side_effect = ["a", "\x03"]
        with pytest.raises(KeyboardInterrupt):
            _masked_input_windows()

    def test_backspace_removes_char(self):
        self.mock_msvcrt.getwch.side_effect = ["a", "b", "\x08", "c", "\r"]
        result = _masked_input_windows()
        assert result == "ac"

    def test_del_removes_char(self):
        self.mock_msvcrt.getwch.side_effect = ["x", "\x7f", "y", "\r"]
        result = _masked_input_windows()
        assert result == "y"

    def test_backspace_at_empty_does_nothing(self):
        self.mock_msvcrt.getwch.side_effect = ["\x08", "a", "\r"]
        result = _masked_input_windows()
        assert result == "a"

    def test_escape_sequence_ignored(self):
        """Arrow keys send escape sequences — they should be consumed and ignored."""
        self.mock_msvcrt.getwch.side_effect = ["a", "\x1b", "[", "H", "b", "\r"]
        self.mock_msvcrt.kbhit.side_effect = [True, True, False]
        result = _masked_input_windows()
        assert result == "ab"

    def test_printable_chars_only(self):
        """Non-printable chars (ord < 32) except special cases are ignored."""
        self.mock_msvcrt.getwch.side_effect = ["\x01", "a", "\x02", "b", "\r"]
        result = _masked_input_windows()
        assert result == "ab"

    def test_empty_input(self):
        self.mock_msvcrt.getwch.side_effect = ["\r"]
        result = _masked_input_windows()
        assert result == ""


class TestMaskedInputUnix:
    """Tests for _masked_input_unix using sys.modules patching."""

    @pytest.fixture(autouse=True)
    def _setup_unix_mocks(self):
        """Inject mock termios and tty into sys.modules for local imports."""
        mock_termios = MagicMock()
        mock_tty = MagicMock()
        modules = {
            "termios": mock_termios,
            "tty": mock_tty,
        }
        with patch.dict("sys.modules", modules), \
             patch.object(sys.stdin, "fileno", return_value=0), \
             patch.object(sys.stdin, "read") as mock_read:
            self.mock_termios = mock_termios
            self.mock_tty = mock_tty
            self.mock_read = mock_read
            yield

    def _run_unix(self, chars):
        """Helper: run _masked_input_unix with given char sequence."""
        self.mock_read.side_effect = list(chars)
        return _masked_input_unix()

    def test_enter_ends_input(self):
        result = self._run_unix(["a", "b", "\r"])
        assert result == "ab"

    def test_newline_ends_input(self):
        result = self._run_unix(["x", "\n"])
        assert result == "x"

    def test_ctrl_c_raises_keyboard_interrupt(self):
        with pytest.raises(KeyboardInterrupt):
            self._run_unix(["a", "\x03"])

    def test_backspace_removes_char(self):
        result = self._run_unix(["a", "b", "\x08", "c", "\r"])
        assert result == "ac"

    def test_empty_input(self):
        result = self._run_unix(["\r"])
        assert result == ""

    def test_restores_terminal_settings_on_exception(self):
        self.mock_read.side_effect = Exception("broken pipe")
        try:
            _masked_input_unix()
        except Exception:
            pass
        self.mock_termios.tcsetattr.assert_called_once()

    def test_restores_terminal_settings_on_keyboard_interrupt(self):
        self.mock_read.side_effect = ["\x03"]
        with pytest.raises(KeyboardInterrupt):
            _masked_input_unix()
        self.mock_termios.tcsetattr.assert_called_once()

    def test_sets_raw_mode(self):
        self._run_unix(["a", "\r"])
        self.mock_tty.setraw.assert_called_once_with(0)

    def test_printable_chars_only(self):
        result = self._run_unix(["\x01", "a", "\x02", "b", "\r"])
        assert result == "ab"

    def test_long_input(self):
        chars = list("hello_world_1234567890") + ["\r"]
        result = self._run_unix(chars)
        assert result == "hello_world_1234567890"
