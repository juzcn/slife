"""A2A (Agent-to-Agent) — one protocol, pluggable transports.

The A2A wire + protocol is the official ``a2a-over-mqtt`` SDK (topics,
cards, JSON-RPC 2.0 over MQTT v5, task lifecycle); slife owns the harness
glue — the mesh driver (:mod:`slife.a2a.mesh`), the plugin
(:mod:`slife.plugins.a2a`), the inbox identity types, and the task store.
The LLM-facing ``a2a_*`` tools live in the a2a plugin, not here.

Subagents are **not** part of A2A — they are local workers (see
:mod:`slife.tools.subagent`).
"""
