"""Subagent — spawn local *agent worker* copies of the current agent.

Each subagent runs a slife worker process that communicates with the
parent via stdin/stdout NDJSON (one JSON object per line).  A subagent is
a local worker, not an A2A peer: it has no independent network identity,
and its tool surface lives in :mod:`slife.tools.subagent`.

Public API
----------
- ``SubagentProcess`` — manage a single subagent child process
  (:mod:`slife.subagent.process`)
- ``SubagentManager`` — manage the collection (spawn / send / stop / list)
  (:mod:`slife.subagent.process`)
- ``run_worker`` — worker entry point (no TUI, stdin/stdout IPC)
  (:mod:`slife.subagent.worker`)

The :class:`Tool` subclasses in :mod:`slife.tools.subagent` are
auto-discovered at startup and use module-level transport references.
"""

# NOTE: run_worker is NOT imported here to avoid a RuntimeWarning
# from Python's runpy when the module is executed via -m.
# When "python -m slife.subagent.worker" runs, Python first imports
# the parent package slife.subagent; if __init__.py eagerly imports
# worker, the module is already in sys.modules by the time runpy
# tries to execute it, triggering:
#   RuntimeWarning: 'slife.subagent.worker' found in sys.modules
#   after import of package 'slife.subagent', but prior to execution
# Import it directly instead: from slife.subagent.worker import run_worker
