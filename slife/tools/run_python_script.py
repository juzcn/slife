"""Platform-aware Python script runner.

Generates a ready-to-execute shell command for running a Python script
with JSON arguments, handling Windows quoting and encoding quirks that
LLMs consistently get wrong.
"""

from slife.platform import run_python_script
from slife.tools.base import Tool


class RunPythonScriptTool(Tool):
    """Generate a platform-correct command to run a Python script with JSON args."""

    name = "run_python_script"
    description = (
        "Build a platform-correct shell command for executing a Python "
        "script with JSON arguments. Handles OS-specific quoting, encoding, "
        "and path resolution.  The returned command MUST be passed verbatim "
        "to execute_shell — do NOT modify, shorten, or rewrite it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "Script path followed by JSON arguments, e.g. 'skills/search.py {\"query\":\"hello\"}'",
            },
        },
        "required": ["script"],
    }

    async def execute(self, **kwargs) -> str:
        return run_python_script(kwargs["script"])
