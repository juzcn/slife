"""Platform detection and platform-aware utilities."""

import shutil
import sys
import platform as _platform

IS_WINDOWS = sys.platform == "win32"


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
