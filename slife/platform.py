"""Platform detection — returns correct shell commands for the current OS.

The get_shell_command() function is the single entry point used by the
get_shell_command tool. Each parameter builds a platform-appropriate
command string that the LLM can paste directly into execute_shell.
"""

import sys

_IS_WIN = sys.platform == "win32"


def get_shell_command(
    run_script: str | None = None,
    check_env: str | None = None,
    install: str | None = None,
    list_files: bool = False,
) -> str:
    """Return platform-correct shell command(s) for the given operation(s).

    Multiple operations can be requested in a single call; results are
    joined with newlines.

    Args:
        run_script: Script path + JSON args, e.g.
            "skills/search.py {\"query\":\"hello\"}".
            Returns a complete ready-to-run command with correct quoting.
        check_env: Environment variable name to check.
        install: Python package name to install.
        list_files: Get the command to list directory contents.

    Returns:
        One or more ready-to-execute command strings.
    """
    results: list[str] = []

    if run_script is not None:
        results.append(_run_script_cmd(run_script))

    if check_env is not None:
        results.append(_check_env_cmd(check_env))

    if install is not None:
        results.append(_install_cmd(install))

    if list_files:
        results.append(_list_files_cmd())

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

    python = "python" if _IS_WIN else "python3"

    if not args:
        return f"{python} {script}"

    if _IS_WIN:
        # Force UTF-8 to avoid GBK encoding errors with Chinese output.
        # chcp sets console code page; -X utf8 forces Python to use UTF-8 for pipes/stdio.
        escaped = args.replace("\\", "\\\\").replace('"', '\\"')
        return f'chcp 65001 >nul && {python} -X utf8 {script} "{escaped}"'
    else:
        # bash: single quotes (no escaping needed for JSON)
        return f"{python} {script} '{args}'"


def _check_env_cmd(name: str) -> str:
    return f"echo %{name}%" if _IS_WIN else f"echo ${name}"


def _install_cmd(package: str) -> str:
    return f"uv pip install {package}"


def _list_files_cmd() -> str:
    return "dir" if _IS_WIN else "ls"
