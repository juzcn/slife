"""Platform-aware Python script runner.

Executes a Python script with JSON arguments directly — the agent gets
the script's output, not an intermediate command string that can be
accidentally mangled.
"""

import asyncio
import logging

from slife.platform import build_python_command
from slife.tools.base import Tool

logger = logging.getLogger(__name__)


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
        cmd = build_python_command(kwargs["script"])
        logger.debug("run_python_script cmd=%s", cmd)

        proc = await asyncio.create_subprocess_shell(
            cmd,
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
