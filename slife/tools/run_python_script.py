"""Platform-aware Python script runner.

Executes a Python script with JSON arguments directly — the agent gets
the script's output, not an intermediate command string that can be
accidentally mangled.
"""

import asyncio
import logging
import sys

from slife.platform import build_python_command, _resolve_skill_script
from slife.tools.base import Tool

logger = logging.getLogger(__name__)


def _parse_input(input_str: str) -> tuple[str, str]:
    """Split input into (script_or_code, json_args).

    Returns (script, args) where args may be empty.
    """
    brace = input_str.find("{")
    bracket = input_str.find("[")
    candidates = [i for i in (brace, bracket) if i >= 0]
    split_at = min(candidates) if candidates else len(input_str)

    if split_at == len(input_str):
        return input_str.strip(), ""
    return input_str[:split_at].strip(), input_str[split_at:].strip()


class RunPythonScriptTool(Tool):
    """Run a Python script with JSON arguments and return its output."""

    name = "run_python_script"
    description = (
        "Run a Python script with JSON arguments. Handles OS encoding, "
        "quoting, and resolves skills/ paths to the correct install location. "
        "Returns the script's stdout, or stderr if the script fails."
    )
    parameters = {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": (
                    "Script path followed by JSON arguments, e.g. "
                    "'skills/search.py {\"query\":\"hello\"}'"
                ),
            },
        },
        "required": ["script"],
    }

    async def execute(self, **kwargs) -> str:
        input_str = kwargs["script"]

        # Parse input into script path and optional JSON args
        if input_str.startswith("-c ") or input_str.startswith("-c"):
            # -c <code> — pass code directly as argv, no shell involved
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
