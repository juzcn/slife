"""Platform detection and platform-aware utilities."""

import asyncio
import logging
import shutil
import signal
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


def run_python_script(input_str: str) -> str:
    """Build a platform-correct command to run a Python script with JSON args.

    input_str format: "<script_path> <json_args>"
    Example: "skills/search.py {\"query\":\"hello\"}"

    Returns a complete command with OS-appropriate quoting and Windows-
    specific UTF-8 workarounds (chcp 65001 + -X utf8).
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

    python = "python" if IS_WINDOWS else "python3"

    if not args:
        return f"{python} {script}"

    if IS_WINDOWS:
        # Force UTF-8 to avoid GBK encoding errors with Chinese output.
        # chcp sets console code page; -X utf8 forces Python to use UTF-8 for pipes/stdio.
        escaped = args.replace("\\", "\\\\").replace('"', '\\"')
        return f'chcp 65001 >nul && {python} -X utf8 {script} "{escaped}"'
    else:
        # bash: single quotes (no escaping needed for JSON)
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
