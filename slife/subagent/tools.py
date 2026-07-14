"""Subagent tools — re-exported from slife.tools.a2a for backward compatibility.

The canonical tool definitions live in :mod:`slife.tools.a2a` as proper
:class:`Tool` subclasses, auto-discovered by ``create_tools_from_config``.

This module is kept for any internal code that still imports from
``slife.subagent.tools``.  New code should import directly from
``slife.tools.a2a``.
"""

from slife.tools.a2a import SubagentSpawnTool, SubagentStopTool  # noqa: F401
