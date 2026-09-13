"""A2A (Agent-to-Agent) — one protocol, pluggable transports.

The A2A wire + protocol is the official ``a2a-over-mqtt`` SDK (topics,
cards, JSON-RPC 2.0 over MQTT v5, task lifecycle); slife owns the harness
glue — the mesh driver (:mod:`slife.a2a.mesh`), the plugin
(:mod:`slife.plugins.a2a`), the inbox identity types, and the task store.
The LLM-facing ``a2a_*`` tools live in the a2a plugin, not here.

Subagents are **not** part of A2A — they are local workers (see
:mod:`slife.tools.subagent`).

This ``__init__`` is import-light (F1): the heavy transport modules are
only imported when actually referenced, so ``from slife.a2a.config import
A2AConfig`` inside ``slife.config`` never pays for aiomqtt/paho.
"""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only view of the lazy exports below: pyright (and editors) resolve
    # ``from slife.a2a import A2AConfig`` etc. against these without importing
    # the heavy transport at runtime.  The runtime path stays ``__getattr__``
    # + ``_LAZY_EXPORTS``.
    from slife.a2a.card import AgentCard
    from slife.a2a.config import A2AConfig
    from slife.a2a.identity import AgentMessage, AgentName, HUMAN

__all__ = [
    "A2AConfig",
    "AgentCard",
    "AgentName",
    "AgentMessage",
    "HUMAN",
]

#: ``pubname → (module, attr)`` — resolved on first access via __getattr__
#: so the package keeps its re-export surface without importing aiomqtt at load.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "A2AConfig": ("slife.a2a.config", "A2AConfig"),
    "AgentCard": ("slife.a2a.card", "AgentCard"),
    "AgentName": ("slife.a2a.identity", "AgentName"),
    "AgentMessage": ("slife.a2a.identity", "AgentMessage"),
    "HUMAN": ("slife.a2a.identity", "HUMAN"),
}


def __getattr__(name: str):
    entry = _LAZY_EXPORTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = entry
    value = getattr(import_module(module_name), attr)
    globals()[name] = value
    return value