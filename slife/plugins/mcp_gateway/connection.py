"""MCP connection pool — persistent connections to external MCP servers.

Supports three transports via the **official ``mcp`` SDK** (the same
mechanism ``client.MCPClient`` uses to reach slife's own plugin children):
  - stdio:           spawn server as subprocess, JSON-RPC over pipes
  - http (SSE):      GET /sse for server→client events, POST /messages for requests
  - http (streamable): POST JSON-RPC with mcp-session-id header

Every transport enters an ``AsyncExitStack`` that yields ``(read, write)``
streams for one ``mcp.ClientSession``.  This class supplies the connection
lifecycle the SDK does not: OAuth device flow, health monitor (ping +
reconnect with backoff), stderr relay, per-server connect locking, and the
review-driven ``needs_user_auth`` pause (F5).

The raw-JSON-RPC client these methods replaced (a ~1200-line hand-rolled
stack that duplicated ClientSession's protocol layer) was consolidated onto
the SDK — E2.
"""

import asyncio
import json
import logging
import os
import subprocess as _subprocess
import time as _time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from enum import Enum
from io import TextIOBase

import httpx2

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import (
    InitializedNotification,
    TextContent,
    ImageContent,
)

from slife.plugins.mcp_gateway import __version__
from slife.plugins.mcp_gateway.config import _is_env_ref, _resolve_embedded_refs, _resolve_secret
from slife.platform import resolve_command

logger = logging.getLogger(__name__)

# ── Health check / reconnect ────────────────────────────────────────────
_HEALTH_CHECK_INTERVAL = 30.0      # seconds between health pings
_HEALTH_PING_TIMEOUT = 5.0         # a ping must answer within this window
_RECONNECT_BACKOFF_INITIAL = 5.0   # first reconnect retry delay (s)
_RECONNECT_BACKOFF_MAX = 60.0      # cap on exponential backoff (s)
_RECONNECT_BACKOFF_MULTIPLIER = 2.0
# The ONLY timer on connect(): bounds the whole transport-establishment +
# handshake span (spawn / socket / SSE negotiation — the phases the server
# cannot answer for while it is still coming up).  Once ``CONNECTED``, the
# protocol period carries no client timer (per the timeout architecture);
# a server that stops answering is the health monitor's concern.
_CONNECT_STARTUP_TIMEOUT = 120.0
# Max time to tear down the SDK transport (AsyncExitStack.aclose()) after a
# failed/cancelled connect — a request hung against a not-yet-ready server
# can keep aclose() from returning promptly; the retry must progress.
_CLEANUP_TIMEOUT = 2.0

#: Per-session deadline for tools/list_changed notifications (matches the
#: gateway server's notify-bound; kept together here for the connection side).
NOTIFY_TIMEOUT = 5.0


class NeedsUserAuthError(RuntimeError):
    """OAuth needs a human (device flow) — not a retriable transport failure.

    Carried out of the OAuth pre-check so the health monitor can tell a
    "give it 5 seconds and retry" failure from a "someone must approve this"
    failure.  The former backs off and retries; the latter stops
    auto-reconnect until the user re-adds the server (F5).
    """


class ServerStatus(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"


@dataclass
class ServerConfig:
    name: str
    command: str = ""                       # stdio: executable to spawn
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    url: str = ""                           # http: MCP endpoint URL
    headers: dict[str, str] | None = None   # http: extra request headers
    enabled: bool = True  # False = don't auto-connect at startup
    description: str = ""
    auth: dict | None = None  # OAuth config for device code flow
    source: dict | None = None  # provenance metadata (e.g. {"type": "rest_api"})
    os_paths: bool = False  # inject --allow-path from the OS-accessible path set
    auto_load: bool = False  # True = host bulk-registers this server's tools on connect

    @property
    def transport(self) -> str:
        """Return the transport mode: 'http' or 'stdio'."""
        return "http" if self.url else "stdio"


class _StderrCapture(TextIOBase):
    """TextIO sink for the SDK's stdio transport — feeds the stderr tail.

    Passed as ``errlog`` to :func:`mcp.client.stdio.stdio_client`, which
    writes the child's stderr hered.  Keeps the last ``_MAX_STDERR_LINES``
    lines (connect()'s error path reads the tail, like the relay it
    replaces); a chatty server must not grow it without bound.
    """

    _MAX_STDERR_LINES = 500

    def __init__(self, connection: "MCPServerConnection") -> None:
        self._conn = connection

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        buf = self._conn._stderr_buffer
        buf.append(text)
        if len(buf) > self._MAX_STDERR_LINES:
            del buf[: len(buf) - self._MAX_STDERR_LINES]
        logger.debug(
            "mcp_stderr server=%s line=%s",
            self._conn.config.name, text.rstrip("\n"),
        )
        return len(text)

    def flush(self) -> None:
        pass


class MCPServerConnection:
    """Persistent MCP client connection over the official SDK sessions.

    Owns one external MCP server's lifecycle: OAuth, transport selection,
    tool catalog cache, health monitor (ping + reconnect with backoff), and
    the review-driven ``needs_user_auth`` pause.  All protocol is the mcp
    SDK's ``ClientSession`` — no hand-rolled JSON-RPC (E2).
    """

    def __init__(
        self,
        config: ServerConfig,
        on_connected: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.config = config
        self._status = ServerStatus.DISCONNECTED
        self._on_connected = on_connected
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._http_client: httpx2.AsyncClient | None = None
        self._sse_mode: bool = False
        self._stderr_capture: _StderrCapture | None = None
        self._stderr_buffer: list[str] = []
        self._tools_cache: list[dict] = []
        self._error: str | None = None
        self._notify_tasks: "set[asyncio.Task]" = set()  # fire-and-forget _notify posts
        self._connect_lock = asyncio.Lock()  # serializes connect()
        # Set by disconnect() so an in-flight connect aborts at its next check
        # point instead of resuming after cleanup and spawning an orphaned
        # transport + health monitor.
        self._disconnecting = False
        # Background health monitor (ping + reconnect) — started on first
        # successful connect, cancelled by disconnect()/remove_server().
        self._health_task: "asyncio.Task | None" = None
        # OAuth: the refresh token was revoked / never granted and the device
        # flow needs a human.  While set, the health monitor STOPS auto-
        # reconnecting (F5).  Cleared by a fresh mcp_set / mcp_remove.
        self._needs_user_auth: bool = False

    @property
    def status(self) -> ServerStatus:
        return self._status

    @property
    def needs_user_auth(self) -> bool:
        return self._needs_user_auth

    @property
    def tool_count(self) -> int:
        return len(self._tools_cache)

    @property
    def error(self) -> str | None:
        return self._error

    # ── OAuth ──────────────────────────────────────────────────────────

    async def _ensure_oauth_token(self) -> None:
        """Obtain or refresh an OAuth token and inject it into connection headers.

        Called before transport connect when ``config.auth.type == "oauth"``.
        Mutates ``self.config.headers`` in place — the transport layer
        picks up the token automatically.

        When the token is gone AND the refresh failed, the ONLY way forward is
        the interactive device flow.  That needs a human at the desktop — it
        must run once, on a real (re)connect attempt the user drives, never on
        every health-monitor backoff cycle.  So after a failed device flow the
        connection is marked :attr:`needs_user_auth` and the monitor stops
        auto-reconnecting (F5).
        """
        from slife.plugins.mcp_gateway.oauth import (
            get_valid_token,
            run_device_code_flow,
            refresh_access_token,
        )

        auth = self.config.auth
        assert auth is not None  # guarded by caller
        name = self.config.name

        tokens = get_valid_token(name)
        if tokens is None:
            # Try refresh first (may have expired with valid refresh_token)
            try:
                tokens = await refresh_access_token(auth, name)
                self._needs_user_auth = False
            except Exception:
                if self._needs_user_auth:
                    # The user was already asked and the flow did not complete —
                    # do NOT re-run it (a monitor reconnect would otherwise pop
                    # a fresh desktop prompt every backoff cycle, F5).  Surface
                    # the state for call_tool / mcp_set_enabled instead.
                    raise NeedsUserAuthError(
                        f"Server '{name}' needs OAuth re-authorization — "
                        "use mcp_remove / mcp_set to re-add it and run the "
                        "device flow again."
                    ) from None
                logger.info("oauth_refresh_failed server=%s action=device_flow", name)
                self._needs_user_auth = True
                try:
                    tokens = await run_device_code_flow(auth, name)
                except Exception as e:
                    logger.warning("oauth_device_flow_failed server=%s err=%s", name, e)
                    # Still needs the user — keep the flag so the monitor stops.
                    raise NeedsUserAuthError(
                        f"Server '{name}' OAuth device flow failed: {e}"
                    ) from e
                self._needs_user_auth = False

        # Inject token into headers
        if self.config.headers is None:
            self.config.headers = {}
        self.config.headers["Authorization"] = (
            f"{tokens.token_type} {tokens.access_token}"
        )
        logger.info("oauth_token_injected server=%s", name)

    # ── Transport selection (SDK transports → one ClientSession) ─────────

    async def _connect_stdio(self) -> None:
        """Spawn server as subprocess and connect the SDK stdio client."""
        exe = resolve_command(self.config.command)
        env = dict(os.environ)
        if self.config.env:
            for key, value in self.config.env.items():
                env[key] = _resolve_secret(value)

        # Resolve ${VAR} references in args (e.g. "Authorization: Bearer ${GITHUB_TOKEN}")
        resolved_args = [
            _resolve_secret(arg) if _is_env_ref(arg)
            else _resolve_embedded_refs(arg)
            for arg in self.config.args
        ]
        if self.config.os_paths:
            from slife.os_detect import get_os_accessible_paths
            for p in get_os_accessible_paths():
                resolved_args += ["--allow-path", p]

        params = StdioServerParameters(
            command=exe, args=resolved_args, env=env or None,
        )
        if self._exit_stack is None:
            self._exit_stack = AsyncExitStack()
        self._stderr_capture = _StderrCapture(self)
        read_stream, write_stream = await self._exit_stack.enter_async_context(
            # _StderrCapture subclasses io.TextIOBase (a typing.TextIO); Pylance
            # treats the stdlib structural alias strictly, but it IS one.
            stdio_client(params, errlog=self._stderr_capture),  # type: ignore[arg-type]
        )
        self._session = await self._exit_stack.enter_async_context(
            self._new_session(read_stream, write_stream),
        )

    async def _connect_http(self) -> None:
        """Create HTTP client; detect SSE vs Streamable HTTP and connect the SDK transport."""
        # Resolve ${VAR} references in URL (e.g. SSE URL with API key)
        url = _resolve_embedded_refs(self.config.url).rstrip("/")

        # Resolve ${VAR} references in headers; OAuth token rides here too.
        headers: dict[str, str] = {}
        if self.config.headers:
            headers.update(
                {k: _resolve_embedded_refs(v) for k, v in self.config.headers.items()}
            )

        # Detect transport: try SSE first (the SDK's sse_client handles the
        # endpoint-discovery handshake itself); fall back to Streamable HTTP.
        # A server that answers a plain POST but not an SSE GET will make
        # sse_client's enter fail (non-event-stream response) → fall through.
        stack = self._exit_stack if self._exit_stack is not None else AsyncExitStack()
        self._exit_stack = stack
        try:
            read_stream, write_stream = await stack.enter_async_context(
                sse_client(url, headers=headers),
            )
            self._sse_mode = True
            logger.info("mcp_sse_connected server=%s url=%s", self.config.name, url)
        except Exception:
            # SSE not supported — release anything the failed enter opened and
            # retry as Streamable HTTP.  The SDK reuses a pre-built httpx2
            # client, so build one lazily here (with the OAuth/resolved
            # headers riding along); the SSE-success path never allocates it.
            if self._exit_stack is not None:
                try:
                    await asyncio.wait_for(
                        self._exit_stack.aclose(), timeout=_CLEANUP_TIMEOUT,
                    )
                except (asyncio.TimeoutError, RuntimeError, BaseExceptionGroup):
                    pass
                self._exit_stack = AsyncExitStack()
            if self._http_client is None:
                # No read/write timeout of our own — enforcement lives in the
                # agent loop's tool_timeout (per the timeout architecture).
                # Only connect/pool are bounded so a dead endpoint can't hang
                # the handshake; ping carries its own 5s wait_for.
                self._http_client = httpx2.AsyncClient(
                    headers=headers,
                    timeout=httpx2.Timeout(
                        connect=10.0, read=None, write=None, pool=10.0,
                    ),
                    trust_env=False,
                )
            read_stream, write_stream = await self._exit_stack.enter_async_context(
                streamable_http_client(url, http_client=self._http_client),
            )
            self._sse_mode = False
            logger.debug("mcp_streamable_http server=%s url=%s", self.config.name, url)

        self._session = await self._exit_stack.enter_async_context(
            self._new_session(read_stream, write_stream),
        )

    def _new_session(self, read_stream, write_stream) -> ClientSession:
        """Build the SDK ClientSession wired to our notification handler."""
        # The SDK's cancel-scope teardown bug surfaces as an unretrieved task
        # exception on the running loop — install the demoting handler before
        # any transport that could trigger it is created (client.py does the
        # same for plugin connections).
        from slife.plugins.mcp_gateway.client import _install_cancel_scope_exception_handler
        _install_cancel_scope_exception_handler()

        from mcp.types import Implementation as _Impl
        return ClientSession(
            read_stream, write_stream,
            message_handler=self._handle_notification,
            client_info=_Impl(name="mcp-plugin", version=__version__),
        )

    async def _handle_notification(self, message) -> None:
        """Forward server notifications (tools/list_changed) to the host via
        ``on_connected``.  Read-only path — never called from a tool call."""
        method = getattr(message, "method", None)
        if not isinstance(method, str):
            return
        if method == "notifications/tools/list_changed" and self._on_connected is not None:
            try:
                await self._on_connected(self.config.name)
            except Exception as exc:
                logger.warning(
                    "mcp_notification_handler_failed server=%s err=%s",
                    self.config.name, exc,
                )

    # ── Connection lifecycle ────────────────────────────────────────────

    async def connect(self) -> None:
        if self._status == ServerStatus.CONNECTED:
            logger.info("mcp_already_connected server=%s", self.config.name)
            return

        # Serialize connects — the health monitor, call_tool's lazy reconnect,
        # and mcp_set_enabled can otherwise each spawn their own transport,
        # orphaning the loser (and starting duplicate monitors).
        async with self._connect_lock:
            # A disconnect() that raced an in-flight connect must not be
            # undone by this fresh connect.
            if self._disconnecting:
                return
            self._status = ServerStatus.CONNECTING
            self._error = None
            self._stderr_buffer.clear()

            # ── OAuth pre-check ───────────────────────────────────────
            if self.config.auth and self.config.auth.get("type") == "oauth":
                await self._ensure_oauth_token()
            if self._disconnecting:
                return  # disconnect() ran mid-OAuth — don't spawn a transport

            t0 = _time.monotonic()
            transport = self.config.transport
            logger.info("mcp_connect server=%s transport=%s", self.config.name, transport)

            self._exit_stack = AsyncExitStack()
            try:
                # Transport establishment is the one client-owned wait (spawn
                # + socket/SSE setup).  asyncio.timeout, not wait_for: on
                # Windows/Proactor a stuck transport op can defeat wait_for's
                # cancellation and block past the deadline.
                async with asyncio.timeout(_CONNECT_STARTUP_TIMEOUT):
                    if transport == "stdio":
                        await self._connect_stdio()
                    else:
                        await self._connect_http()

                    assert self._session is not None
                    # MCP initialize handshake (SDK-managed, official params).
                    await self._session.initialize()
                    # Send the initialized notification (typed SDK notification).
                    await self._session.send_notification(InitializedNotification())

                    # Discover tools
                    tools_result = await self._session.list_tools()
                    self._tools_cache = [
                        {
                            "name": t.name,
                            "description": t.description or "",
                            # input_schema is the canonical attr in mcp-types ≥2.0;
                            # the proxy contract is the wire name (camelCase).
                            "inputSchema": t.input_schema,
                        }
                        for t in tools_result.tools
                    ]

                self._status = ServerStatus.CONNECTED
                elapsed = (_time.monotonic() - t0) * 1000
                logger.info(
                    "mcp_connected server=%s tools=%d took_ms=%.0f",
                    self.config.name, len(self._tools_cache), elapsed,
                )

                # Start the health monitor once per connection object — a
                # running monitor is reused across reconnects, so never spawn
                # a second.  A disconnect() that landed mid-connect must not
                # leave an orphaned monitor pinging a torn-down transport.
                if self._disconnecting:
                    self._status = ServerStatus.DISCONNECTED
                    return

                # A connect (first or reconnect) can mean the server's tool
                # surface appeared or changed — notify listeners so they
                # re-discover and re-register (idempotent full-diff).
                await self._fire_on_reconnect()

                if self._health_task is None or self._health_task.done():
                    self._health_task = asyncio.create_task(self._health_monitor())

                # Run post-connect setup (best-effort, never blocks on failure)
                await self._post_connect_setup()

            except asyncio.CancelledError:
                # A cancelled connect must not leave the status stuck in
                # CONNECTING.  Reset to DISCONNECTED unconditionally — a
                # CONNECTED-with-dead-transport wedge makes __check report
                # "running" while call_tool skips its lazy reconnect (F5/A6).
                self._status = ServerStatus.DISCONNECTED
                await self._cleanup_resources()
                # A cancelled mid-sync connect (e.g. a host tool-timeout on
                # mcp_set) must still recover: start a health monitor when
                # none is running so DISCONNECTED is reconnected in background.
                if (
                    not self._disconnecting
                    and self.config.enabled
                    and (self._health_task is None or self._health_task.done())
                ):
                    self._health_task = asyncio.create_task(self._health_monitor())
                raise

            except Exception as e:
                self._status = ServerStatus.FAILED
                stderr_tail = "".join(self._stderr_buffer[-20:]).strip()
                self._error = f"{e}\n\n[server stderr]\n{stderr_tail}" if stderr_tail else str(e)
                logger.exception("mcp_connect_failed server=%s err=%s", self.config.name, e)
                await self._cleanup_resources()
                # Start the health monitor even on a failed initial connect so
                # a server that was down at startup is retried in background.
                if self.config.enabled and (
                    self._health_task is None or self._health_task.done()
                ):
                    self._health_task = asyncio.create_task(self._health_monitor())

    async def _fire_on_reconnect(self) -> None:
        """Notify listeners that the server is connected.

        Fires on EVERY successful connect (first and reconnects).  Best-effort:
        a failing listener never breaks the connection.
        """
        logger.info("mcp_connected server=%s", self.config.name)
        if self._on_connected is not None:
            try:
                await self._on_connected(self.config.name)
            except Exception:
                logger.exception("mcp_on_connected_failed server=%s", self.config.name)

    async def _post_connect_setup(self) -> None:
        """Run server-specific post-connect setup (best-effort).

        On Windows, the ``mcp-server-fetch`` package's ``readabilipy``
        dependency cannot detect ``npm`` because Python's ``subprocess.run``
        on Windows only tries ``.exe`` extensions via ``CreateProcess``,
        and ``npm`` only ships as ``npm.cmd``.

        Pre-installing the ``node_modules`` into ``readabilipy``'s
        ``javascript`` directory lets ``have_node()`` succeed without
        ever calling ``have_npm()``, sidestepping the detection bug.
        """
        if self.config.name != "fetch":
            return

        try:
            # Locate readabilipy inside the uvx-managed environment
            result = _subprocess.run(
                [
                    "uvx", "--from", "mcp-server-fetch", "python", "-c",
                    "import readabilipy, os; print(os.path.dirname(readabilipy.__file__))",
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return
            readabilipy_dir = result.stdout.strip()
            if not readabilipy_dir or not os.path.isdir(readabilipy_dir):
                return

            jsdir = os.path.join(readabilipy_dir, "javascript")
            if not os.path.isdir(jsdir):
                return

            if os.path.isdir(os.path.join(jsdir, "node_modules")):
                logger.debug("fetch_npm_skip reason=node_modules_present")
                return

            logger.info("fetch_npm_install jsdir=%s", jsdir)
            npm_cmd = ["cmd", "/c", "npm", "install"]
            install = _subprocess.run(
                npm_cmd, cwd=jsdir,
                capture_output=True, text=True, timeout=60,
            )
            if install.returncode == 0:
                logger.info("fetch_npm_installed jsdir=%s", jsdir)
            else:
                logger.warning(
                    "fetch_npm_install_failed jsdir=%s err=%s",
                    jsdir, (install.stderr or "")[-500:],
                )
        except Exception:
            logger.debug("fetch_npm_setup_error", exc_info=True)

    # ── Tool operations (SDK session) ────────────────────────────────────

    def list_tools(self) -> list[dict]:
        return list(self._tools_cache)

    async def ping(self, timeout: float = _HEALTH_PING_TIMEOUT) -> bool:
        """Return True if the server answers an MCP ping (SDK send_ping)."""
        if self._status != ServerStatus.CONNECTED or self._session is None:
            return False
        try:
            await asyncio.wait_for(self._session.send_ping(), timeout=timeout)
            return True
        except Exception:
            return False

    async def _health_monitor(self) -> None:
        """Background health check: ping the server and reconnect when dead.

        Covers both failure modes:
          - CONNECTED but unresponsive (process died or hung): mark
            DISCONNECTED, tear down the transport, and reconnect.
          - DISCONNECTED/FAILED (e.g. a prior connect attempt failed): keep
            retrying with exponential backoff while the server is enabled.

        Runs for the connection object's lifetime — cancelled by
        ``disconnect()``/``remove_server()``.  Reconnect attempts are paced by
        backoff (5s → … → 60s) so a server that is down for a while isn't
        hammered.
        """
        backoff = _RECONNECT_BACKOFF_INITIAL

        async def _try_reconnect() -> bool:
            nonlocal backoff
            try:
                await self.connect()
                backoff = _RECONNECT_BACKOFF_INITIAL
                return True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._status = ServerStatus.DISCONNECTED
                self._error = f"Reconnect failed: {e}"
                logger.warning(
                    "mcp_health_reconnect_failed server=%s backoff=%.1fs err=%s",
                    self.config.name, backoff, e,
                )
                return False

        try:
            while True:
                # OAuth needs a human (F5): do not keep re-running the device
                # flow on every backoff cycle — that would pop a desktop
                # prompt over and over.  The user re-adds the server after
                # authorizing; the flag is cleared then.
                if self._needs_user_auth:
                    logger.warning(
                        "mcp_needs_user_auth server=%s — auto-reconnect paused",
                        self.config.name,
                    )
                    await asyncio.sleep(_HEALTH_CHECK_INTERVAL)
                    continue
                wait = _HEALTH_CHECK_INTERVAL
                if not self.config.enabled:
                    return
                if self._status == ServerStatus.CONNECTING:
                    pass  # a manual connect is already in progress — wait
                elif self._status == ServerStatus.CONNECTED:
                    if self._connect_lock.locked():
                        pass  # a connect is in flight — don't interrupt it
                    elif await self.ping():
                        backoff = _RECONNECT_BACKOFF_INITIAL
                    else:
                        # Died or hung — mark disconnected, tear down, reconnect.
                        logger.warning(
                            "mcp_health_check_failed server=%s action=reconnect",
                            self.config.name,
                        )
                        self._status = ServerStatus.DISCONNECTED
                        self._error = (
                            "Health check failed — server not responding to ping."
                        )
                        await self._cleanup_resources()
                        if not await _try_reconnect():
                            wait = backoff
                            backoff = min(
                                backoff * _RECONNECT_BACKOFF_MULTIPLIER,
                                _RECONNECT_BACKOFF_MAX,
                            )
                else:
                    # DISCONNECTED or FAILED → (re)connect with backoff pacing.
                    if not await _try_reconnect():
                        wait = backoff
                        backoff = min(
                            backoff * _RECONNECT_BACKOFF_MULTIPLIER,
                            _RECONNECT_BACKOFF_MAX,
                        )
                await asyncio.sleep(wait)
        except asyncio.CancelledError:
            pass

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        if self._status != ServerStatus.CONNECTED:
            # The health monitor marks a dead/hung server DISCONNECTED.  If
            # the server is enabled, try a lazy reconnect first.  Connecting IS
            # the probe — EXCEPT OAuth: a needs-auth server must not be lazily
            # reconnected (that re-runs the device flow; F5).
            if (
                self.config.enabled
                and not self._needs_user_auth
                and self._status == ServerStatus.DISCONNECTED
            ):
                try:
                    await self.connect()
                except Exception:
                    pass
            if self._status != ServerStatus.CONNECTED:
                if self._needs_user_auth:
                    raise NeedsUserAuthError(
                        f"Server '{self.config.name}' needs OAuth re-authorization — "
                        "use mcp_remove / mcp_set to re-add it and run the "
                        "device flow again."
                    )
                raise ValueError(
                    f"Server '{self.config.name}' is not connected "
                    f"(status: {self._status.value})"
                )

        logger.debug("mcp_tool_call server=%s tool=%s", self.config.name, tool_name)

        session = self._session
        if session is None:
            raise ValueError(
                f"Server '{self.config.name}' has no live session "
                f"(status: {self._status.value})"
            )
        try:
            result = await session.call_tool(tool_name, arguments or {})
        except (ConnectionError, OSError):
            # Transport error — the server may have died.  Attempt one reconnect
            # before giving up.
            logger.warning(
                "mcp_tool_call_transport_error server=%s tool=%s action=reconnect",
                self.config.name, tool_name,
            )
            try:
                await self._cleanup_resources()
                self._status = ServerStatus.DISCONNECTED
                await self.connect()
                if self._status != ServerStatus.CONNECTED:
                    raise ConnectionError(
                        f"Reconnect to '{self.config.name}' failed: "
                        f"status is {self._status.value}"
                    )
                if self._session is None:
                    raise ConnectionError(
                        f"Reconnect to '{self.config.name}' produced no session"
                    )
                result = await self._session.call_tool(tool_name, arguments or {})
                logger.info(
                    "mcp_tool_call_reconnect_ok server=%s tool=%s",
                    self.config.name, tool_name,
                )
            except Exception as reconnect_error:
                self._status = ServerStatus.FAILED
                self._error = str(reconnect_error)
                logger.exception(
                    "mcp_tool_call_reconnect_failed server=%s err=%s",
                    self.config.name, reconnect_error,
                )
                raise ConnectionError(
                    f"Server '{self.config.name}' connection lost and "
                    f"reconnect failed: {reconnect_error}"
                ) from reconnect_error

        # Format content blocks (SDK typed blocks → strings).  The SDK's
        # CallToolResult carries ``is_error`` (snake_case, mcp-types ≥2.1).
        if getattr(result, "is_error", False):
            parts = [b.text for b in result.content if isinstance(b, TextContent)]
            return "Error: " + "\n".join(parts) or "Error"

        parts: list[str] = []
        for block in result.content:
            if isinstance(block, TextContent):
                parts.append(block.text)
            elif isinstance(block, ImageContent):
                parts.append(f"[image: {getattr(block, 'mimeType', '')} {len(block.data)} bytes]")
            else:
                try:
                    parts.append(block.model_dump_json())
                except Exception:
                    parts.append(str(block))
        return "\n".join(parts) if parts else json.dumps(result.model_dump())

    # ── Teardown ────────────────────────────────────────────────────────

    async def disconnect(self) -> None:
        logger.info("mcp_disconnect server=%s", self.config.name)
        # Flag any in-flight connect to abort at its next check point, then
        # serialize with it under the connect lock — otherwise a slow connect
        # (OAuth, HTTP handshake) resumes after this cleanup and spawns an
        # orphaned transport + health monitor that nothing references.
        self._disconnecting = True
        async with self._connect_lock:
            self._status = ServerStatus.DISCONNECTED
            # Stop the health monitor first — it must not keep pinging a
            # deliberately-disconnected server.
            if self._health_task is not None and not self._health_task.done():
                self._health_task.cancel()
                try:
                    await self._health_task
                except asyncio.CancelledError:
                    pass
                self._health_task = None
            await self._cleanup_resources()
            self._tools_cache = []
        self._disconnecting = False
        logger.info("mcp_disconnected server=%s", self.config.name)

    async def _cleanup_resources(self) -> None:
        # Cancel in-flight fire-and-forget notifications before closing the
        # client — a closed client would surface unretrieved task exceptions.
        for task in list(self._notify_tasks):
            task.cancel()
        self._notify_tasks.clear()

        if self._exit_stack is not None:
            try:
                # Bounded teardown: a request hung against a not-yet-ready
                # server can keep aclose() from returning promptly.
                await asyncio.wait_for(
                    self._exit_stack.aclose(), timeout=_CLEANUP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.debug("cleanup_aclose_timeout abandoning stack")
            except RuntimeError as e:
                if "cancel scope" in str(e):
                    logger.debug("cleanup_cancel_scope_suppressed err=%s", e)
                else:
                    raise
            except (Exception, BaseExceptionGroup):
                pass
            self._exit_stack = None
        self._session = None
        self._stderr_capture = None
        self._sse_mode = False
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None


class ConnectionPool:
    """Manages a collection of MCP server connections."""

    def __init__(self, on_connected: Callable[[str], Awaitable[None]] | None = None):
        self._connections: dict[str, MCPServerConnection] = {}
        # Fired on every successful connect (first and reconnects — see
        # MCPServerConnection._fire_on_reconnect).  The wrapper wires this to
        # a tools/list_changed notification so the agent re-syncs.
        self._on_connected = on_connected

    async def add_server(self, config: ServerConfig) -> MCPServerConnection:
        if config.name in self._connections:
            logger.info("mcp_replace server=%s", config.name)
            await self.remove_server(config.name)
        conn = MCPServerConnection(config=config, on_connected=self._on_connected)
        self._connections[config.name] = conn
        if config.enabled:
            await conn.connect()
        else:
            logger.info("mcp_server_disabled name=%s", config.name)
        return conn

    async def remove_server(self, name: str) -> None:
        conn = self._connections.pop(name, None)
        if conn is None:
            return
        await conn.disconnect()

    async def disconnect_server(self, name: str) -> None:
        """Disconnect a server without removing it from the pool.

        Keeps the server config in the pool (with enabled=False) so it can
        be re-enabled later without re-adding from config.
        """
        conn = self._connections.get(name)
        if conn is None:
            return
        await conn.disconnect()

    def get_server(self, name: str) -> MCPServerConnection | None:
        return self._connections.get(name)

    def server_names(self) -> list[str]:
        """Names of all registered servers (connected, disabled, or failed)."""
        return list(self._connections.keys())

    def list_configured(self) -> list[dict]:
        """List configured servers — static config fields only, no live state.

        This is the *config view*: what servers are configured, their transport,
        command/args or URL, enabled/disabled, and description.
        It deliberately excludes live connection state (connected/disconnected,
        tool counts, errors) — that is reported by :meth:`list_servers` for the
        ``__check`` internal tool.  Secret-holding fields (``env``,
        ``headers``, ``auth``) are omitted so the listing never leaks tokens.
        """
        return [
            {
                "name": name,
                "transport": conn.config.transport,
                "command": conn.config.command,
                "args": list(conn.config.args),
                "url": conn.config.url,
                "enabled": conn.config.enabled,
                "auto_load": conn.config.auto_load,
                "description": conn.config.description,
            }
            for name, conn in self._connections.items()
        ]

    def list_servers(self) -> list[dict]:
        return [
            {
                "name": name,
                "state": "running" if conn.status == ServerStatus.CONNECTED else "stopped",
                "status": conn.status.value,
                "enabled": conn.config.enabled,
                "tool_count": conn.tool_count,
                "error": conn.error,
                "needs_user_auth": conn.needs_user_auth,
                "transport": conn.config.transport,
                "command": conn.config.command,
                "args": conn.config.args,
                "url": conn.config.url,
                "description": conn.config.description,
            }
            for name, conn in self._connections.items()
        ]

    def list_all_tools(self, server_name: str) -> list[dict]:
        """List all tools from a specific server, regardless of active state."""
        conn = self._connections.get(server_name)
        if conn is None or conn.status != ServerStatus.CONNECTED:
            return []
        return [
            {**tool, "server": server_name, "full_name": f"{server_name}__{tool['name']}"}
            for tool in conn.list_tools()
        ]

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> str:
        conn = self._connections.get(server_name)
        if conn is None:
            return f"Error: Server '{server_name}' not found."
        try:
            return await conn.call_tool(tool_name, arguments)
        except Exception as e:
            logger.exception("mcp_tool_call_failed server=%s tool=%s err=%s", server_name, tool_name, e)
            return f"Error calling '{tool_name}' on '{server_name}': {e}"

    async def shutdown(self) -> None:
        for name in list(self._connections):
            await self.remove_server(name)