"""Shell command execution tool."""

import asyncio

from slife.tools.base import Tool


class ShellTool(Tool):
    """Execute shell commands on the user's system."""

    name = "execute_shell"
    description = (
        "Execute a shell command on the user's system. "
        "Returns stdout and stderr combined. "
        "Use this to run commands, read files, list directories, "
        "or interact with the system. "
        "On Windows, commands run in cmd.exe; on Unix, in sh."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute",
            },
        },
        "required": ["command"],
    }

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    async def execute(self, command: str) -> str:
        """Execute a shell command and return its output."""
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return f"Error: Command timed out after {self.timeout}s"

        output = stdout.decode("utf-8", errors="replace")
        err_output = stderr.decode("utf-8", errors="replace")

        result = output
        if err_output:
            result += f"\n[stderr]\n{err_output}"

        if not result.strip():
            result = (
                f"Command completed with exit code {process.returncode} "
                "(no output)"
            )

        return result
