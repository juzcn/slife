"""Code execution tools.

Tools:
    execute_shell          — run shell commands (disabled by default)
    run_python_script      — run Python scripts with JSON arguments
    install_python_package — install PyPI packages into slife's environment
"""

import asyncio
import logging
import sys

from slife.platform import _resolve_skill_script
from slife.tools.base import Tool

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# execute_shell
# ═══════════════════════════════════════════════════════════════════════

class ShellTool(Tool):
    """Execute a shell command via the system shell (cmd on Windows, sh on Unix)."""

    name = "execute_shell"
    description = (
        "Run a shell command and return stdout and stderr. "
        "Uses the system shell: cmd.exe on Windows, /bin/sh on Unix. "
        "Long-running commands should set a timeout; default is 30 seconds. "
        "For Python scripts prefer run_python_script."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute, exactly as you would type it in a terminal.",
            },
            "timeout": {
                "type": "integer",
                "description": "Max seconds to wait for the command to finish. Default 30. Set higher for long tasks.",
            },
        },
        "required": ["command"],
    }

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg, config):
        return cls(timeout=cfg.get("timeout", 30))

    async def execute(self, **kwargs) -> str:
        command: str = kwargs["command"]
        timeout: int = kwargs.get("timeout", self.timeout)
        logger.debug("shell_exec cmd=%.200s timeout=%d", command, timeout)

        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
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
    description = (
        "Run a Python script file or inline code. "
        "For a script, pass the path followed by a JSON argument string: "
        "'path/to/script.py {\"key\": \"value\"}'. "
        "For inline code, prefix with '-c ': '-c print(1+1)'. "
        "Returns stdout on success, or stderr with exit code on failure."
    )
    parameters = {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": (
                    "Script path with optional JSON args, e.g. 'skills/search.py {\"query\":\"hello\"}'. "
                    "Or inline code with '-c <python code>'."
                ),
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
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
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
    description = (
        "Install one or more PyPI packages into slife's Python environment "
        "using uv pip install. Use when a script fails with ModuleNotFoundError. "
        "Supports version pins like 'requests>=2.31'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "packages": {
                "type": "array",
                "description": "Package names to install, e.g. ['requests'] or ['requests', 'beautifulsoup4']. Each may include version pins.",
                "items": {"type": "string"},
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
