"""Mosquitto broker detection.

Slif does NOT spawn Mosquitto — the user must start it before launching
slife.  The presence of a listening broker acts as the MQTT on/off switch.
"""

from __future__ import annotations

import asyncio
import logging
import socket

import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe
from slife.threads import run_daemon

logger = logging.getLogger(__name__)


async def probe_broker(
    host: str = "localhost", port: int = 1883, timeout: float | None = None,
) -> bool:
    """Check whether a TCP listener is present on *host*:*port*.

    ``timeout`` defaults to the registry's ready.probe_broker, and it is the
    budget for EACH address the name resolves to (so a dual-stack name costs at
    most two): a broker bound to one family only must not be reported missing
    because the other family was tried first and answered slowly.  On Windows a
    dead ``::1`` refused only after ~2 s while the IPv4 listener was up, so a
    single ``open_connection`` against the FIRST resolved address spent the
    whole budget and answered False — which silently disabled A2A on a machine
    whose broker was running fine.

    Returns ``True`` if any address accepts a connection, ``False`` otherwise.
    Used at startup to decide whether to enable A2A over MQTT.
    """
    if timeout is None:
        timeout = _timeouts.timeouts.ready.probe_broker  # call-time lookup
    # Resolution goes to a daemon thread, not ``loop.getaddrinfo``.  The loop's
    # helper is ``run_in_executor(None, ...)`` — the DEFAULT executor, whose
    # non-daemon workers both shutdown paths join with ``wait=True``, so a
    # resolver that hangs would wedge interpreter exit (DESIGN.md Appendix A 30,
    # and the incident slife/threads.py documents).
    try:
        infos = await run_daemon(
            socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM,
            name="broker-resolve",
        )
    except OSError as e:
        logger.info("broker_unresolved host=%s port=%d err=%s", host, port, e)
        infos = []
    # An unresolvable name still deserves one attempt against the literal.
    attempts: list[tuple[int, str, int]] = [
        (info[0], str(info[4][0]), int(info[4][1])) for info in infos
    ] or [(0, host, port)]

    for family, address, port_ in attempts:
        try:
            kwargs = {"family": family} if family else {}
            # ``family`` pins the resolved entry: without it the name would be
            # resolved AGAIN, landing back on the slow/dead family.
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(address, port_, **kwargs),
                timeout=timeout,
            )
        except Exception:
            continue
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        logger.info("broker_found host=%s port=%d addr=%s", host, port, address)
        return True
    logger.info("broker_not_found host=%s port=%d — A2A disabled", host, port)
    return False
