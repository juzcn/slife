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
        "Supports: run_script — run a Python script with JSON arguments; "
        "install — install a Python package via uv pip; "
        "check_installed — check if a CLI tool is on PATH; "
        "download_file — download a file via curl."
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
            "check_installed": {
                "type": "string",
                "description": "CLI name to check, e.g. 'yt-dlp', 'npx', 'docker'. Returns path if found or NOT_FOUND.",
            },
            "download_file": {
                "type": "string",
                "description": "URL to download, optionally followed by output name. E.g. 'https://example.com/file.zip' or 'https://example.com/file.zip output.zip'.",
            },
        },
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        return get_shell_command(
            run_script=kwargs.get("run_script"),
            install=kwargs.get("install"),
            check_installed=kwargs.get("check_installed"),
            download_file=kwargs.get("download_file"),
        )
