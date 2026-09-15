"""Protocol-era glue for every MCP link slife owns.

The 2026-07-28 revision changed two things that reach every one of our
links (host ↔ plugin child, subagent ↔ plugin, gateway ↔ external server),
so both live here instead of being re-derived per call site:

**Connect-time negotiation.**  ``ClientSession.initialize()`` speaks the
legacy handshake ONLY — against a modern peer it either fails or locks the
connection to a pre-2026 era.  :func:`negotiate_era` drives the SDK's own
``mode="auto"`` policy: probe ``server/discover``, adopt a mutual modern
version when the peer answers as modern, fall back to the handshake when it
answers as legacy, and let transport errors propagate (an outage is never
an era verdict).  The peer's era is therefore DISCOVERED, never configured
— a mixed fleet needs no per-server switch.

**Change notifications.**  The modern era forbids pushing a notification a
client did not ask for, and the SDK drops a bare
``ServerSession.send_tool_list_changed()`` at that era.  A change event
reaches a modern client only through a ``subscriptions/listen`` stream it
opened, so :func:`watch_tools_changed` keeps such a stream open and calls
the link's existing notification handler per event — the handler is
unchanged, only its trigger moves from the session channel to the listen
stream.  A legacy peer raises ``ListenNotSupportedError`` on the first
attempt; its events ride the session as before.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable

import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

logger = logging.getLogger(__name__)


async def negotiate_era(session: Any) -> str | None:
    """Adopt *session*'s protocol era; returns the negotiated version.

    Legacy peer → the ``initialize`` handshake exactly as before.  Modern
    peer → ``server/discover`` + ``adopt``, which is what makes the link
    stateless: no session id, no handshake, per-request ``_meta``.

    Raises whatever the transport raised when the peer was simply
    unreachable — the caller's connect-retry loop owns that case, and a
    network blip must never be recorded as an era.
    """
    try:
        from mcp.client._probe import negotiate_auto
    except ImportError:  # pragma: no cover — an SDK without the era policy
        await session.initialize()
        return getattr(session, "protocol_version", None)
    await negotiate_auto(session)
    return getattr(session, "protocol_version", None)


def peer_era(session: Any) -> str:
    """``"modern"`` / ``"legacy"`` / ``"unknown"`` — for logs and health text.

    ``discover_result`` is set iff the era was adopted through
    ``server/discover``; ``initialize_result`` iff through the handshake.
    """
    if getattr(session, "discover_result", None) is not None:
        return "modern"
    if getattr(session, "initialize_result", None) is not None:
        return "legacy"
    return "unknown"


async def watch_tools_changed(
    session: Any,
    handler: Callable[[], Awaitable[None] | None],
) -> None:
    """Keep a ``tools/list_changed`` listen stream open; call *handler* per event.

    Runs until cancelled (the link disconnects).  A legacy peer raises
    ``ListenNotSupportedError`` on the first attempt — its notifications
    ride the session channel, so this returns quietly and the caller's
    ``message_handler`` path stays the trigger.  Every other failure
    (``SubscriptionLost`` on an abrupt drop, a rejected listen, a transport
    that died) re-listens after ``timeouts.ready.relisten``.

    The event is a bare level trigger: a listener re-reads the tool list
    rather than trusting a payload, so a missed-during-reconnect change is
    harmless (the next event, or the reconcile that the reconnect itself
    triggers, covers it).
    """
    from mcp.client.subscriptions import ListenNotSupportedError, listen

    while True:
        try:
            async with listen(session, tools_list_changed=True) as subscription:
                async for _event in subscription:
                    result = handler()
                    if inspect.isawaitable(result):
                        await result
        except ListenNotSupportedError:
            logger.debug("mcp_listen_unsupported era=%s", peer_era(session))
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("mcp_listen_stream_lost err=%s", e)
        await asyncio.sleep(_timeouts.timeouts.ready.relisten)
