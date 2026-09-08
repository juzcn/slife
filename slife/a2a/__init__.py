"""A2A (Agent-to-Agent) — one protocol, pluggable transports.

The A2A protocol operations and data model (mirroring the official
a2a-python reference interface) with a custom transport binding (MQTT).
The LLM-facing ``a2a_*`` tools live in the a2a plugin
(:mod:`slife.plugins.a2a`), not here.

Subagents are **not** part of A2A — they are local workers (see
:mod:`slife.tools.subagent`).

This ``__init__`` is import-light (F1): the heavy protocol modules
(client → mqtt → paho) are only imported when actually referenced, so
``from slife.a2a.config import A2AConfig`` inside ``slife.config`` never
pays for paho.
"""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only view of the lazy exports below: pyright (and editors) resolve
    # ``from slife.a2a import A2AClient`` etc. against these without importing
    # paho at runtime.  The runtime path stays ``__getattr__`` + ``_LAZY_EXPORTS``.
    from slife.a2a.card import AgentCard
    from slife.a2a.client import A2AClient
    from slife.a2a.config import A2AConfig
    from slife.a2a.identity import AgentMessage, AgentName, HUMAN
    from slife.a2a.mqtt import MQTTAdapter
    from slife.a2a.transport import TransportAdapter, TransportMessage

__all__ = [
    "A2AClient",
    "A2AConfig",
    "AgentCard",
    "AgentName",
    "AgentMessage",
    "HUMAN",
    "MQTTAdapter",
    "TransportAdapter",
    "TransportMessage",
]

#: ``pubname → (module, attr)`` — resolved on first access via __getattr__
#: so the package keeps its re-export surface without importing paho at load.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "A2AClient": ("slife.a2a.client", "A2AClient"),
    "A2AConfig": ("slife.a2a.config", "A2AConfig"),
    "AgentCard": ("slife.a2a.card", "AgentCard"),
    "AgentName": ("slife.a2a.identity", "AgentName"),
    "AgentMessage": ("slife.a2a.identity", "AgentMessage"),
    "HUMAN": ("slife.a2a.identity", "HUMAN"),
    "MQTTAdapter": ("slife.a2a.mqtt", "MQTTAdapter"),
    "TransportAdapter": ("slife.a2a.transport", "TransportAdapter"),
    "TransportMessage": ("slife.a2a.transport", "TransportMessage"),
}


def __getattr__(name: str):
    entry = _LAZY_EXPORTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = entry
    value = getattr(import_module(module_name), attr)
    globals()[name] = value
    return value
