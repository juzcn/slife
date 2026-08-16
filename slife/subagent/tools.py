"""Subagent tools — re-exported from Slife.tools.subagent for backward compatibility.

The canonical tool definitions live in :mod:`slife.tools.subagent` as proper
:class:`Tool` subclasses, auto-discovered by ``create_tools_from_config``
(the factory scans the ``slife.tools`` package).

This module is kept for any internal code that still imports from
``slife.subagent.tools``.  New code should import directly from
``slife.tools.subagent``.
"""

from slife.tools.subagent import (  # noqa: F401
    ListSubagentsTool,
    SpawnSubagentTool,
    StopSubagentTool,
    SubagentCancelTaskTool,
    SubagentGetTaskResultTool,
    SubagentListTasksTool,
    SubagentSendTaskAsyncTool,
    SubagentSendTaskTool,
)

__all__ = [
    "ListSubagentsTool",
    "SpawnSubagentTool",
    "StopSubagentTool",
    "SubagentCancelTaskTool",
    "SubagentGetTaskResultTool",
    "SubagentListTasksTool",
    "SubagentSendTaskAsyncTool",
    "SubagentSendTaskTool",
]
