"""Tests for slife.tools.exec — code execution tools."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from slife.tools.exec import (
    ShellTool,
    RunPythonScriptTool,
    InstallPythonPackageTool,
    _kill_process_tree,
    _parse_input,
)


# ═══════════════════════════════════════════════════════════════════════════
# _parse_input helper
# ═══════════════════════════════════════════════════════════════════════════


class TestParseInput:
    """Tests for _parse_input helper."""

    def test_no_json_args(self):
        script, args = _parse_input("path/to/script.py")
        assert script == "path/to/script.py"
        assert args == ""

    def test_with_json_dict_args(self):
        script, args = _parse_input('path/to/script.py {"key": "val"}')
        assert script == "path/to/script.py"
        assert args == '{"key": "val"}'

    def test_with_json_list_args(self):
        script, args = _parse_input('path/to/script.py ["a", "b"]')
        assert script == "path/to/script.py"
        assert args == '["a", "b"]'

    def test_json_args_before_brace_takes_precedence(self):
        """Curly-brace JSON is preferred over bracket JSON if both appear."""
        script, args = _parse_input('script.py {"a": 1} ["b"]')
        assert script == "script.py"
        assert args == '{"a": 1} ["b"]'

    def test_empty_input(self):
        script, args = _parse_input("")
        assert script == ""
        assert args == ""


# ═══════════════════════════════════════════════════════════════════════════
# ShellTool — metadata
# ═══════════════════════════════════════════════════════════════════════════


class TestShellToolMetadata:
    """Tests for ShellTool class-level attributes."""

    def test_name(self):
        assert ShellTool.name == "execute_shell"

    def test_description(self):
        assert "Run a shell command" in ShellTool.description

    def test_category(self):
        assert ShellTool.category == "Execution"

    def test_parameters_schema_type(self):
        assert ShellTool.parameters["type"] == "object"

    def test_parameters_command_required(self):
        assert "command" in ShellTool.parameters["required"]
        assert "command" in ShellTool.parameters["properties"]

    def test_parameters_timeout_optional(self):
        assert "timeout" not in ShellTool.parameters["required"]
        assert "timeout" in ShellTool.parameters["properties"]
        assert ShellTool.parameters["properties"]["timeout"]["type"] == "integer"


# ═══════════════════════════════════════════════════════════════════════════
# ShellTool — construction
# ═══════════════════════════════════════════════════════════════════════════


class TestShellToolConstruction:
    """Tests for ShellTool.__init__."""

    def test_default_timeout(self):
        tool = ShellTool()
        assert tool.timeout == 30

    def test_custom_timeout(self):
        tool = ShellTool(timeout=60)
        assert tool.timeout == 60


# ═══════════════════════════════════════════════════════════════════════════
# ShellTool — execute
# ═══════════════════════════════════════════════════════════════════════════


class TestShellToolExecute:
    """Tests for ShellTool.execute."""

    @pytest.mark.asyncio
    async def test_successful_command(self):
        tool = ShellTool(timeout=10)
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"hello world", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_shell", AsyncMock(return_value=mock_process)):
            result = await tool.execute(command="echo hello")

        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_command_not_found_error(self):
        """Non-existent command returns stderr, no crash."""
        tool = ShellTool(timeout=10)
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b"notfound: command not found"))
        mock_process.returncode = 127

        with patch("asyncio.create_subprocess_shell", AsyncMock(return_value=mock_process)):
            result = await tool.execute(command="notfound_cmd")

        assert "[stderr]" in result
        assert "notfound" in result

    @pytest.mark.asyncio
    async def test_timeout_error(self):
        tool = ShellTool(timeout=5)
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_process.returncode = None  # still running when the timeout fires
        mock_process.kill = MagicMock()
        mock_process.wait = AsyncMock()

        with patch("asyncio.create_subprocess_shell", AsyncMock(return_value=mock_process)):
            result = await tool.execute(command="sleep 999")

        assert "Error:" in result
        assert "timed out" in result
        assert "5s" in result
        mock_process.kill.assert_called_once()
        mock_process.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stderr_capture(self):
        """Stderr output is appended after stdout with a [stderr] label."""
        tool = ShellTool(timeout=10)
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"stdout line", b"stderr line"))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_shell", AsyncMock(return_value=mock_process)):
            result = await tool.execute(command="cmd_with_stderr")

        assert "stdout line" in result
        assert "[stderr]" in result
        assert "stderr line" in result

    @pytest.mark.asyncio
    async def test_env_var_setting(self):
        """Environment variables set via shell syntax are visible to the command."""
        tool = ShellTool(timeout=10)
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"my_value", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_shell", AsyncMock(return_value=mock_process)):
            result = await tool.execute(command="export MY_VAR=my_value && echo $MY_VAR")

        assert result == "my_value"


# ═══════════════════════════════════════════════════════════════════════════
# InstallPythonPackageTool — metadata
# ═══════════════════════════════════════════════════════════════════════════


class TestInstallPythonPackageToolMetadata:
    """Tests for InstallPythonPackageTool class-level attributes."""

    def test_name(self):
        assert InstallPythonPackageTool.name == "install_python_package"

    def test_description(self):
        assert "Install PyPI packages" in InstallPythonPackageTool.description

    def test_category(self):
        assert InstallPythonPackageTool.category == "Execution"

    def test_parameters_schema_type(self):
        assert InstallPythonPackageTool.parameters["type"] == "object"

    def test_parameters_packages_required(self):
        assert "packages" in InstallPythonPackageTool.parameters["required"]
        assert "packages" in InstallPythonPackageTool.parameters["properties"]
        prop = InstallPythonPackageTool.parameters["properties"]["packages"]
        assert prop["type"] == "array"
        assert prop["items"]["type"] == "string"


# ═══════════════════════════════════════════════════════════════════════════
# InstallPythonPackageTool — execute
# ═══════════════════════════════════════════════════════════════════════════


class TestInstallPythonPackageToolExecute:
    """Tests for InstallPythonPackageTool.execute."""

    @pytest.mark.asyncio
    async def test_successful_install_with_output(self):
        """Successful install returns uv output."""
        tool = InstallPythonPackageTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"Successfully installed requests-2.31.0", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            with patch("asyncio.wait_for", AsyncMock(return_value=(b"Successfully installed requests-2.31.0", b""))):
                result = await tool.execute(packages=["requests"])

        assert "Successfully installed" in result

    @pytest.mark.asyncio
    async def test_successful_install_no_output(self):
        """When uv produces no stdout, fallback success message is returned."""
        tool = InstallPythonPackageTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            with patch("asyncio.wait_for", AsyncMock(return_value=(b"", b""))):
                result = await tool.execute(packages=["requests"])

        assert "Installed:" in result
        assert "requests" in result

    @pytest.mark.asyncio
    async def test_pip_not_found_error(self):
        """When uv is not available, subprocess raises FileNotFoundError."""
        tool = InstallPythonPackageTool()

        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError("No such file: uv"))):
            with pytest.raises(FileNotFoundError):
                await tool.execute(packages=["requests"])

    @pytest.mark.asyncio
    async def test_package_install_failure(self):
        """Install failure returns error with stderr details."""
        tool = InstallPythonPackageTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b"ERROR: No matching distribution found"))
        mock_process.returncode = 1

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            with patch("asyncio.wait_for", AsyncMock(return_value=(b"", b"ERROR: No matching distribution found"))):
                result = await tool.execute(packages=["nonexistent-pkg-xyz"])

        assert "Error installing" in result
        assert "No matching distribution" in result

    @pytest.mark.asyncio
    async def test_package_name_with_special_chars(self):
        """Package specs with version pins and extras are passed correctly."""
        tool = InstallPythonPackageTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"Successfully installed", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)) as mock_exec:
            with patch("asyncio.wait_for", AsyncMock(return_value=(b"Successfully installed", b""))):
                await tool.execute(packages=["requests>=2.31,<3.0", "beautifulsoup4[html5lib]>=4.12"])

        # Verify the special-char packages were passed through to uv
        call_args = mock_exec.call_args[0]
        # The call looks like: uv, pip, install, --python, <python>, pkg1, pkg2
        assert "requests>=2.31,<3.0" in call_args
        assert "beautifulsoup4[html5lib]>=4.12" in call_args

    @pytest.mark.asyncio
    async def test_empty_packages_list(self):
        """Empty packages list returns an error."""
        tool = InstallPythonPackageTool()
        result = await tool.execute(packages=[])
        assert result == "Error: no package names provided."


# ═══════════════════════════════════════════════════════════════════════════
# RunPythonScriptTool — metadata
# ═══════════════════════════════════════════════════════════════════════════


class TestRunPythonScriptToolMetadata:
    """Tests for RunPythonScriptTool class-level attributes."""

    def test_name(self):
        assert RunPythonScriptTool.name == "run_python_script"

    def test_description(self):
        assert "Run a Python script" in RunPythonScriptTool.description

    def test_category(self):
        assert RunPythonScriptTool.category == "Execution"

    def test_parameters_schema_type(self):
        assert RunPythonScriptTool.parameters["type"] == "object"

    def test_parameters_script_required(self):
        assert "script" in RunPythonScriptTool.parameters["required"]
        assert "script" in RunPythonScriptTool.parameters["properties"]
        assert RunPythonScriptTool.parameters["properties"]["script"]["type"] == "string"


# ═══════════════════════════════════════════════════════════════════════════
# RunPythonScriptTool — execute
# ═══════════════════════════════════════════════════════════════════════════


class TestRunPythonScriptToolExecute:
    """Tests for RunPythonScriptTool.execute."""

    @pytest.mark.asyncio
    async def test_successful_script_run(self):
        """A valid script file runs and returns stdout."""
        tool = RunPythonScriptTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"script output", b""))
        mock_process.returncode = 0

        with patch("slife.tools.exec._resolve_skill_script", return_value="/fake/script.py"):
            with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
                result = await tool.execute(script="script.py")

        assert result == "script output"

    @pytest.mark.asyncio
    async def test_successful_inline_code(self):
        """Inline code with -c flag runs and returns result."""
        tool = RunPythonScriptTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"2", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            result = await tool.execute(script="-c print(1+1)")

        assert result == "2"

    @pytest.mark.asyncio
    async def test_inline_code_without_space(self):
        """Inline code with -c<code> (no space) also works."""
        tool = RunPythonScriptTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"hello", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            result = await tool.execute(script="-cprint('hello')")

        assert result == "hello"

    @pytest.mark.asyncio
    async def test_inline_code_with_double_quote_wrapping(self):
        """Shell-style '-c "code"' strips the wrapping quotes before exec.

        Without this, python -c receives a bare string-literal expression and
        silently does nothing (exit 0, no output) — the turn-488 regression
        where every -c call reported "Script completed with no output."
        """
        tool = RunPythonScriptTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"openpyxl 3.1.5", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)) as mock_exec:
            result = await tool.execute(
                script="-c \"import openpyxl; print('openpyxl', openpyxl.__version__)\""
            )

        assert result == "openpyxl 3.1.5"
        # The code handed to python -c must not carry the wrapping quotes.
        call_args = mock_exec.call_args[0]
        assert call_args[1] == "-c"
        assert call_args[2] == "import openpyxl; print('openpyxl', openpyxl.__version__)"

    @pytest.mark.asyncio
    async def test_inline_code_with_single_quote_wrapping(self):
        """Single-quoted '-c 'code'' is stripped too, even when the code
        contains double quotes."""
        tool = RunPythonScriptTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)) as mock_exec:
            result = await tool.execute(script="-c 'print(\"hi\")'")

        assert result == "ok"
        call_args = mock_exec.call_args[0]
        assert call_args[2] == 'print("hi")'

    @pytest.mark.asyncio
    async def test_syntax_error(self):
        """Python syntax error returns error with exit code and stderr."""
        tool = RunPythonScriptTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b"SyntaxError: invalid syntax"))
        mock_process.returncode = 1

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            result = await tool.execute(script="-c print(1+")

        assert "Error" in result
        assert "exit 1" in result
        assert "SyntaxError" in result

    @pytest.mark.asyncio
    async def test_syntax_error_stdout_present(self):
        """If there is stdout even on error, it's returned directly."""
        tool = RunPythonScriptTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"partial output before crash", b"Traceback ..."))
        mock_process.returncode = 1

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            result = await tool.execute(script="-c print('hi'); 1/0")

        assert result == "partial output before crash"

    @pytest.mark.asyncio
    async def test_import_error(self):
        """Import error returns error with exit code and stderr."""
        tool = RunPythonScriptTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b"ModuleNotFoundError: No module named 'nonexistent'"))
        mock_process.returncode = 1

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            result = await tool.execute(script="-c import nonexistent_module")

        assert "Error" in result
        assert "exit 1" in result
        assert "ModuleNotFoundError" in result

    @pytest.mark.asyncio
    async def test_script_with_json_args(self):
        """Script path with JSON arguments passes args correctly."""
        tool = RunPythonScriptTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"done", b""))
        mock_process.returncode = 0

        with patch("slife.tools.exec._resolve_skill_script", return_value="/fake/script.py"):
            with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)) as mock_exec:
                result = await tool.execute(script='script.py {"key": "val"}')

        assert result == "done"
        # Verify args were passed to subprocess
        call_args = mock_exec.call_args[0]
        assert '{"key": "val"}' in call_args

    @pytest.mark.asyncio
    async def test_no_output(self):
        """Script with no output returns a descriptive message."""
        tool = RunPythonScriptTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            result = await tool.execute(script="-c pass")

        assert "no output" in result

    @pytest.mark.asyncio
    async def test_no_output_with_stderr(self):
        """Script with no stdout but stderr shows stderr in message."""
        tool = RunPythonScriptTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b"deprecation warning"))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            result = await tool.execute(script="-c import warnings; warnings.warn('deprecation')")

        assert "no output" in result
        assert "stderr" in result
        assert "deprecation warning" in result


# ═══════════════════════════════════════════════════════════════════════════
# Error format
# ═══════════════════════════════════════════════════════════════════════════


class TestErrorFormat:
    """All execution-tool errors start with 'Error:' prefix."""

    @pytest.mark.asyncio
    async def test_timeout_error_starts_with_error(self):
        tool = ShellTool(timeout=1)
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_process.returncode = None  # still running when the timeout fires
        mock_process.kill = MagicMock()
        mock_process.wait = AsyncMock()

        with patch("asyncio.create_subprocess_shell", AsyncMock(return_value=mock_process)):
            result = await tool.execute(command="sleep 999")

        assert result.startswith("Error:")

    def test_empty_packages_error_starts_with_error(self):
        tool = InstallPythonPackageTool()
        result = asyncio.run(tool.execute(packages=[]))
        assert result.startswith("Error:")

    @pytest.mark.asyncio
    async def test_install_failure_error_starts_with_error(self):
        tool = InstallPythonPackageTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b"some error"))
        mock_process.returncode = 1

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            with patch("asyncio.wait_for", AsyncMock(return_value=(b"", b"some error"))):
                result = await tool.execute(packages=["bad-pkg"])

        assert result.startswith("Error installing")

    @pytest.mark.asyncio
    async def test_python_script_error_starts_with_error(self):
        tool = RunPythonScriptTool()
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b"traceback"))
        mock_process.returncode = 1

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            result = await tool.execute(script="-c 1/0")

        assert result.startswith("Error ")


# ═══════════════════════════════════════════════════════════════════════════
# Required parameters
# ═══════════════════════════════════════════════════════════════════════════


class TestRequiredParams:
    """Parameter validation — required params cause KeyError when missing."""

    @pytest.mark.asyncio
    async def test_execute_shell_requires_command(self):
        """Calling execute without 'command' raises KeyError."""
        tool = ShellTool()
        with pytest.raises(KeyError):
            await tool.execute()

    @pytest.mark.asyncio
    async def test_install_python_package_requires_packages(self):
        """Calling execute without 'packages' raises KeyError."""
        tool = InstallPythonPackageTool()
        with pytest.raises(KeyError):
            await tool.execute()

    @pytest.mark.asyncio
    async def test_run_python_script_requires_script(self):
        """Calling execute without 'script' raises KeyError."""
        tool = RunPythonScriptTool()
        with pytest.raises(KeyError):
            await tool.execute()


# ── Process-tree kill (REVIEW: exec timeout leaks orphaned children) ─────


class TestKillProcessTree:
    """_kill_process_tree must terminate the process AND its descendants.

    A bare process.kill() only kills the shell (cmd.exe / sh); children
    like yt-dlp/ffmpeg survive as orphans, keep writing to the console,
    and garble the TUI.  This is the regression for that bug.
    """

    @pytest.mark.asyncio
    async def test_kill_process_tree_terminates_real_process(self):
        """The process itself is terminated and reaped."""
        import sys as _sys

        proc = await asyncio.create_subprocess_exec(
            _sys.executable, "-c", "import time; time.sleep(300)",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            # Own process group so a POSIX killpg only hits the child, not
            # the pytest/CI process group.
            start_new_session=True,
        )
        assert proc.returncode is None
        await _kill_process_tree(proc)
        assert proc.returncode is not None

    @pytest.mark.asyncio
    async def test_kill_process_tree_uses_taskkill_tree_on_windows(self, monkeypatch):
        """On Windows the tree is killed via taskkill /T, not just the shell."""
        import os as _os
        import sys as _sys
        from slife.tools import exec as exec_mod

        proc = await asyncio.create_subprocess_exec(
            _sys.executable, "-c", "import time; time.sleep(300)",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            # Own process group so a POSIX killpg only hits the child.
            start_new_session=True,
        )

        fake_run = MagicMock()
        monkeypatch.setattr(exec_mod.subprocess, "run", fake_run)
        await _kill_process_tree(proc)

        if _os.name == "nt":
            assert fake_run.called
            args = fake_run.call_args[0][0]
            assert args[0] == "taskkill"
            assert "/F" in args and "/T" in args  # force + whole tree
            assert str(proc.pid) in args
        # process is actually gone regardless of platform
        assert proc.returncode is not None
