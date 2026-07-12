"""OS info tool — returns the current operating system name."""

from slife.platform import get_os_info
from slife.tools.base import Tool


class GetOsInfoTool(Tool):
    """Return the current operating system name."""

    name = "get_os_info"
    description = "Return the current operating system: 'Windows', 'Linux', or 'macOS'."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        return get_os_info()
