"""MCP connection pool — persistent connections to external MCP servers.

Supports three transports:
  - stdio:           spawn server as subprocess, raw JSON-RPC over pipes
  - http (SSE):      GET /sse for server→client events, POST /messages for requests
  - http (streamable): POST JSON-RPC with mcp-session-id header

SSE is tried first when a URL is provided; falls back to streamable HTTP
if the server doesn't respond with text/event-stream.

Avoids anyio and ClientSession entirely to prevent TaskGroup conflicts
with FastMCP.
"""

import asyncio
import json
import logging
import os
import subprocess as _subprocess
import time as _time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

import httpx2

from mcp_plugin.config import _is_env_ref, _resolve_embedded_refs, _resolve_secret
from mcp_plugin.platform import kill_process_tree, resolve_command, terminate_process

logger = logging.getLogger(__name__)

# ── Health check / reconnect ────────────────────────────────────────────
_HEALTH_CHECK_INTERVAL = 30.0      # seconds between health pings
_HEALTH_PING_TIMEOUT = 5.0         # a ping must answer within this window
_RECONNECT_BACKOFF_INITIAL = 5.0   # first reconnect retry delay (s)
_RECONNECT_BACKOFF_MAX = 60.0      # cap on exponential backoff (s)
_RECONNECT_BACKOFF_MULTIPLIER = 2.0
# The ONLY timer on connect(): bounds just the transport-establishment phase
# (spawn + socket/SSE setup) — the one phase the external server can't answer
# for.  The protocol period (initialize, tools/list, tool calls, SSE endpoint
# event) deliberately carries no client timer per the timeout design: a slow
# cold start gets the full budget, and a server that is established but stops
# answering is the health monitor's concern, not a connect-timeout race.
_CONNECT_STARTUP_TIMEOUT = 120.0


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


class MCPServerConnection:
    """Persistent MCP client connection using raw JSON-RPC.

    Supports two transports:
      - stdio: spawn server as subprocess, JSON-RPC over pipes
      - http:  POST JSON-RPC to a Streamable HTTP MCP endpoint

    No ClientSession, no anyio, no TaskGroup conflicts.
    """

    def __init__(
        self,
        config: ServerConfig,
        on_connected: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.config = config
        self._status = ServerStatus.DISCONNECTED
        self._on_connected = on_connected
        # True once connect() has succeeded at least once.  Used to tell a
        # RECONNECT apart from the first connect — only reconnects fire
        # ``on_connected`` (the initial connect is handled by the caller,
        # e.g. mcp_set/auto-connect, which already registers tools).
        self._ever_connected = False
        # True when the INITIAL connect attempt failed (timed out).  The
        # caller (mcp_set/auto-connect) saw the failure and skipped tool
        # registration, so the next successful connect — the health
        # monitor's recovery — must notify listeners even though it is not
        # a "reconnect" in the _ever_connected sense.
        self._notify_on_next_success = False
        self._process: asyncio.subprocess.Process | None = None
        self._http_client: httpx2.AsyncClient | None = None
        self._session_id: str | None = None
        self._next_id: int = 0
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock() # serializes connect()
        # Set by disconnect() so an in-flight connect aborts at its next check
        # point instead of resuming after cleanup and spawning an orphaned
        # transport + health monitor.
        self._disconnecting = False
        self._notify_tasks: "set[asyncio.Task]" = set()  # fire-and-forget _notify posts
        self._tools_cache: list[dict] = []
        self._error: str | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_buffer: list[str] = []
        # SSE transport state
        self._sse_mode: bool = False
        self._sse_message_url: str = ""
        self._sse_queue: "asyncio.Queue[dict] | None" = None
        self._sse_task: "asyncio.Task | None" = None
        # Background health monitor (ping + reconnect) — started on first
        # successful connect, cancelled by disconnect()/remove_server().
        self._health_task: "asyncio.Task | None" = None

    @property
    def status(self) -> ServerStatus:
        return self._status

    @property
    def tool_count(self) -> int:
        return len(self._tools_cache)

    @property
    def error(self) -> str | None:
        return self._error

    async def _ensure_oauth_token(self) -> None:
        """Obtain or refresh an OAuth token and inject it into connection headers.

        Called before transport connect when ``config.auth.type == "oauth"``.
        Mutates ``self.config.headers`` in place — the transport layer
        picks up the token automatically.
        """
        from mcp_plugin.oauth import (
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
            except Exception:
                logger.info("oauth_refresh_failed server=%s action=device_flow", name)
                tokens = await run_device_code_flow(auth, name)

        # Inject token into headers
        if self.config.headers is None:
            self.config.headers = {}
        self.config.headers["Authorization"] = (
            f"{tokens.token_type} {tokens.access_token}"
        )
        logger.info("oauth_token_injected server=%s", name)

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
            # A fresh transport must initialize with no session id — a stale one
            # from the previous connection (only cleared here; _cleanup_resources
            # intentionally doesn't) would be sent on the new initialize and a
            # session-enforcing server would reject every reconnect.
            self._session_id = None

            # ── OAuth pre-check ───────────────────────────────────────
            if self.config.auth and self.config.auth.get("type") == "oauth":
                await self._ensure_oauth_token()
            if self._disconnecting:
                return  # disconnect() ran mid-OAuth — don't spawn a transport

            t0 = _time.monotonic()
            transport = self.config.transport
            logger.info(
                "mcp_connect server=%s transport=%s",
                self.config.name, transport,
            )

            try:
                # Transport establishment is the one client-owned wait (spawn +
                # socket/SSE setup — the server can't signal readiness during
                # it).  asyncio.timeout, not wait_for: on Windows/Proactor a
                # stuck transport op can defeat wait_for's cancellation and
                # block past the deadline.  The initialize handshake below is
                # protocol period — no client timer (timeout design).
                async with asyncio.timeout(_CONNECT_STARTUP_TIMEOUT):
                    if transport == "stdio":
                        await self._connect_stdio()
                    else:
                        await self._connect_http()

                # MCP initialize handshake (transport-agnostic).  Follows the
                # official spec: standard params (protocolVersion,
                # capabilities, clientInfo) all live in the request's
                # ``params`` object; the client advertises the latest protocol
                # version it understands and the server negotiates.  Protocol
                # period — no client timer; the server's own response governs.
                from mcp.types import LATEST_PROTOCOL_VERSION
                init_result = await self._request("initialize", {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-plugin", "version": "0.1.0"},
                })

                server_info = init_result.get("serverInfo", {})
                logger.debug(
                    "mcp_initialized server=%s remote=%s ver=%s proto=%s",
                    self.config.name,
                    server_info.get("name", "unknown"),
                    server_info.get("version", ""),
                    init_result.get("protocolVersion", ""),
                )

                # Send initialized notification
                await self._notify("notifications/initialized", {})

                # Discover tools
                tools_result = await self._request("tools/list", {})
                self._tools_cache = [
                    {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "inputSchema": t.get("inputSchema", {"type": "object", "properties": {}}),
                    }
                    for t in tools_result.get("tools", [])
                ]

                self._status = ServerStatus.CONNECTED
                elapsed = (_time.monotonic() - t0) * 1000
                logger.info(
                    "mcp_connected server=%s tools=%d took_ms=%.0f",
                    self.config.name, len(self._tools_cache), elapsed,
                )

                # Start the health monitor once per connection object — a running
                # monitor is reused across reconnects, so never spawn a second.
                # A disconnect() that landed mid-connect must not leave an
                # orphaned monitor pinging a transport that is being torn down.
                if self._disconnecting:
                    self._status = ServerStatus.DISCONNECTED
                    return

                # A connect (first or reconnect) can mean the server's tool surface
                # appeared or changed — notify listeners so they re-discover
                # and re-register (idempotent full-diff registration).
                await self._fire_on_reconnect()

                if self._health_task is None or self._health_task.done():
                    self._health_task = asyncio.create_task(self._health_monitor())

                # Run post-connect setup (best-effort, never blocks on failure)
                await self._post_connect_setup()

            except asyncio.CancelledError:
                # A cancelled connect must not leave the status stuck in
                # CONNECTING — the monitor would skip it forever and call_tool
                # would raise "not connected (connecting)" with no recovery.
                # Reset to DISCONNECTED so both paths retry.
                if self._status == ServerStatus.CONNECTING:
                    self._status = ServerStatus.DISCONNECTED
                # An externally-timed-out connect (e.g. a tool-call wait_for)
                # must not leak its transport: the spawned npx/uvx process,
                # http client and stderr relay would
                # otherwise keep running — and cold-starting on a slow machine
                # — after the caller gave up.  The Exception path already
                # cleans up; cancellation now does too.
                await self._cleanup_resources()
                raise

            except Exception as e:
                self._status = ServerStatus.FAILED
                # A failed INITIAL connect means the caller (mcp_set /
                # auto-connect) saw the failure and skipped tool registration
                # — the health monitor's eventual recovery must notify
                # listeners so the tools still get registered.
                if not self._ever_connected:
                    self._notify_on_next_success = True
                stderr_tail = "".join(self._stderr_buffer[-20:]).strip()
                if stderr_tail:
                    self._error = f"{e}\n\n[server stderr]\n{stderr_tail}"
                else:
                    self._error = str(e)
                logger.exception("mcp_connect_failed server=%s err=%s", self.config.name, e)
                await self._cleanup_resources()
                # Start the health monitor even on a failed initial connect so a
                # server that was down at startup is retried in the background
                # (the monitor's DISCONNECTED/FAILED branch handles it).  When
                # connect() was called by the monitor itself, this is a no-op —
                # the running monitor is still current.
                if self.config.enabled and (
                    self._health_task is None or self._health_task.done()
                ):
                    self._health_task = asyncio.create_task(self._health_monitor())

    async def _fire_on_reconnect(self) -> None:
        """Notify listeners that the server is connected.

        Fires on EVERY successful connect (first and reconnects).  A server
        connecting asynchronously from mcp-plugin.json5 — the standalone
        startup path — must also reach registered listeners; full-diff
        registration on the listener side keeps this idempotent.  Best-effort:
        a failing listener never breaks the connection.
        """
        self._ever_connected = True
        self._notify_on_next_success = False
        logger.info("mcp_connected server=%s", self.config.name)
        if self._on_connected is not None:
            try:
                await self._on_connected(self.config.name)
            except Exception:
                logger.exception(
                    "mcp_on_connected_failed server=%s",
                    self.config.name,
                )

    async def _connect_stdio(self) -> None:
        """Spawn server as subprocess and set up pipe I/O."""
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
            from mcp_plugin.os_detect import get_os_accessible_paths
            for p in get_os_accessible_paths():
                resolved_args += ["--allow-path", p]

        self._process = await asyncio.create_subprocess_exec(
            exe, *resolved_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group on POSIX so teardown can kill the whole
            # tree (npx/uvx spawn the real server as a grandchild).  Without
            # this, kill_process_tree's killpg check sees the shared group
            # and can only kill the direct child — the grandchild survives,
            # holds stdout/stderr open, and process.wait() in teardown never
            # resolves (WSL hang).  Mirrors slife.tools.exec's spawns.
            start_new_session=True,
            env=env or None,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _connect_http(self) -> None:
        """Create HTTP client; detect SSE vs Streamable HTTP.

        Tries SSE first (GET the URL with ``Accept: text/event-stream``).
        If the server responds with SSE, enters SSE mode — otherwise
        falls back to Streamable HTTP (POST JSON-RPC directly).
        """
        # Resolve ${VAR} references in URL (e.g. SSE URL with API key)
        self._resolved_url = _resolve_embedded_refs(self.config.url)

        # Resolve ${VAR} references in headers
        headers: dict[str, str] = {}
        if self.config.headers:
            headers.update(
                {k: _resolve_embedded_refs(v) for k, v in self.config.headers.items()}
            )

        # No read/write timeout of our own — enforcement lives in the agent
        # loop's tool_timeout (per the timeout architecture).  A fixed 30s
        # here would abort a legitimately slow external tool call and spuriously
        # trigger the reconnect path.  Only connect/pool are bounded so a dead
        # endpoint can't hang the handshake; ping carries its own
        # 5s wait_for.
        self._http_client = httpx2.AsyncClient(
            headers=headers,
            timeout=httpx2.Timeout(connect=10.0, read=None, write=None, pool=10.0),
        )

        # Detect SSE: send GET with Accept: text/event-stream
        url = self._resolved_url.rstrip("/")
        sse_detected = False

        # The detection GET doubles as the persistent SSE stream.  Once
        # handed to ``_read_sse_stream`` the response is owned by that task
        # (it closes it), so it must stay open here — a context-managed
        # ``stream()`` would ``aclose()`` it on exit and kill the reader
        request = self._http_client.build_request(
            "GET", url,
            headers={"Accept": "text/event-stream", **headers},
        )
        resp: httpx2.Response | None = None
        try:
            resp = await self._http_client.send(request, stream=True)
            if (
                resp.status_code == 200
                and "text/event-stream" in resp.headers.get("content-type", "")
            ):
                self._sse_mode = True
                self._sse_queue = asyncio.Queue()
                self._sse_task = asyncio.create_task(
                    self._read_sse_stream(resp)
                )
                # Wait for the endpoint event to discover the POST URL.  Protocol period —
                # no client timer: a dead SSE stream is the server's problem,
                # and the health monitor treats the connection as unresponsive
                # and reconnects rather than racing a fixed connect timeout.
                endpoint = await self._sse_queue.get()
                if endpoint.get("type") != "endpoint":
                    raise ConnectionError(
                        f"Expected endpoint event, got {endpoint.get('type')}"
                    )
                # Resolve the message endpoint.  Servers may send it as a
                # bare URL/path or as a JSON object
                # {"uri": ..., "type": "endpoint"}; prefer the parsed ``uri``
                # when present.
                ep = None
                msg = endpoint.get("msg")
                if isinstance(msg, dict):
                    ep = msg.get("uri")
                if not ep:
                    ep = endpoint["data"]
                if ep.startswith("/"):
                    from urllib.parse import urlparse
                    parsed = urlparse(url)
                    ep = f"{parsed.scheme}://{parsed.netloc}{ep}"
                self._sse_message_url = ep
                logger.info(
                    "mcp_sse_connected server=%s msg_url=%s",
                    self.config.name, self._sse_message_url,
                )
                sse_detected = True
                # ``resp`` stays open — owned by ``_read_sse_stream`` and
                # closed there (or by its cancellation in cleanup).
                return
            else:
                # Non-200 or wrong content-type — drain the response body
                # (bounded) so the httpx2 connection is returned to the pool,
                # then fall through to streamable HTTP.  aclose() without
                # reading leaks the connection on every reconnect.
                try:
                    _n = 0
                    async for _chunk in resp.aiter_bytes():
                        _n += len(_chunk)
                        if _n > 65536:
                            break
                except Exception:
                    pass
                logger.debug(
                    "mcp_sse_not_detected server=%s status=%d content_type=%s",
                    self.config.name, resp.status_code,
                    resp.headers.get("content-type", ""),
                )
        except Exception:
            if self._sse_task:
                self._sse_task.cancel()
                try:
                    await self._sse_task
                except asyncio.CancelledError:
                    pass
                self._sse_task = None
            self._sse_queue = None
            self._sse_mode = False
        finally:
            if not sse_detected and resp is not None:
                # Streamable-HTTP fallthrough: release the detection
                # response; the client (carrying user headers) is reused
                # for subsequent POST requests.
                await resp.aclose()

        logger.debug(
            "mcp_streamable_http server=%s url=%s",
            self.config.name, url,
        )

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

            # Already installed — nothing to do
            if os.path.isdir(os.path.join(jsdir, "node_modules")):
                logger.debug("fetch_npm_skip reason=node_modules_present")
                return

            logger.info(
                "fetch_npm_install jsdir=%s", jsdir,
            )
            npm_cmd = ["cmd", "/c", "npm", "install"]
            install = _subprocess.run(
                npm_cmd, cwd=jsdir,
                capture_output=True, text=True, timeout=60,
            )
            if install.returncode == 0:
                logger.info("fetch_npm_install_done jsdir=%s", jsdir)
            else:
                logger.debug(
                    "fetch_npm_install_fail rc=%d stderr=%s",
                    install.returncode, install.stderr[:200],
                )
        except Exception:
            # Best-effort — never let setup failure block the connection
            pass

    async def _request(self, method: str, params: dict, timeout: float | None = None) -> dict:
        """Send a JSON-RPC request and wait for the response.

        *timeout* bounds the SSE response wait (``None`` = no inner bound —
        enforcement lives in the agent loop's tool_timeout for tool calls, or
        the caller's ``wait_for`` for the connect handshake).
        """
        async with self._lock:
            self._next_id += 1
            req_id = self._next_id

            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }

            if self.config.transport == "stdio":
                return await self._request_stdio(request, req_id)
            elif self._sse_mode:
                return await self._request_sse(request, timeout=timeout)
            else:
                return await self._request_http(request)

    async def _request_stdio(self, request: dict, req_id: int) -> dict:
        """Send JSON-RPC over subprocess pipes and wait for matching response."""
        assert self._process and self._process.stdin and self._process.stdout
        line = json.dumps(request, ensure_ascii=False) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()

        while True:
            resp_line = await self._process.stdout.readline()
            if not resp_line:
                raise ConnectionError(f"Server '{self.config.name}' closed connection")

            try:
                response = json.loads(resp_line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                logger.debug("mcp_invalid_json server=%s line=%.100s", self.config.name, resp_line)
                continue

            if response.get("id") == req_id:
                if "error" in response:
                    raise Exception(
                        f"MCP error from '{self.config.name}': {response['error']}"
                    )
                return response.get("result", {})

    async def _read_sse_stream(self, response) -> None:
        """Read SSE events from *response* and push JSON-RPC messages
        into ``_sse_queue``.

        Owns ``response`` — it is closed here when the stream ends or the
        task is cancelled, never by the caller.
        """
        import json as _json
        event_type = ""
        data_buffer = ""
        try:
            async for line in response.aiter_lines():
                if line == "":
                    # Blank line ends the current event.
                    if data_buffer:
                        try:
                            msg = _json.loads(data_buffer)
                        except ValueError:
                            # Non-JSON payload (e.g. a bare endpoint URL) —
                            # pass through the raw text.
                            msg = data_buffer
                        entry = {"type": event_type, "data": data_buffer, "msg": msg}
                        if self._sse_queue:
                            await self._sse_queue.put(entry)
                    event_type = ""
                    data_buffer = ""
                    continue
                if line.startswith(":"):
                    continue  # SSE comment
                # Accept both "field: value" and "field:value" (the space after
                # the colon is optional per the SSE spec); multi-line data
                # joins with a newline.
                if ":" in line:
                    field, _, value = line.partition(":")
                    value = value.lstrip()
                else:
                    field, value = line, ""
                if field == "event":
                    event_type = value
                elif field == "data":
                    data_buffer = f"{data_buffer}\n{value}" if data_buffer else value
        except Exception as e:
            logger.debug("sse_stream_closed server=%s err=%s", self.config.name, e)
            if self._sse_queue:
                await self._sse_queue.put(
                    {"type": "error", "data": str(e), "msg": {}}
                )
        finally:
            try:
                await response.aclose()
            except Exception:
                pass

    async def _request_sse(self, request: dict, timeout: float | None = None) -> dict:
        """Send JSON-RPC via POST to SSE message endpoint, wait for
        matching response on the SSE event stream.

        *timeout* bounds the response wait; ``None`` waits indefinitely and the
        caller (agent-loop tool_timeout / connect-handshake wait_for) governs —
        a fixed 30s here failed legitimate SSE tool calls that ran longer.
        """
        assert self._http_client is not None
        assert self._sse_queue is not None

        # POST the request to the SSE message endpoint
        post_resp = await self._http_client.post(
            self._sse_message_url,
            json=request,
            headers={"Content-Type": "application/json"},
        )
        if post_resp.status_code not in (200, 202):
            post_resp.raise_for_status()

        req_id = request["id"]
        # Wait for the matching JSON-RPC response on the SSE stream
        while True:
            if timeout is None:
                entry = await self._sse_queue.get()
            else:
                entry = await asyncio.wait_for(self._sse_queue.get(), timeout=timeout)
            if entry["type"] == "error":
                raise ConnectionError(
                    f"SSE stream closed for '{self.config.name}': {entry['data']}"
                )
            msg = entry.get("msg", {})
            if isinstance(msg, dict) and msg.get("id") == req_id:
                if "error" in msg:
                    raise Exception(
                        f"MCP error from '{self.config.name}': {msg['error']}"
                    )
                return msg.get("result", {})

    async def _request_http(self, request: dict) -> dict:
        """Send JSON-RPC via HTTP POST and parse the response.

        Handles both response shapes the MCP Streamable HTTP spec allows:
        a single ``application/json`` body, or an SSE stream
        (``text/event-stream``) whose first message is the JSON-RPC
        response and whose later messages are server-initiated
        notifications (dropped — this connection is request/response).
        """
        assert self._http_client is not None

        headers = {}
        if self._session_id:
            headers["mcp-session-id"] = self._session_id

        url = getattr(self, '_resolved_url', self.config.url)
        try:
            resp = await self._http_client.post(
                url, json=request, headers=headers,
            )
            resp.raise_for_status()
        except httpx2.HTTPError as e:
            raise ConnectionError(
                f"HTTP error from '{self.config.name}': {e}"
            ) from e

        # Extract session ID from response header (first initialize response)
        sid = resp.headers.get("mcp-session-id")
        if sid and not self._session_id:
            self._session_id = sid

        if "text/event-stream" in resp.headers.get("content-type", ""):
            response = await self._read_streamable_sse_response(
                resp, request["id"],
            )
        else:
            try:
                response = resp.json()
            except ValueError as e:
                raise ConnectionError(
                    f"Invalid JSON from '{self.config.name}': {e}"
                ) from e

        if "error" in response:
            raise Exception(
                f"MCP error from '{self.config.name}': {response['error']}"
            )
        return response.get("result", {})

    async def _read_streamable_sse_response(
        self, response, req_id: int,
    ) -> dict:
        """Extract the JSON-RPC response from a streamable SSE response.

        A Streamable HTTP server may stream the POST response as
        ``text/event-stream``.  The first ``data:`` event whose JSON-RPC
        message carries ``req_id`` is the response; other events
        (notifications, non-matching messages) are skipped.  Owns
        ``response`` — closed here in all cases.
        """
        data_buffer = ""
        try:
            async for line in response.aiter_lines():
                if line == "":
                    # Blank line ends the current event.
                    if data_buffer:
                        try:
                            msg = json.loads(data_buffer)
                        except ValueError:
                            data_buffer = ""  # non-JSON data event — skip
                            continue
                        if isinstance(msg, dict) and msg.get("id") == req_id:
                            return msg
                    data_buffer = ""  # notification / other event — drop
                    continue
                if line.startswith(":"):
                    continue  # SSE comment
                # Accept "data: value" and "data:value"; join multi-line data
                # with a newline.
                if ":" in line:
                    field, _, value = line.partition(":")
                    value = value.lstrip()
                else:
                    field, value = line, ""
                if field == "data":
                    data_buffer = f"{data_buffer}\n{value}" if data_buffer else value
            raise ConnectionError(
                f"Streamable SSE response from '{self.config.name}' "
                f"carried no response for id={req_id}"
            )
        finally:
            try:
                await response.aclose()
            except Exception:
                pass

    async def _notify(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        if self.config.transport == "stdio":
            assert self._process and self._process.stdin
            line = json.dumps(notification, ensure_ascii=False) + "\n"
            self._process.stdin.write(line.encode("utf-8"))
            await self._process.stdin.drain()
        elif self._sse_mode:
            assert self._http_client is not None
            task = asyncio.create_task(
                self._http_client.post(
                    self._sse_message_url,
                    json=notification,
                    headers={"Content-Type": "application/json"},
                )
            )
            # Track so teardown cancels them — a closed client would otherwise
            # surface unretrieved task exceptions.
            self._notify_tasks.add(task)
            task.add_done_callback(self._notify_tasks.discard)
        else:
            assert self._http_client is not None
            headers: dict[str, str] = {}
            if self._session_id:
                headers["mcp-session-id"] = self._session_id
            url = getattr(self, '_resolved_url', self.config.url)
            task = asyncio.create_task(
                self._http_client.post(
                    url, json=notification, headers=headers,
                )
            )
            self._notify_tasks.add(task)
            task.add_done_callback(self._notify_tasks.discard)

    async def _drain_stderr(self) -> None:
        assert self._process and self._process.stderr
        from mcp_plugin.logging import read_stderr_lines, sanitize_secrets
        try:
            # Shared hardened reader — an over-long stderr line must not
            # kill this relay (a dead relay wedges the server's stderr
            # pipe and the server blocks on its next log write).
            async for text in read_stderr_lines(self._process):
                self._stderr_buffer.append(text + "\n")
                # Bound the buffer — only the tail is ever read (the last
                # 20 lines in connect()'s error path); a chatty server must
                # not grow it without bound.
                if len(self._stderr_buffer) > 500:
                    del self._stderr_buffer[:len(self._stderr_buffer) - 500]
                logger.debug("mcp_stderr server=%s line=%s", self.config.name, sanitize_secrets(text))
        except asyncio.CancelledError:
            pass

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
            # deliberately-disconnected server.  The monitor is the only task
            # that reconnects, so cancelling it here (and only here) prevents
            # self-cancellation from ``_cleanup_resources``.
            if self._health_task is not None and not self._health_task.done():
                self._health_task.cancel()
                try:
                    await self._health_task
                except asyncio.CancelledError:
                    pass
                self._health_task = None
            await self._cleanup_resources()
            self._tools_cache = []
            self._session_id = None
        self._disconnecting = False
        logger.info("mcp_disconnected server=%s", self.config.name)

    async def _cleanup_resources(self) -> None:
        # -- stdio cleanup --
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        self._stderr_task = None

        if self._process and self._process.stdin:
            try:
                self._process.stdin.write(b'')
                await self._process.stdin.drain()
            except Exception:
                pass

        if self._process is not None:
            # terminate_process kills only the direct child — npx/uvx spawn
            # grandchildren that outlive it on Windows.  Kill the whole tree
            # first, then terminate_process cleans up the pipe transports
            await kill_process_tree(self._process)
            await terminate_process(self._process, label=f"mcp_conn:{self.config.name}")
        self._process = None

        # -- sse cleanup --
        if self._sse_task and not self._sse_task.done():
            self._sse_task.cancel()
            try:
                await self._sse_task
            except asyncio.CancelledError:
                pass
        self._sse_task = None
        self._sse_queue = None
        self._sse_message_url = ""
        self._sse_mode = False

        # -- http cleanup --
        # Cancel in-flight fire-and-forget notifications before closing the
        # client — a closed client would surface unretrieved task exceptions.
        for task in list(self._notify_tasks):
            task.cancel()
        self._notify_tasks.clear()
        if self._http_client is not None:
            # Best-effort session termination
            if self._session_id:
                try:
                    url = getattr(self, '_resolved_url', self.config.url)
                    await self._http_client.delete(
                        url,
                        headers={"mcp-session-id": self._session_id},
                    )
                except Exception:
                    pass
            await self._http_client.aclose()
            self._http_client = None

    def list_tools(self) -> list[dict]:
        return list(self._tools_cache)

    async def ping(self, timeout: float = _HEALTH_PING_TIMEOUT) -> bool:
        """Return True if the server answers a JSON-RPC ping.

        Used by the background health monitor.  A died or hung server (stdio
        process that stopped answering, or an HTTP/SSE endpoint that times
        out) makes this return False — the monitor then marks it DISCONNECTED
        and reconnects.
        """
        if self._status != ServerStatus.CONNECTED:
            return False
        try:
            await asyncio.wait_for(self._request("ping", {}), timeout=timeout)
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
            """Attempt a reconnect; returns True on success. On failure the
            backoff is NOT advanced here — the caller advances it after
            deciding the wait."""
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
                # Healthy paths wait the full check interval; a failed
                # reconnect sleeps ONLY its backoff (5s→60s) instead of
                # interval + backoff, so a down server isn't polled ~30s
                # later than the documented backoff promises.
                wait = _HEALTH_CHECK_INTERVAL
                if not self.config.enabled:
                    return
                if self._status == ServerStatus.CONNECTING:
                    pass  # a manual connect is already in progress — wait
                elif self._status == ServerStatus.CONNECTED:
                    if self._lock.locked():
                        pass  # a request is in flight — don't interrupt it
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
            # the server is enabled, try a lazy reconnect first — it may have
            # recovered while the monitor's reconnect backoff was counting
            # down.  Connecting IS the probe, so nothing gates this but the
            # enabled flag.
            if self.config.enabled and self._status == ServerStatus.DISCONNECTED:
                try:
                    await self.connect()
                except Exception:
                    pass
            if self._status != ServerStatus.CONNECTED:
                raise ValueError(
                    f"Server '{self.config.name}' is not connected "
                    f"(status: {self._status.value})"
                )

        logger.debug("mcp_tool_call server=%s tool=%s", self.config.name, tool_name)

        try:
            result = await self._request("tools/call", {
                "name": tool_name,
                "arguments": arguments,
            })
        except (ConnectionError, OSError):
            # Transport error — the server may have died.
            # Attempt one reconnect before giving up.
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
                # Retry
                result = await self._request("tools/call", {
                    "name": tool_name,
                    "arguments": arguments,
                })
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

        # Format content blocks
        parts: list[str] = []
        for block in result.get("content", []):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "resource":
                parts.append(f"[resource: {block.get('resource', {})}]")
            else:
                parts.append(json.dumps(block))
        return "\n".join(parts) if parts else json.dumps(result)


class ConnectionPool:
    """Manages a collection of MCP server connections."""

    def __init__(self, on_connected: Callable[[str], Awaitable[None]] | None = None):
        self._connections: dict[str, MCPServerConnection] = {}
        # Fired on every successful RECONNECT of any server (never on the
        # first connect — see MCPServerConnection.connect).  The wrapper wires
        # this to a tools/list_changed notification so the agent re-syncs.
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
                "tool_count": conn.tool_count, "error": conn.error,
                "transport": conn.config.transport,
                "command": conn.config.command, "args": conn.config.args,
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
        logger.info("mcp_shutdown servers=%d", len(self._connections))
        for name in list(self._connections.keys()):
            await self.remove_server(name)
        logger.info("mcp_shutdown_done servers=%d", len(self._connections))
