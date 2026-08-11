"""Code execution tools.

Tools:
    execute_shell          — run shell commands (disabled by default)
    run_python_script      — run Python scripts with JSON arguments
    install_python_package — install PyPI packages into slife's environment
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys

from slife.platform import _resolve_skill_script
from slife.tools.base import Tool


async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess and its whole process tree.

    ``process.kill()`` only kills the direct child (``cmd.exe`` / ``sh``).
    Any grandchildren it spawned — e.g. yt-dlp started by a shell, or
    ffmpeg spawned by yt-dlp — survive as orphans, keep writing to the
    console and garble the TUI, and hold their pipes open forever.  This
    kills the tree: ``taskkill /T`` on Windows, the process group on POSIX
    (children are spawned with ``start_new_session=True``).

    Runs even when the direct child already exited — its grandchildren may
    still be alive as orphans (REVIEW M4).  ``taskkill``/``killpg`` on a dead
    pid is harmless (errors are swallowed).
    """
    if process is None:
        return
    if os.name == "nt":
        await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        try:
            pgid = os.getpgid(process.pid)
            if pgid == process.pid:
                # Child leads its own process group (start_new_session=True)
                # — kill the whole group safely.
                os.killpg(pgid, signal.SIGKILL)
            else:
                # Child shares our process group — killing the group would
                # SIGKILL us too.  Kill only the direct child (REVIEW H8 fix).
                os.kill(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        await process.wait()
    except (ProcessLookupError, OSError):
        pass

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# execute_shell
# ═══════════════════════════════════════════════════════════════════════

class ShellTool(Tool):
    """Execute a shell command via the system shell (cmd on Windows, sh on Unix)."""

    name = "execute_shell"
    category = "Execution"
    description = "Run a shell command. Returns stdout + stderr. Default timeout 30s."
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute."},
            "timeout": {"type": "integer", "description": "Timeout in seconds. Default 30."},
        },
        "required": ["command"],
    }

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg, config, ctx=None):
        tool = cls(timeout=cfg.get("timeout", 30))
        if ctx is not None:
            object.__setattr__(tool, "_ctx", ctx)
        return tool

    async def execute(self, **kwargs) -> str:
        command: str = kwargs["command"]
        timeout: int = kwargs.get("timeout", self.timeout)
        logger.debug("shell_exec cmd=%.200s timeout=%d", command, timeout)

        process = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group on POSIX so a timeout can kill the whole
            # tree (sh + children like yt-dlp/ffmpeg) — see _kill_process_tree.
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Kill the whole tree — a bare process.kill() only kills the
            # shell and orphans yt-dlp/ffmpeg, which keep writing to the
            # console and garble the TUI.
            await _kill_process_tree(process)
            logger.warning("shell_timeout timeout=%ds cmd=%.200s", timeout, command)
            return f"Error: Command timed out after {timeout}s"

        output = stdout.decode("utf-8", errors="replace")
        err_output = stderr.decode("utf-8", errors="replace")
        result = output
        if err_output:
            result += f"\n[stderr]\n{err_output}"
        if not result.strip():
            result = f"Command completed with exit code {process.returncode} (no output)"

        logger.debug("shell_done exit=%d out_len=%d err_len=%d",
                     process.returncode or 0, len(output), len(err_output))
        return result


# ═══════════════════════════════════════════════════════════════════════
# run_python_script
# ═══════════════════════════════════════════════════════════════════════

def _parse_input(input_str: str) -> tuple[str, str]:
    """Split input into (script_or_code, json_args)."""
    brace = input_str.find("{")
    bracket = input_str.find("[")
    candidates = [i for i in (brace, bracket) if i >= 0]
    split_at = min(candidates) if candidates else len(input_str)
    if split_at == len(input_str):
        return input_str.strip(), ""
    return input_str[:split_at].strip(), input_str[split_at:].strip()


class RunPythonScriptTool(Tool):
    """Run a Python script with arguments, or inline code with -c."""

    name = "run_python_script"
    category = "Execution"
    description = (
        "Run a Python script or inline code. "
        "Script: 'path/to/script.py {\"arg\": \"val\"}'. Inline: '-c print(1+1)'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "Script path [+ JSON args], or '-c <code>'.",
            },
        },
        "required": ["script"],
    }

    async def execute(self, **kwargs) -> str:
        input_str = kwargs["script"]

        if input_str.startswith("-c ") or input_str.startswith("-c"):
            code = input_str[2:].strip()
            argv = [sys.executable, "-c", code]
            logger.debug("run_python_script argv=%s", argv)
        else:
            script, args = _parse_input(input_str)
            script = _resolve_skill_script(script)
            argv = [sys.executable, script]
            if args:
                argv.append(args)
            logger.debug("run_python_script argv=%s", argv)

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group on POSIX so cancel/timeout can kill the
            # whole tree — see _kill_process_tree.
            start_new_session=True,
        )
        try:
            stdout, stderr = await proc.communicate()
        except asyncio.CancelledError:
            # The loop's tool-timeout cancels communicate() — kill the
            # child tree so the running script (e.g. a yt-dlp download)
            # doesn't survive as an orphan writing to the console.
            await _kill_process_tree(proc)
            raise
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            if out:
                return out
            return f"Error (exit {proc.returncode}): {err}" if err else f"Error (exit {proc.returncode})"
        return out if out else f"Script completed with no output. stderr: {err}" if err else "Script completed with no output."


# ═══════════════════════════════════════════════════════════════════════
# install_python_package
# ═══════════════════════════════════════════════════════════════════════

class InstallPythonPackageTool(Tool):
    """Install Python packages into slife's environment via uv pip install."""

    name = "install_python_package"
    category = "Execution"
    description = "Install PyPI packages via uv pip install. Supports version pins like 'requests>=2.31'."
    parameters = {
        "type": "object",
        "properties": {
            "packages": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Package specs, e.g. ['requests', 'beautifulsoup4>=4.12'].",
            },
        },
        "required": ["packages"],
    }

    async def execute(self, **kwargs) -> str:
        packages: list[str] = kwargs["packages"]
        if not packages:
            return "Error: no package names provided."
        logger.info("pip_install packages=%s", packages)

        proc = await asyncio.create_subprocess_exec(
            "uv", "pip", "install", "--python", sys.executable, *packages,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode == 0:
            logger.info("pip_install_done packages=%s", packages)
            return out or f"✓ Installed: {', '.join(packages)}"
        else:
            logger.warning("pip_install_failed packages=%s err=%s", packages, err)
            return f"Error installing {', '.join(packages)}:\n{err}" if err else f"Error installing {', '.join(packages)} (exit {proc.returncode})"
