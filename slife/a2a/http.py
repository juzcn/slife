"""A2A HTTP Streamable transport — uses the ``mcp`` library's standard
Streamable HTTP client (:func:`mcp.client.streamable_http.streamablehttp_client`).

Topic-to-endpoint mapping::

    Slife/<agent_id>/tasks/inbox   →  POST /tasks/inbox
    Slife/<agent_id>/tasks/result  →  GET  /tasks/result (SSE)
    Slife/<agent_id>/presence      →  POST /presence
    Slife/+/presence               →  GET  /presence (SSE, wildcard)

Server-side receives requests via FastMCP's
:class:`~mcp.server.streamable_http.StreamableHTTPServerTransport`
(standard, no monkey-patch).

.. note::
   This is a **skeleton** — ``connect`` / ``disconnect`` work, but
   ``publish`` / ``subscribe`` / ``messages`` raise
   ``NotImplementedError``.  Full implementation follows when the
   A2A HTTP server side (subagent FastMCP endpoint) is built.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from slife.a2a.transport import TransportAdapter, TransportMessage

logger = logging.getLogger(__name__)

# ── Topic-to-path helpers ──────────────────────────────────────────────


def _topic_to_path(topic: str) -> str:
    """Map an A2A topic to an HTTP endpoint path.

    ``Slife/<agent_id>/tasks/inbox`` → ``/tasks/inbox``
    ``Slife/<agent_id>/tasks/result`` → ``/tasks/result``
    ``Slife/<agent_id>/presence`` → ``/presence``
    """
    parts = topic.split("/")
    # topics are: Slife / <agent_id> / <action> [/ <sub-action>]
    if len(parts) >= 3 and parts[0] == "Slife":
        return "/" + "/".join(parts[2:])
    return "/" + "/".join(parts)


# ── HttpStreamableTransport ────────────────────────────────────────────


class HttpStreamableTransport(TransportAdapter):
    """A2A transport over HTTP Streamable (``mcp`` library standard client).

    Connects to an A2A HTTP server (FastMCP-based) and maps A2A topic
    pub/sub to HTTP endpoint paths + SSE streams.

    Lifecycle::

        transport = HttpStreamableTransport("agent-1")
        await transport.connect("127.0.0.1", 8080)
        await transport.publish("Slife/agent-2/tasks/inbox", task_json)
        await transport.disconnect()
    """

    def __init__(self, agent_id: str):
        self._agent_id = agent_id
        self._session: ClientSession | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._connected = False
        self._host: str = "127.0.0.1"
        self._port: int = 0

        # topic_filter → asyncio.Queue[TransportMessage]
        self._queues: dict[str, asyncio.Queue[TransportMessage]] = {}

    # ── Connection lifecycle ──────────────────────────────────────────

    async def connect(self, host: str, port: int) -> None:
        """Connect to the A2A HTTP server via Streamable HTTP.

        Uses the standard ``mcp`` library client path:
        ``streamablehttp_client(url)`` → ``ClientSession`` → ``initialize()``.
        """
        if self._connected:
            return

        self._host = host
        self._port = port
        url = f"http://{host}:{port}/mcp"

        logger.info(
            "a2a_http_connect transport=streamable-http url=%s agent=%s",
            url, self._agent_id,
        )

        last_err = None
        attempt: int = -1
        for attempt in range(10):  # retry: server may still be starting
            try:
                self._exit_stack = AsyncExitStack()
                read_stream, write_stream, _ = (
                    await self._exit_stack.enter_async_context(
                        streamablehttp_client(url),
                    )
                )
                self._session = await self._exit_stack.enter_async_context(
                    ClientSession(read_stream, write_stream),
                )
                await asyncio.wait_for(
                    self._session.initialize(), timeout=10.0,
                )
                break
            except (
                ConnectionError,
                OSError,
                asyncio.TimeoutError,
            ) as e:
                last_err = e
                await self._cleanup()
                if attempt < 9:
                    await asyncio.sleep(0.1)
            except Exception:
                await self._cleanup()
                raise

        if not self._session:
            raise ConnectionError(
                f"Failed to connect to {url} after 10 attempts: {last_err}"
            )

        self._connected = True
        logger.info(
            "a2a_http_connected url=%s attempts=%d", url, attempt + 1,
        )

    async def disconnect(self) -> None:
        """Disconnect from the A2A HTTP server and release resources."""
        self._connected = False
        await self._cleanup()
        logger.info("a2a_http_disconnected")

    async def _cleanup(self) -> None:
        """Close the AsyncExitStack, properly exiting nested contexts."""
        if self._exit_stack:
            try:
                await self._exit_stack.aclose()
            except RuntimeError as e:
                if "cancel scope" in str(e):
                    logger.debug("cleanup_cancel_scope_suppressed err=%s", e)
                else:
                    raise
            except (Exception, BaseExceptionGroup):
                pass
            try:
                await asyncio.sleep(0)
            except Exception:
                pass
            self._exit_stack = None
        self._session = None

    # ── Pub / Sub ─────────────────────────────────────────────────────

    async def publish(
        self, topic: str, payload: str, qos: int = 1,
    ) -> None:
        """Publish *payload* to *topic* via HTTP POST.

        Maps the topic to an endpoint path and sends a JSON-RPC
        notification with the payload.

        .. note::
           Not yet implemented — raises :class:`NotImplementedError`.
           Full implementation follows when the A2A HTTP server side is built.
        """
        raise NotImplementedError(
            "HttpStreamableTransport.publish() is not yet implemented. "
            "Use MQTT transport for A2A pub/sub."
        )

    async def subscribe(self, topic: str, qos: int = 1) -> None:
        """Subscribe to *topic* (supports wildcards).

        Registers interest in a topic pattern; matching messages
        are delivered via :meth:`messages`.

        .. note::
           Not yet implemented — raises :class:`NotImplementedError`.
        """
        raise NotImplementedError(
            "HttpStreamableTransport.subscribe() is not yet implemented. "
            "Use MQTT transport for A2A pub/sub."
        )

    def messages(self, topic_filter: str) -> AsyncIterator[TransportMessage]:
        """Yield messages matching *topic_filter* as they arrive via SSE.

        .. note::
           Not yet implemented — raises :class:`NotImplementedError`.
           Will return an async generator that reads from the internal
           queue (populated by SSE events from the A2A HTTP server).
        """
        raise NotImplementedError(
            "HttpStreamableTransport.messages() is not yet implemented. "
            "Use MQTT transport for A2A pub/sub."
        )

    # ── Properties ────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected
