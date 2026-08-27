"""Code execution tools.

Tools:
    execute_shell          — run shell commands (default timeout 30s)
    run_python_script      — run Python scripts with JSON arguments
    install_python_package — install PyPI packages into slife's environment
    run_schedule_now       — trigger a scheduled task immediately (dispatch worker)
"""

import asyncio
import base64
import locale
import logging
import os
import signal
import subprocess
import sys
from typing import ClassVar

from slife.platform import _resolve_skill_script
from slife.logfmt import sanitize_secrets
from slife.tools.base import Tool, make_params, require_params


async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess and its whole process tree.

    ``process.kill()`` only kills the direct child (``cmd.exe`` / ``sh``).
    Any grandchildren it spawned — e.g. yt-dlp started by a shell, or
    ffmpeg spawned by yt-dlp — survive as orphans, keep writing to the
    console and garble the TUI, and hold their pipes open forever.  This
    kills the tree: ``taskkill /T`` on Windows, the process group on POSIX
    (children are spawned with ``start_new_session=True``).

    Runs even when the direct child already exited — its grandchildren may
    still be alive as orphans. ``taskkill``/``killpg`` on a dead
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
                # SIGKILL us too. Kill only the direct child.
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


def _shell_argv(command: str) -> list[str]:
    """Build argv that runs *command* in the shell the prompt claims.

    ``asyncio.create_subprocess_shell`` runs ``COMSPEC`` (cmd.exe) on Windows
    even when the detected shell is PowerShell, so the LLM's PS commands
    failed while the prompt said ``powershell``.  Run the detected shell
    explicitly so annotation and behaviour agree everywhere (native Windows,
    WSL, POSIX).
    """
    if os.name == "nt":
        from slife.platform import detect_current_shell
        if detect_current_shell() == "powershell":
            # -EncodedCommand (UTF-16LE base64) sidesteps all quoting issues
            # with arbitrary command strings.  Prepend $ProgressPreference so
            # PowerShell's "preparing module for first use" progress record is
            # not serialized as CLIXML noise on stderr when stdout/stderr are
            # pipes (no console) — it pollutes every command's stderr.
            script = "$ProgressPreference = 'SilentlyContinue'; " + command
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            return [
                "powershell", "-NoProfile", "-NonInteractive",
                "-EncodedCommand", encoded,
            ]
        return ["cmd", "/c", command]
    # POSIX (incl. WSL): $SHELL — the same value the prompt reports.
    return [os.environ.get("SHELL", "/bin/sh"), "-c", command]


def _shell_output_codec() -> str:
    """Codec for decoding shell output bytes.

    On Windows, Windows PowerShell 5.1 writes the console/OEM code page to a
    pipe (GBK/cp936 on a zh-CN locale) — decoding as UTF-8 produces mojibake.
    ``locale.getpreferredencoding(False)`` returns the right codec.  POSIX
    shells emit UTF-8.
    """
    if os.name == "nt":
        return locale.getpreferredencoding(False) or "utf-8"
    return "utf-8"


# ═══════════════════════════════════════════════════════════════════════
# execute_shell
# ═══════════════════════════════════════════════════════════════════════

class ShellTool(Tool):
    """Execute a shell command via the detected shell (PowerShell/cmd on
    Windows, $SHELL on POSIX incl. WSL) — matching what the system prompt
    reports, so PS commands like ``Get-Date`` actually work."""

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
        logger.debug("shell_exec cmd=%.200s timeout=%d", sanitize_secrets(command), timeout)

        # Run the detected shell (not COMSPEC=cmd.exe on Windows) so the
        # command executes in the same shell the system prompt reports.
        argv = _shell_argv(command)
        process = await asyncio.create_subprocess_exec(
            *argv,
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
            logger.warning("shell_timeout timeout=%ds cmd=%.200s", timeout, sanitize_secrets(command))
            return f"Error: Command timed out after {timeout}s"

        codec = _shell_output_codec()
        output = stdout.decode(codec, errors="replace")
        err_output = stderr.decode(codec, errors="replace")
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
    """Split input into (script_or_code, json_args).

    JSON args follow the script path after whitespace (``script.py {"a": 1}``).
    Only a ``{``/``[`` preceded by whitespace starts the args block — a ``[``
    *inside* the script path (``C:\\code\\my[2024]\\run.py``) is not a
    delimiter and must not split the path.
    """
    for i, ch in enumerate(input_str):
        if ch in ("{", "["):
            if i == 0 or input_str[i - 1].isspace():
                return input_str[:i].strip(), input_str[i:].strip()
    return input_str.strip(), ""


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
            # LLMs naturally write shell-style '-c "code"'.  Strip the
            # wrapping quotes, otherwise python -c gets a bare string-literal
            # expression and silently does nothing (exit 0, no output).
            if len(code) >= 2 and code[0] == code[-1] and code[0] in "\"'":
                code = code[1:-1]
            argv = [sys.executable, "-X", "utf8", "-c", code]
            logger.debug("run_python_script argv=%s", sanitize_secrets(str(argv)))
        else:
            script, args = _parse_input(input_str)
            script = _resolve_skill_script(script)
            argv = [sys.executable, "-X", "utf8", script]
            if args:
                argv.append(args)
            logger.debug("run_python_script argv=%s", sanitize_secrets(str(argv)))

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
    description = "Install PyPI packages into slife's environment via uv pip install."
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

        # The `--` separator ends uv's option parsing: a package spec that
        # begins with `-` (e.g. "--index-url https://attacker") would otherwise
        # be consumed as a uv flag and redirect the install to a hostile index.
        proc = await asyncio.create_subprocess_exec(
            "uv", "pip", "install", "--python", sys.executable, "--", *packages,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group so timeout/cancel can kill the whole tree —
            # otherwise a mid-install uv process survives as an orphan.
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            await _kill_process_tree(proc)
            logger.warning("pip_install_timeout packages=%s", packages)
            return f"Error: pip install timed out after 120s"
        except asyncio.CancelledError:
            await _kill_process_tree(proc)
            raise
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode == 0:
            logger.info("pip_install_done packages=%s", packages)
            return out or f"✓ Installed: {', '.join(packages)}"
        else:
            logger.warning("pip_install_failed packages=%s err=%s", packages, err)
            return f"Error installing {', '.join(packages)}:\n{err}" if err else f"Error installing {', '.join(packages)} (exit {proc.returncode})"


# ═══════════════════════════════════════════════════════════════════════
# run_schedule_now
# ═══════════════════════════════════════════════════════════════════════

class RunScheduleNowTool(Tool):
    """Trigger a scheduled task now (cron fire / missed backfill)."""

    name = "run_schedule_now"
    category: ClassVar[str] = "Execution"
    description = (
        "Trigger a scheduled task now — records a pending run and dispatches "
        "it immediately to the task's subagent worker (worker name = task "
        "name).  Called when a '[Schedule <name>]' trigger fires (cron) and "
        "to backfill a missed/failed run.  A backfill passes the run's due_at "
        "(the footer reminder / scheduled_run_list shows it) so THAT run "
        "transitions missed/failed → pending → ran; without due_at a fresh "
        "run is recorded at now.  The worker saves the report via "
        "save_cron_report (confirming the run) and notifies the user when done."
    )
    parameters = make_params(
        name={
            "type": "string",
            "description": "The scheduled task's name (from scheduled_task_list).",
        },
        due_at={
            "type": "string",
            "default": "",
            "description": (
                "Optional ISO due time of the exact run to trigger — pass the "
                "missed/failed run's due_at from the footer reminder or "
                "scheduled_run_list when backfilling.  Omit for a fresh "
                "cron-fire run."
            ),
        },
    )

    async def execute(self, name: str = "", due_at: str = "", **kwargs) -> str:
        if err := require_params(name=name):
            return err
        ctx = getattr(self, "_ctx", None)
        fire = getattr(ctx, "fire_schedule_now", None) if ctx is not None else None
        if fire is None:
            return (
                "Error: the scheduler is not available yet — call this after "
                "the agent service has started."
            )
        return await fire(name, due_at)
