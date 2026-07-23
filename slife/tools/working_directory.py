"""get_working_directory — return the current working directory path."""

import os

from slife.tools.base import Tool


class GetWorkingDirectoryTool(Tool):
    """Return the absolute path of the current working directory."""

    name = "get_working_directory"
    description = (
        "Return the absolute path of the current working directory (CWD). "
        "Use this to determine where slife is running — far more reliable "
        "than running a shell command like pwd or cd."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        return os.getcwd()
