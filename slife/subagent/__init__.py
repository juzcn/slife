"""Subagent — spawn copies of the current agent in independent processes.

Each subagent runs a headless slife instance that communicates with the
parent via stdin/stdout NDJSON (one JSON object per line).  No MQTT,
no network — just local pipes.

Public API
----------
- ``SubagentProcess`` — manage a single subagent child process
- ``SubagentManager`` — manage the collection (spawn / send / stop / list)
- ``run_headless`` — headless slife entry point (no TUI, stdin/stdout IPC)

The native :class:`Tool` subclasses in :mod:`slife.tools.a2a` are
auto-discovered at startup and use module-level transport references.
"""

from slife.subagent.headless import run_headless
from slife.subagent.process import SubagentManager, SubagentProcess
from slife.subagent.tools import SubagentSpawnTool, SubagentStopTool

__all__ = [
    "run_headless",
    "SubagentManager",
    "SubagentProcess",
    "SubagentSpawnTool",
    "SubagentStopTool",
]
