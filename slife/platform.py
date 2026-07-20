"""Platform detection and platform-aware utilities."""

import asyncio
import logging
import shutil
import signal
import subprocess as _subprocess
import sys
import platform as _platform

IS_WINDOWS = sys.platform == "win32"

logger = logging.getLogger(__name__)


def resolve_command(command: str) -> str:
    """Resolve a command name to its full path on Windows.

    On Windows, appends .cmd/.exe extensions if needed and resolves
    via shutil.which(). On other platforms, returns the command as-is.
    """
    if IS_WINDOWS and not command.lower().endswith((".exe", ".cmd", ".bat")):
        resolved = shutil.which(command) or shutil.which(command + ".cmd") or shutil.which(command + ".exe")
        if resolved:
            return resolved
    return command


def get_os_info() -> str:
    """Return a human-readable OS identifier.

    Returns one of: "Windows", "Linux", "macOS".
    """
    system = _platform.system()
    if system == "Darwin":
        return "macOS"
    if system == "Windows":
        return "Windows"
    if system == "Linux":
        return "Linux"
    return system  # Fallback for other platforms (e.g. "FreeBSD")


def _resolve_skill_script(script_path: str) -> str:
    """Resolve a ``skills/…`` path to the actual install location.

    Skills ship inside the slife package (``slife/skills/``) in production
    and in the project root in dev mode.  Returns the absolute path if the
    file exists; otherwise returns the original path unchanged.
    """
    from slife.paths import get_skills_dir

    if script_path.startswith(("skills/", "skills\\")):
        skills_dir = get_skills_dir()
        rel = script_path[len("skills/"):].lstrip("/\\") if script_path.startswith("skills/") else script_path[len("skills\\"):].lstrip("/\\")
        resolved = skills_dir / rel
        if resolved.is_file():
            return str(resolved)
    return script_path


def build_python_command(input_str: str) -> str:
    """Build a platform-correct command to run a Python script with JSON args.

    input_str format: "<script_path> <json_args>"
    Example: "skills/search.py {\"query\":\"hello\"}"

    Returns a complete command with OS-appropriate quoting and UTF-8 handling.
    """
    # Split on first { or [ to separate script path from JSON args
    brace = input_str.find("{")
    bracket = input_str.find("[")
    candidates = [i for i in (brace, bracket) if i >= 0]
    split_at = min(candidates) if candidates else len(input_str)

    if split_at == len(input_str):
        script = input_str.strip()
        args = ""
    else:
        script = input_str[:split_at].strip()
        args = input_str[split_at:].strip()

    # Resolve skills/ paths to the installed package location.
    script = _resolve_skill_script(script)

    # Use sys.executable on all platforms — the exact Python that is
    # running slife.  On Windows this avoids the MS Store app alias
    # ("python") and version mismatches from "py".  On macOS / Linux
    # it avoids missing-python3 issues when Python was installed
    # via uv (which adds python3.13 but may not create a python3
    # symlink, especially in CI).
    python = sys.executable

    if not args:
        return f"{python} {script}"

    if IS_WINDOWS:
        # -X utf8 forces Python to use UTF-8 for pipes/stdio — sufficient
        # to avoid GBK encoding errors.  No chcp prefix needed (it only
        # tempts the LLM to "simplify" the command by stripping the prefix).
        escaped = args.replace('"', '\\"')
        return f'{python} -X utf8 {script} "{escaped}"'
    else:
        return f"{python} {script} '{args}'"


async def terminate_process(
    process: asyncio.subprocess.Process,
    *,
    graceful_timeout: float = 3.0,
    force_timeout: float = 5.0,
    label: str = "",
) -> None:
    """Gracefully terminate an asyncio subprocess with escalating force.

    1. Close stdin to signal EOF.
    2. Send SIGTERM / ``terminate()``.
    3. Wait *graceful_timeout* seconds for graceful exit.
    4. Force-kill if still running.
    5. Wait *force_timeout* seconds for kill to take effect.

    Swallows ``ProcessLookupError`` (already exited) and logs otherwise.
    """
    if process is None:
        return
    try:
        if process.returncode is None:
            # Close stdin first to signal the process
            if process.stdin:
                try:
                    process.stdin.close()
                except Exception:
                    pass

            # Graceful termination
            if IS_WINDOWS:
                process.terminate()
            else:
                process.send_signal(signal.SIGTERM)

            # Wait for graceful exit
            try:
                await asyncio.wait_for(process.wait(), timeout=graceful_timeout)
                logger.debug("process_exited pid=%s label=%s", process.pid, label)
            except asyncio.TimeoutError:
                logger.warning("process_force_kill pid=%s label=%s", process.pid, label)
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=force_timeout)
                except asyncio.TimeoutError:
                    pass  # Best effort
    except ProcessLookupError:
        pass  # Already exited
    except Exception as e:
        logger.debug("process_terminate_error label=%s err=%s", label, e)


def desktop_notify(title: str, message: str) -> None:
    """Fire a best-effort desktop notification (cross-platform).

    Uses native platform facilities — never raises, never blocks the
    caller on failure.
    """
    system = _platform.system()
    try:
        if system == "Windows":
            _subprocess.run(
                ["powershell", "-Command",
                 f"Add-Type -AssemblyName System.Windows.Forms; "
                 f"$n = New-Object System.Windows.Forms.NotifyIcon; "
                 f"$n.Icon = [System.Drawing.SystemIcons]::Information; "
                 f"$n.BalloonTipTitle = '{title}'; "
                 f"$n.BalloonTipText = '{message}'; "
                 f"$n.Visible = $true; "
                 f"$n.ShowBalloonTip(5000);"],
                capture_output=True, timeout=10,
            )
        elif system == "Darwin":
            _subprocess.run(
                ["osascript", "-e",
                 f'display notification "{message}" with title "{title}"'],
                capture_output=True, timeout=5,
            )
        else:
            _subprocess.run(
                ["notify-send", title, message],
                capture_output=True, timeout=5,
            )
    except Exception:
        # Desktop notification is best-effort — never let it fail the caller
        pass
