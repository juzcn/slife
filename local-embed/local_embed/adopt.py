"""Adopting an already-running local-embed, and taking the port back if it dies.

local-embed binds a **fixed** port because the host's embeddings ``base_url`` is
static config — it points at the port, not at whatever port this process
happens to serve.  That makes the port the service's identity: whoever holds it
*is* local-embed, as far as every host is concerned.

Which means a second local-embed is not always a mistake.  When one instance
already serves the port (a daemon in WSL shared by several hosts, or simply one
started by hand), the host's ``base_url`` is already being served — by a process
that has arguably the better claim, since it may be warm.  Refusing to start
there reports a working system as broken, and its only remedy ("stop the other
one") throws away a running service to start an identical cold one.

So the spawned child probes first and, finding a healthy local-embed already on
the port, **adopts** it: it serves MCP on an OS-assigned port, loads no model of
its own, and reports the adopted service's state.  The fixed port keeps being
served by whoever was already serving it.

What it will not adopt is a *stranger*: a port held by something that is not a
local-embed is still a hard error, because adopting it would point nothing at
anything and hide the real conflict (see :func:`probe_service`).

Since the child sits there anyway, it also watches: if the adopted service goes
away, the child takes the fixed port over and serves it itself, so hosts keep
embedding without a restart.  Note the inverse is not possible — once the child
holds the port, the original service cannot come back until the child restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: How long to wait for the adopted service's reply.  Generous for a loopback
#: GET, but still far inside the host's port-signal budget: the probe runs
#: before the child signals readiness, and a probe that hangs would delay the
#: signal for its whole timeout.
_PROBE_TIMEOUT = 2.0

#: A loopback probe must never go through a proxy.  ``urlopen`` honours
#: ``$http_proxy`` and friends, and on Windows ``getproxies()`` also reads the
#: WinINET registry — so on a machine with a proxy configured, a request to
#: 127.0.0.1 would be routed off-box and never reach our own service, silently
#: turning adoption into a permanent miss.  An opener with an empty
#: ProxyHandler bypasses both sources.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: How often the adopter re-checks whether the fixed port has come free.
#: The port only frees when a service dies, which is not a fast-moving event —
#: and each tick is one failed connect, so the cost of being slow to notice is
#: a few seconds of embeddings being down.
_WATCH_INTERVAL = 5.0


@dataclass(frozen=True)
class Adoption:
    """The external local-embed this process is standing behind."""

    host: str
    port: int
    models: tuple[str, ...]

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}"


def probe_service(host: str, port: int, *, timeout: float = _PROBE_TIMEOUT) -> dict | None:
    """``GET /health`` on *host*:*port*; the payload if a local-embed answers.

    Returns ``None`` for everything that is not a local-embed — nothing
    listening, a timeout, a non-JSON body, or a JSON body that is not shaped
    like ours.  The shape check is the point: it is what separates "another
    local-embed is already serving the hosts' base_url" (adopt it) from "some
    unrelated service happens to hold this port" (still a hard error — adopting
    it would silently point the embeddings config at a stranger).

    Deliberately stdlib: this package has no HTTP client, and one loopback GET
    is not a reason to grow a dependency.
    """
    url = f"http://{host}:{port}/health"
    try:
        with _OPENER.open(url, timeout=timeout) as resp:  # noqa: S310 — fixed loopback http
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.debug("local_embed_probe_miss port=%s err=%s", port, e)
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        logger.warning("local_embed_probe_not_ours port=%s", port)
        return None
    return payload


# ── Module-level adoption state ─────────────────────────────────────────
#
# Mirrors the engine singleton: one process, one adoption, set by ``main()``
# before the server starts and read by the lifespan and ``__check``.

_adoption: Adoption | None = None


def set_adoption(adoption: Adoption | None) -> None:
    """Record the adopted external service (``None`` = we own the port)."""
    global _adoption
    _adoption = adoption


def get_adoption() -> Adoption | None:
    """The adopted external service, or ``None`` when this process owns the
    fixed port itself."""
    return _adoption


def adopted_check_payload(timeout: float = _PROBE_TIMEOUT) -> dict:
    """``__check``'s payload while adopted — the external service's own facts.

    Re-read live on every probe rather than cached at adoption: ``__check`` is
    a health probe, and a snapshot from startup would report a service that has
    since died as healthy.  Raises when the service cannot be reached, which
    the host's ``_probe_plugin`` renders as ``unavailable`` with the reason.
    """
    adoption = _adoption
    if adoption is None:  # pragma: no cover — caller checks first
        raise RuntimeError("not adopted")
    payload = probe_service(adoption.host, adoption.port, timeout=timeout)
    if payload is None:
        raise RuntimeError(
            f"adopted local-embed at {adoption.endpoint} is not answering "
            "/health"
        )
    return {
        "adopted": True,
        "endpoint": adoption.endpoint,
        "models": payload.get("models", []),
    }


# ── Taking the fixed port over ──────────────────────────────────────────


async def _forward(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                   target_port: int) -> None:
    """Pipe one accepted connection to this process's own server.

    The adopter is already serving the full app (``/v1/embeddings``,
    ``/v1/models``, ``/health``) on its MCP port; the fixed port just needs to
    reach it.  A byte pipe reuses all of that as-is — a second uvicorn on the
    fixed port would run the app's lifespan a second time and share one FastMCP
    session manager across two servers, for no gain.
    """
    try:
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", target_port)
    except OSError:
        writer.close()
        return

    async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while chunk := await src.read(65536):
                dst.write(chunk)
                await dst.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                dst.close()
            except OSError:
                pass

    await asyncio.gather(
        pipe(reader, up_writer), pipe(up_reader, writer),
        return_exceptions=True,
    )


async def watch_and_take_over(
    host: str, port: int, target_port: int, engine, *,
    interval: float = _WATCH_INTERVAL,
) -> None:
    """Hold the fixed port once the adopted service lets go of it.

    Runs for the life of the process.  Each tick asks :func:`bind_port` for the
    port — the same probe-then-bind the owner path uses, so a port that is still
    served keeps raising and a port that has come free is bound atomically.

    On success the adopted state is cleared (this process now serves the port,
    so it is no longer standing behind anyone), the models flagged
    ``autoload`` are warmed, and the pipe is served until shutdown.
    """
    from local_embed.server_utils import bind_port

    while True:
        await asyncio.sleep(interval)
        try:
            sock, _ = bind_port(host, port)
        except RuntimeError:
            continue  # still served — keep watching
        except OSError as e:  # pragma: no cover — bind_port wraps these
            logger.warning("local_embed_takeover_bind_failed err=%s", e)
            continue

        logger.info("local_embed_took_over port=%s -> %s", port, target_port)
        set_adoption(None)
        if engine is not None:
            try:
                await engine.load_autoload()
            except Exception as e:  # noqa: BLE001 — a warm-up must not kill the server
                logger.warning("autoload_failed models=%s err=%s", engine.models, e)
        try:
            server = await asyncio.start_server(
                lambda r, w: _forward(r, w, target_port),
                sock=sock,
            )
        except OSError as e:  # pragma: no cover — socket is already bound
            logger.error("local_embed_takeover_serve_failed err=%s", e)
            sock.close()
            return
        async with server:
            await server.serve_forever()
