"""Platform-aware shell command helper tool.

Exposes get_shell_command() as an LLM-callable tool that returns
ready-to-execute command strings with correct platform syntax.
"""

from slife.platform import get_shell_command
from slife.tools.base import Tool


class GetShellCommandTool(Tool):
    """Return platform-correct shell commands ready to execute."""

    name = "get_shell_command"
    description = (
        "Build a platform-correct shell command ready to paste into execute_shell. "
        "Use this to translate skill examples (which may use bash syntax) into "
        "commands that work on this OS."
    )
    parameters = {
        "type": "object",
        "properties": {
            "run_script": {
                "type": "string",
                "description": (
                    "Script path and JSON arguments to run. "
                    "Pass the script path followed by the JSON string, e.g. "
                    '"skills/search.py {\\"query\\":\\"hello\\"}". '
                    "Returns a complete command with correct Python and quoting."
                ),
            },
            "check_env": {
                "type": "string",
                "description": "Check if an environment variable is set. Returns the check command.",
            },
            "install": {
                "type": "string",
                "description": "Install a Python package. Returns the install command.",
            },
            "list_files": {
                "type": "boolean",
                "description": "Get the command to list files in a directory.",
            },
        },
        "required": [],
    }

    async def execute(
        self,
        run_script: str | None = None,
        check_env: str | None = None,
        install: str | None = None,
        list_files: bool = False,
    ) -> str:
        return get_shell_command(
            run_script=run_script,
            check_env=check_env,
            install=install,
            list_files=list_files,
        )
