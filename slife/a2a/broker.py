"""Mosquitto broker detection.

Slif does NOT spawn Mosquitto — the user must start it before launching
slife.  The presence of a listening broker acts as the MQTT on/off switch.
"""

from __future__ import annotations

import asyncio
import logging

import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

logger = logging.getLogger(__name__)


async def probe_broker(
    host: str = "localhost", port: int = 1883, timeout: float | None = None,
) -> bool:
    """Check whether a TCP listener is present on *host*:*port*.

    ``timeout`` defaults to the registry's ready.probe_broker.
    Returns ``True`` if the connection succeeds, ``False`` otherwise.
    Used at startup to decide whether to enable A2A over MQTT.
    """
    if timeout is None:
        timeout = _timeouts.timeouts.ready.probe_broker  # call-time lookup
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        logger.info("broker_found host=%s port=%d", host, port)
        return True
    except Exception:
        logger.info("broker_not_found host=%s port=%d — A2A disabled", host, port)
        return False
