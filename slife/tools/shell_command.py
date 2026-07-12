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
        "Get the correct shell command for the current operating system. "
        "Always call this before execute_shell — never guess a command directly, "
        "because syntax differs between Windows (cmd) and Unix (bash). "
        "Supports: "
        "run_script — run a Python script with JSON arguments; "
        "install — install a Python package via uv pip."
    )
    parameters = {
        "type": "object",
        "properties": {
            "run_script": {
                "type": "string",
                "description": "Script path + JSON args, e.g. 'skills/search.py {\"query\":\"hello\"}'",
            },
            "install": {
                "type": "string",
                "description": "Package name to install via uv pip",
            },
        },
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        return get_shell_command(
            run_script=kwargs.get("run_script"),
            install=kwargs.get("install"),
        )
