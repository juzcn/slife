"""Platform detection — returns correct shell commands for the current OS.

The get_shell_command() function is the single entry point used by the
get_shell_command tool. Each parameter builds a platform-appropriate
command string that the LLM can paste directly into execute_shell.
"""

import sys

IS_WINDOWS = sys.platform == "win32"


def get_shell_command(
    run_script: str | None = None,
    install: str | None = None,
    check_installed: str | None = None,
    download_file: str | None = None,
) -> str:
    """Return platform-correct shell command(s) for the given operation(s).

    Multiple operations can be requested in a single call; results are
    joined with newlines.

    Args:
        run_script: Script path + JSON args, e.g.
            "skills/search.py {\"query\":\"hello\"}".
            Returns a complete ready-to-run command with correct quoting.
        install: Python package name to install.
        check_installed: CLI name to check (e.g. "yt-dlp", "npx").
            Returns a command that prints the path if found, or NOT_FOUND.
        download_file: URL to download, optionally followed by output name.
            E.g. "https://example.com/file.zip" or
            "https://example.com/file.zip output.zip".

    Returns:
        One or more ready-to-execute command strings.
    """
    results: list[str] = []

    if run_script is not None:
        results.append(_run_script_cmd(run_script))

    if install is not None:
        results.append(_install_cmd(install))

    if check_installed is not None:
        results.append(_check_installed_cmd(check_installed))

    if download_file is not None:
        results.append(_download_cmd(download_file))

    return "\n".join(results) if results else "No action specified."


def _run_script_cmd(input_str: str) -> str:
    """Build a platform-correct command to run a Python script.

    input_str format: "<script_path> <json_args>"
    Example: "skills/search.py {\"query\":\"hello\"}"

    Returns a complete command with OS-appropriate quoting.
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



def _install_cmd(package: str) -> str:
    return f"uv pip install {package}"


def _download_cmd(input_str: str) -> str:
    """Build a file download command using curl.

    input_str format: "<url>" or "<url> <output_name>"

    curl is bundled with Windows 10+ and all Unix systems — a single
    command avoids the LLM guessing winget/choco/apt/wget/etc.
    -L follows redirects.
    """
    parts = input_str.strip().split(maxsplit=1)
    url = parts[0]
    output = parts[1] if len(parts) > 1 else ""

    if output:
        return f'curl -L -o "{output}" "{url}"'
    else:
        return f'curl -L -O "{url}"'


def _check_installed_cmd(name: str) -> str:
    """Build a platform-correct command to check if a CLI is installed.

    Returns a command that prints the tool's path if found on PATH,
    or "NOT_FOUND" if not. Works for any CLI executable.

    On Windows, where.exe is the native equivalent of Unix which.
    On Unix, command -v is preferred over which (it's a shell builtin,
    so it works even if which is not installed).
    """
    if IS_WINDOWS:
        # where.exe: prints all matches on PATH, or returns error with no output
        # 2>nul suppresses stderr; || runs if the previous command failed
        return f'where {name} 2>nul || echo NOT_FOUND'
    else:
        # command -v: POSIX-compliant, prints path or returns non-zero
        return f'command -v {name} || echo NOT_FOUND'
