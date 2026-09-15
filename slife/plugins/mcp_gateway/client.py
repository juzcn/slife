"""MCP client — connects to MCP servers via Streamable HTTP transport.

Uses ``mcp.client.streamable_http.streamable_http_client`` for the
transport layer and ``mcp.ClientSession`` for the MCP protocol,
managed via ``contextlib.AsyncExitStack`` for correct async-context
nesting.
"""

import asyncio
import logging
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx2

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

from slife.mcp.era import negotiate_era, peer_era, watch_tools_changed
from slife.plugins.mcp_gateway import __version__
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe


async def close_exit_stack_bounded(
    stack: AsyncExitStack, *, label: str = "cleanup",
) -> None:
    """Close an ``AsyncExitStack`` with a grace timeout and the SDK teardown swallow.

    The ``streamable_http_client`` async generator from the MCP library uses
    ``anyio.create_task_group()`` internally; when the connection fails during
    setup its cancel-scope cleanup can raise ``BaseExceptionGroup`` or
    ``RuntimeError`` (task mismatch) that escape a bare ``except Exception``.
    A request hung against a not-yet-ready server can also keep ``aclose``
    from returning promptly, so the teardown is bounded — an abandoned stack
    is reclaimed by GC and the caller builds a fresh one.  The three-teardown
    copies (client cleanup + the connection's reconnect paths) shared this
    block before it lived here.
    """
    try:
        await asyncio.wait_for(
            stack.aclose(),
            timeout=_timeouts.timeouts.grace.cleanup,
        )
    except asyncio.TimeoutError:
        logger.debug("%s_aclose_timeout abandoning stack", label)
    except RuntimeError as e:
        if "cancel scope" in str(e):
            logger.debug("%s_cancel_scope_suppressed err=%s", label, e)
        else:
            raise
    except (Exception, BaseExceptionGroup):
        pass


def make_local_http_client(headers: dict | None = None) -> httpx2.AsyncClient:
    """Build the gateway's httpx2 client — the shared timeout tuple, proxy-free.

    Local plugin servers only — never route localhost through the OS proxy
    (a Windows system proxy like 127.0.0.1:7890 would 502 the local MCP
    session), so ``trust_env=False`` keeps the client proxy-free.  Read/write
    timeouts are DELEGATED — the loop's tool budget owns the read bound (both
    are None); only connect/pool are bounded so a dead endpoint can't hang
    the handshake.  ``headers`` ride the session (external server auth, etc.).
    """
    return httpx2.AsyncClient(
        trust_env=False,
        timeout=httpx2.Timeout(
            connect=_timeouts.timeouts.transport.connect,
            # read/write are DELEGATED — the loop's tool budget owns the read
            # bound (both are None).
            read=_timeouts.timeouts.transport.read,
            write=_timeouts.timeouts.transport.write,
            pool=_timeouts.timeouts.transport.pool,
        ),
        headers=headers,
    )

logger = logging.getLogger(__name__)

# True once the loop-level cancel-scope exception handler is installed.  A
# process may create many MCPClient instances (one per plugin + subagents);
# the handler is installed once per process and reused by all of them.
_cancel_scope_handler_installed: bool = False


def _install_cancel_scope_exception_handler() -> None:
    """Demote the MCP SDK's 'cancel scope' RuntimeError to a debug log.

    When a request dies mid-flight against a killed server, the SDK's
    ``streamable_http_client`` tears its anyio TaskGroup down from a
    different task than the one that entered it, raising::

        RuntimeError: Attempted to exit cancel scope in a different task
        than it was entered in

    That task's exception is never retrieved (it fires after the caller has
    already moved on), so it surfaces via the loop's exception handler as
    ``Task exception was never retrieved`` — not catchable at any call site.
    :meth:`_cleanup` already suppresses the synchronous variant; this
    handler covers the async one.  Installed once, chained to any existing
    handler.
    """
    global _cancel_scope_handler_installed
    if _cancel_scope_handler_installed:
        return
    _cancel_scope_handler_installed = True
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def _handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        msg = str(context.get("message", ""))
        if (
            (isinstance(exc, RuntimeError) and "cancel scope" in str(exc))
            or "cancel scope" in msg
        ):
            logger.debug(
                "asyncio_cancel_scope_suppressed err=%s", exc or msg,
            )
            return
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


# Bounds one full connect attempt — transport setup (the SDK's
# streamable_http_client context) AND the initialize handshake.  Previously
# only initialize() was wrapped, so a hang in transport setup (e.g. memfiles'
# eager ngrok tunnel delaying the app past the port signal) left the spawn
# pending forever.
# Per-attempt connect bound is developer-owned (registry ready.connect_attempt).
# Max time to wait for the SDK transport to tear down after a failed attempt.
# A request hung against a not-yet-ready server may keep aclose() from
# returning promptly; the connect retry must progress rather than block on it.
# (registry grace.cleanup).
# Retry window: server prints port signal BEFORE uvicorn starts listening
# (and memfiles' eager ngrok tunnel can delay readiness by another ~2s), so
# the client may need a few attempts before the socket accepts and responds.
# WSL's slower plugin lifespans make the window wider still — a plugin whose
# startup takes ~10s (e.g. mcp auto-connecting a slow catalog) needs the band
# to outlast it, bounded above by the spawn guard in the harness (a count,
# the registry's ready.connect_retry_delay governs the band).
_CONNECT_RETRY_ATTEMPTS = 20  # up to ~10 s of slow-start plugins (WSL)


# ── Binary → temp file helper ──────────────────────────────────────

# Image file magic bytes for format detection
_IMAGE_MAGIC: dict[bytes, str] = {
    b"\x89PNG\r\n\x1a\n": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"GIF87a": ".gif",
    b"GIF89a": ".gif",
    b"RIFF": ".webp",       # RIFF....WEBP — checked separately below
    b"BM": ".bmp",
}


def _guess_image_extension(data: bytes) -> str | None:
    """Detect image format from magic bytes, returning e.g. ``".png"``."""
    for magic, ext in _IMAGE_MAGIC.items():
        if data[:len(magic)] == magic:
            if ext == ".webp" and data[8:12] != b"WEBP":
                continue
            return ext
    return None


def _is_retryable_connect_error(exc: BaseException) -> bool:
    """True if *exc* is a transient connection/transport failure worth retrying.

    The MCP SDK surfaces connection failures either directly or wrapped in an
    ``ExceptionGroup``/``BaseExceptionGroup`` during task-group teardown, so we
    look inside groups.  httpx2 transport/timeout errors (mcp ≥2.0's HTTP
    layer) are retryable too — the plugin may still be starting up after
    signaling its port.
    """
    if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError)):
        return True
    try:
        import httpx2
    except ImportError:
        httpx2 = None
    if httpx2 is not None and isinstance(exc, httpx2.HTTPError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_retryable_connect_error(e) for e in exc.exceptions)
    return False


def _is_external_cancel() -> bool:
    """True if the current task was cancelled from outside (``task.cancel()``).

    The MCP SDK's ``streamable_http_client`` tears its own anyio task group
    down — ``CancelledError("Cancelled via cancel scope ...")`` — when a
    sibling stream fails (e.g. GET/SSE negotiation against a server whose
    uvicorn accept loop has not started yet).  That cancellation is raised
    *inside* the connect's own await, so the task's cancellation counter
    stays ``0``.  A genuine external cancellation (a ``wait_for``/timeout
    elapsing, or shutdown) strikes the task itself and raises the counter.
    ``retry-cancel-vs-external`` empirically: SDK-scope cancel → ``0``,
    ``task.cancel()`` → ``>=1``.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class MCPClient:
    """MCP client for connecting to Slife plugin servers via Streamable HTTP."""

    def __init__(self, tool_timeout: float | None = None,
                 client_info_extra: dict | None = None):
        if tool_timeout is None:
            tool_timeout = _timeouts.timeouts.work.tool_budget  # call-time lookup
        self._session: ClientSession | None = None
        self._connected: bool = False
        #: The protocol version the era negotiation adopted (None before
        #: connect) — modern means session-less requests, per-request `_meta`.
        self._era: str | None = None
        #: Supervisor for the `subscriptions/listen` stream a modern peer
        #: needs (see watch_tools_changed); None on legacy links.
        self._watch_task: asyncio.Task | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._http_client: httpx2.AsyncClient | None = None
        self._tool_timeout = tool_timeout
        # Temp image files handed to the UI for display, deleted on disconnect
        # so a long session's image tool results don't accumulate.  Per-client
        # (not module-global) so one client's disconnect can't clear another's.
        self._temp_image_files: set[str] = set()
        # Extra host params the server should see: carried on the standard
        # ``initialize`` request in ``capabilities.extensions`` (mcp ≥2.0;
        # the ``clientInfo.other`` slot was dropped) — e.g. the host's active
        # embedding endpoint when connecting to the mcp-gateway wrapper.
        self._client_info_extra = client_info_extra
        # Optional async callback(method, params) invoked for server-initiated
        # notifications (e.g. ``notifications/tools/list_changed``).  Must
        # return quickly — it runs on the SDK's receive loop; a handler that
        # awaits a call_tool on the same session would deadlock that loop.
        self.on_notification: Callable[[str, dict], Awaitable[None]] | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self, url: str) -> None:
        """Connect to an MCP server via Streamable HTTP transport.

        Retries on connection failure — the server may still be starting
        (the port signal is sent before uvicorn begins accepting).
        """
        if self._connected:
            logger.warning("mcp_client_already_connected")
            return

        # The SDK's cancel-scope teardown bug surfaces as an unretrieved task
        # exception on the running loop — install the demoting handler before
        # any transport that could trigger it is created.
        _install_cancel_scope_exception_handler()

        logger.info("mcp_client_connect transport=%s url=%s", "streamable-http", url)

        last_err = None
        attempt: int = -1
        for attempt in range(_CONNECT_RETRY_ATTEMPTS):
            try:
                # Bound the WHOLE attempt — transport setup included.  A
                # plugin whose app isn't serving yet (memfiles' eager ngrok
                # tunnel delays lifespan startup) would otherwise leave the
                # streamable_http_client enter pending forever, with no
                # timeout to surface it into the retry loop.
                async with asyncio.timeout(_timeouts.timeouts.ready.connect_attempt):
                    self._exit_stack = AsyncExitStack()
                    if self._http_client is None:
                        # Shared construction (timeouts + proxy-free): a
                        # provided client is owned by us (the SDK does not
                        # manage its lifecycle), so it is closed in _cleanup.
                        # mcp ≥2.0's transport is built on `httpx2` and types
                        # its http_client against it — the older `httpx`
                        # distribution would only duck-type-fly.  Use httpx2
                        # so the injected client matches the SDK's contract.
                        self._http_client = make_local_http_client()
                    # mcp ≤2.0 yielded ``(read, write, get_session_id)``; mcp
                    # 2.1 dropped the session-id callback → a 2-tuple.  We
                    # never consumed it, so unpack the current shape only.
                    read_stream, write_stream = await self._exit_stack.enter_async_context(
                        streamable_http_client(url, http_client=self._http_client),
                    )
                    # mcp ≥2.0: host extras ride in the standard
                    # ``capabilities.extensions`` map (identifier → settings)
                    # on the initialize request — the old ``clientInfo.other``
                    # smuggling was dropped from the wire models.  The mcp
                    # gateway's ``_client_info_extra`` is exactly that shape
                    # (``{"embeddings": {...}}``), so pass it as session
                    # extensions; the server's ``_CaptureClientEmbeddings``
                    # reads it back from init params.
                    self._session = await self._exit_stack.enter_async_context(
                        ClientSession(
                            read_stream, write_stream,
                            message_handler=self._handle_server_message,
                            client_info=Implementation(name="slife", version=__version__),
                            extensions=(
                                dict(self._client_info_extra)
                                if self._client_info_extra else None
                            ),
                        ),
                    )
                    # Era negotiation, not a handshake: our own plugin servers
                    # answer `server/discover`, so the link comes up modern
                    # (2026-07-28 — no session, per-request `_meta`); a peer
                    # that answers as legacy still gets the initialize
                    # handshake.  See slife/mcp/era.py.
                    self._era = await negotiate_era(self._session)
                break  # success
            except asyncio.CancelledError as exc:
                await self._cleanup()
                if _is_external_cancel():
                    # Real controller cancellation (wait_for / shutdown):
                    # it has already struck this task — propagate, don't
                    # swallow it into a retry.
                    raise
                # No external cancel striking this task → the cancellation
                # came from the MCP SDK's own task group ("Cancelled via
                # cancel scope ...") when a sibling stream — the GET/SSE
                # negotiation — died against a server whose uvicorn accept
                # loop has not started yet (the port signal fires inside the
                # plugin's lifespan; uvicorn serves only after it completes).
                # WSL's slower plugin lifespans hit that window.  It is a
                # transient transport failure, not a cancellation of the
                # connect — fall through to the retry path.
                last_err = exc
                if attempt < _CONNECT_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_timeouts.timeouts.ready.connect_retry_delay)
                    continue
            except Exception as e:
                last_err = e
                await self._cleanup()
                if not _is_retryable_connect_error(e):
                    raise
                if attempt < _CONNECT_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_timeouts.timeouts.ready.connect_retry_delay)

        if not self._session:
            raise ConnectionError(
                f"Failed to connect to {url} after "
                f"{_CONNECT_RETRY_ATTEMPTS} attempts: {last_err}"
            )

        self._connected = True
        logger.info(
            "mcp_client_connected transport=%s url=%s attempts=%d era=%s "
            "protocol=%s",
            "streamable-http", url, attempt + 1,
            peer_era(self._session), self._era,
        )
        # A modern peer delivers `tools/list_changed` ONLY on a listen stream
        # it is asked for (the session channel is gone at that era), so keep
        # one open.  A legacy peer keeps its session channel — no stream to
        # open (and asking would only earn a ListenNotSupportedError).
        if peer_era(self._session) == "modern":
            self._watch_task = asyncio.create_task(
                watch_tools_changed(self._session, self._forward_tools_changed),
                name="mcp-listen",
            )

    async def _forward_tools_changed(self) -> None:
        """Feed a listen-stream event into the existing notification handler.

        The listen stream carries a bare level trigger, so this reuses the
        exact contract the session channel used (`on_notification(method,
        params)`) — every wiring site stays unchanged, only the trigger moves.
        """
        handler = self.on_notification
        if handler is None:
            return
        try:
            await handler("notifications/tools/list_changed", {})
        except Exception:
            logger.debug("mcp_listen_handler_failed", exc_info=True)

    async def _handle_server_message(self, message: Any) -> None:
        """Dispatch server-initiated notifications to ``on_notification``.

        Installed as the SDK ``ClientSession`` message_handler — the receive
        loop feeds every server notification here (responses are matched by
        id internally and never reach this).  Only ``notifications/*`` methods
        are forwarded; everything else (server→client requests, errors) is
        ignored.  Must return without awaiting a call_tool on this session —
        the SDK receive loop awaits this handler, so that would deadlock.
        """
        method = getattr(message, "method", None)
        if not isinstance(method, str) or not method.startswith("notifications/"):
            return
        params = getattr(message, "params", None)
        params_dict: dict = {}
        if isinstance(params, dict):
            params_dict = dict(params)
        elif params is not None:
            dump = getattr(params, "model_dump", None)
            if callable(dump):
                dumped = dump()
                if isinstance(dumped, dict):
                    params_dict = dumped
        handler = self.on_notification
        if handler is None:
            return
        try:
            await handler(method, params_dict)
        except Exception:
            logger.exception("mcp_notification_handler_failed method=%s", method)

    async def disconnect(self) -> None:
        """Disconnect from the MCP server and release all resources."""
        self._connected = False
        await self._cleanup()
        # Remove temp images handed out for display — a long session would
        # otherwise accumulate one per image tool result.
        for p in list(self._temp_image_files):
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
        self._temp_image_files.clear()
        logger.info("mcp_client_disconnected")

    async def _cleanup(self) -> None:
        """Close the exit stack, properly exiting all nested contexts.

        The ``streamable_http_client`` async generator from the MCP library
        uses ``anyio.create_task_group()`` internally.  When the connection
        fails during setup (before ``session.initialize()`` succeeds), the
        TaskGroup's cancel-scope cleanup can raise ``BaseExceptionGroup``
        or ``RuntimeError`` (task mismatch) — both escape the bare
        ``except Exception`` and need to be swallowed explicitly.

        A zero-sleep after ``aclose()`` lets the event loop deliver any
        pending generator finalisation callbacks so they don't fire during
        garbage collection and crash the process.
        """
        # Stop the listen supervisor FIRST: its stream lives on the session
        # this stack owns, and a re-listen racing the teardown would surface
        # as a spurious error from a dead transport.
        if self._watch_task is not None and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("mcp_listen_stop_failed", exc_info=True)
        self._watch_task = None
        if self._exit_stack:
            await close_exit_stack_bounded(self._exit_stack)
            # Give pending generator-finalisation callbacks a chance to run
            # in the current task instead of during GC.
            try:
                await asyncio.sleep(0)
            except Exception:
                pass
            self._exit_stack = None
        # Close our proxy-free HTTP client (the SDK does not own it when
        # provided).  Recreated fresh on the next connect().
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None
        self._session = None

    async def list_tools(self) -> list[dict]:
        """Return tools from the connected MCP server.

        Wraps ``session.list_tools()`` with a timeout so a hung
        Streamable HTTP session can't block the caller indefinitely.
        On Windows / ProactorEventLoop, concurrent sessions to the
        same server may hang if the underlying SSE transport gets
        into a bad state — the timeout ensures we fail fast instead
        of blocking for 30+ seconds.
        """
        self._ensure_connected()
        assert self._session is not None  # post-condition of _ensure_connected
        # list_tools is a local Streamable HTTP call — should complete
        # in under 15s even with hundreds of tools.  Cap the timeout
        # so a stuck SSE session on a subagent doesn't outlive the
        # parent's 30s spawn timeout.
        list_timeout = min(
            self._tool_timeout, _timeouts.timeouts.ready.list_tools,
        )
        try:
            # asyncio.timeout, not asyncio.wait_for: a stuck SSE session on
            # Windows/Proactor can defeat wait_for's cancellation (the inner
            # task never finishes cancelling, so wait_for blocks forever).
            # asyncio.timeout raises at the deadline without waiting for the
            # inner task — the hang becomes a recoverable TimeoutError.
            async with asyncio.timeout(list_timeout):
                result = await self._session.list_tools()
        except TimeoutError:
            raise TimeoutError(
                f"list_tools timed out after {list_timeout}s — "
                f"the MCP server may have a stuck SSE session"
            )
        return [
            # ``input_schema`` is the canonical attr in mcp-types ≥2.0
            # (``inputSchema`` remains the JSON alias); the proxy contract is
            # the wire name, so the key keeps the camelCase form.
            {"name": t.name, "description": t.description or "",
             "inputSchema": t.input_schema}
            for t in result.tools
        ]

    def _save_image_bytes(self, data: bytes) -> str | None:
        """Save *data* to a temp file if it looks like an image.

        Returns the absolute path, or ``None`` if the data is not a
        recognised image format or saving fails.  The file is registered for
        deletion at client disconnect (:meth:`MCPClient.disconnect`).
        """
        ext = _guess_image_extension(data)
        if ext is None:
            return None
        try:
            tmp = tempfile.NamedTemporaryFile(
                suffix=ext, delete=False, dir=tempfile.gettempdir(),
            )
            tmp.write(data)
            tmp.close()
            path = str(Path(tmp.name).resolve())
            self._temp_image_files.add(path)
            return path
        except Exception:
            return None

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Call an MCP tool.

        Returns the result text on success, or an ``"Error: …"`` string
        on failure — this function NEVER raises, so a single hung MCP
        server can't stall the entire agent loop.  The LLM sees the
        error as a normal tool result and can retry or report it.

        Timeout enforcement is handled by the Agent Loop (``agent/loop.py``)
        via ``asyncio.wait_for`` — this method does NOT apply its own
        timeout, so per-call overrides (e.g. ``call_tool_with_timeout``)
        propagate correctly.
        """
        self._ensure_connected()
        assert self._session is not None  # post-condition of _ensure_connected
        args = arguments or {}
        try:
            result = await self._session.call_tool(name, args)
        except Exception as e:
            msg = (
                f"Tool '{name}' failed: {type(e).__name__}: {e}. "
                f"Check the MCP server status."
            )
            logger.warning("mcp_tool_error name=%s err=%s", name, e)
            return f"Error: {msg}"

        # CallToolResult carries ``is_error`` (snake_case) — the SDK's
        # pydantic field name, matching connection.py's read.  The wire/
        # camelCase alias ``isError`` does not exist on the parsed model.
        if getattr(result, "is_error", False):
            parts: list[str] = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)  # type: ignore[union-attr]
            return "Error: " + "\n".join(parts)

        parts: list[str] = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)  # type: ignore[union-attr]
            elif hasattr(block, "data"):
                img_path = self._save_image_bytes(block.data)  # type: ignore[union-attr]
                if img_path is not None:
                    # Binary image content is materialized to a temp file so
                    # the LLM can reference it by path — no in-terminal
                    # rendering; the user opens the file with the OS.
                    parts.append(str(img_path))
                else:
                    parts.append(f"[binary data: {len(block.data)} bytes]")  # type: ignore[union-attr]
            else:
                parts.append(str(block))
        return "\n".join(parts)

    async def ping(self) -> bool:
        if self._session is None:
            return False
        try:
            await self._session.send_ping()
            return True
        except Exception:
            return False

    def _ensure_connected(self) -> None:
        if not self._connected or self._session is None:
            raise RuntimeError("MCP client is not connected.")
