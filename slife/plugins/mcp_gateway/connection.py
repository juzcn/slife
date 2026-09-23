"""MCP connection pool — persistent connections to external MCP servers.

Supports three transports via the **official ``mcp`` SDK** (the same
mechanism ``client.MCPClient`` uses to reach slife's own plugin children):
  - stdio:           spawn server as subprocess, JSON-RPC over pipes
  - http (SSE):      GET /sse for server→client events, POST /messages for requests
  - http (streamable): POST JSON-RPC with mcp-session-id header

Every transport enters an ``AsyncExitStack`` that yields ``(read, write)``
streams for one ``mcp.ClientSession``.  This class supplies the connection
lifecycle the SDK does not: OAuth device flow, transport establishment and
re-establishment, stdio stderr relay, per-server connect locking, and the
review-driven ``needs_user_auth`` pause (F5).

**Health is a tool list, not a connection.**  A 2026-07-28 peer is stateless —
no session id, no handshake, per-request ``_meta`` — and that revision removed
``ping`` from the protocol outright: ``mcp_types``'s per-version method maps
carry no ``ping`` at 2026-07-28 in either direction, so a modern peer answers
``-32601 Method not found`` to one.  A probe that reads that as death tears
healthy servers down and respawns their children forever (1ba354e: eight
servers, 167 spawns in 11 minutes); a probe that reads it as life can never
report anything at all.  There is no third reading — so there is no probe.

What does answer the question is ``tools/list``, the very call the host's
reconcile already makes to feed the shared catalog (``tools.db``).  A list
that succeeds IS the health verdict and a list that fails IS the record, so
this module keeps a per-server **tool snapshot** instead of a connection state
machine.  The snapshot is re-read on the peer's own signals — a
``tools/list_changed`` event, a dead transport, a failed call — never on a
timer, and a failure is repaired by the failing request itself, the same
policy ``MCPClient`` already applies to plugin links (DESIGN.md).
"""

import asyncio
import json
import logging
import os
import subprocess as _subprocess
import tempfile
import time as _time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import httpx2

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from slife.mcp.era import negotiate_era, peer_era, watch_tools_changed
from slife.plugins.mcp_gateway.client import (
    _is_link_down,
    close_exit_stack_bounded,
    make_local_http_client,
)
from mcp.types import (
    TextContent,
    ImageContent,
)

from slife.plugins.mcp_gateway import __version__
from slife.plugins.mcp_gateway.config import _is_env_ref, _resolve_embedded_refs, _resolve_secret
from slife.platform import resolve_command
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

logger = logging.getLogger(__name__)

# ── Pacing ──────────────────────────────────────────────────────────────
# Cadence, not per-await budgets, so these stay local (DESIGN.md §4.7): the
# backoff profile of the one
# background job this module has — acquiring a tool list for a server that
# has none.  It stops the moment a list succeeds; a healthy server is never
# polled.  Deadlines it needs are the registry's (ready.connect_startup for
# establishment, ready.list_tools for one listing).
_REFRESH_RETRY_INITIAL = 5.0    # first retry delay (s)
_REFRESH_RETRY_MAX = 60.0       # cap on the exponential backoff (s)
_REFRESH_RETRY_MULTIPLIER = 2.0

# stdio stderr capture: poll interval for the errlog-file drain task, and how
# many lines of the tail an error path may read back.
_STDERR_POLL_INTERVAL = 0.05
_STDERR_BUFFER_LIMIT = 500


class NeedsUserAuthError(RuntimeError):
    """OAuth needs a human (device flow) — not a retriable transport failure.

    Carried out of the OAuth pre-check so a caller can tell a "give it 5
    seconds and retry" failure from a "someone must approve this" failure.
    The former is recorded and retried in the background; the latter stops
    auto-repair until the user re-adds the server (F5).
    """


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
    #: Provenance: where this server definition came from, e.g.
    #: ``{"type": "github", "url": ..., "version": ...}`` — the ``type`` is the
    #: DOWNLOAD SOURCE (registry/file/hand), never a category.  Never written
    #: by us and never overwritten.
    source: dict | None = None
    #: Which family this entry belongs to — derived from the config SECTION it
    #: lives in (``rest-api`` vs ``mcp.servers``); not a config key.  The two
    #: are different things that share this pool's plumbing, not one thing with
    #: a flag.
    rest_api: bool = False
    os_paths: bool = False  # inject --allow-path from the OS-accessible path set
    auto_load: bool = False  # True = host bulk-registers this server's tools on connect

    @property
    def transport(self) -> str:
        """Return the transport mode: 'http' or 'stdio'."""
        return "http" if self.url else "stdio"


class MCPServerConnection:
    """One external MCP server: its transport, its session, its tool list.

    Owns the lifecycle the SDK does not — OAuth, transport selection and
    re-establishment, the stdio stderr relay, connect locking, and the
    review-driven ``needs_user_auth`` pause.  All protocol is the mcp SDK's
    ``ClientSession``; no hand-rolled JSON-RPC (E2).

    The live state it publishes is the :meth:`snapshot` of its last tool
    list.  Whether a transport happens to be up between calls is not a fact
    anyone needs: a stateless peer is reachable or it is not, and the next
    request is what finds out.
    """

    def __init__(
        self,
        config: ServerConfig,
        on_tools_changed: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.config = config
        self._on_tools_changed = on_tools_changed
        self._session: ClientSession | None = None
        self._exit_stack: AsyncExitStack | None = None
        #: Protocol version negotiated at establishment (None before) — a
        #: modern peer is session-less and pushes changes on a listen stream
        #: only.
        self._era: str | None = None
        #: Supervisor for that listen stream (modern peers only).
        self._watch_task: "asyncio.Task | None" = None
        self._http_client: httpx2.AsyncClient | None = None
        self._sse_mode: bool = False
        # stdio stderr capture: a real temp file passed as the SDK's errlog
        # (the child writes straight to disk), drained by _drain_stderr into
        # the ring buffer below.  The file handle is the capture's source of
        # truth on every platform — an in-memory TextIO cannot be a child's
        # stderr (the subprocess machinery needs a real fd).
        self._stderr_dump: Any | None = None
        self._stderr_task: "asyncio.Task | None" = None
        self._stderr_buffer: list[str] = []
        # ── The tool snapshot: the live state this class publishes ──────
        self._tools: list[dict] = []
        self._tools_fetched_at: float | None = None   # monotonic; None = never listed
        self._tools_ttl_ms: int = 0                   # the peer's cache hint (0 = none)
        self._tools_stale: bool = False               # the peer said the list changed
        self._last_error: str | None = None           # last failed read/call
        self._connect_lock = asyncio.Lock()  # serializes session establishment
        # Set by disconnect() so an in-flight attempt aborts at its next check
        # point instead of resuming after cleanup and leaving an orphaned
        # transport behind.
        self._disconnecting = False
        # The session under us died (the SDK delivered the transport fault, or
        # a send was refused on a transport already gone).  Consumed under
        # _connect_lock by the next establishment, which tears the corpse down
        # first — see _handle_notification's note on why the fault is not
        # handled inline.
        self._link_dead = False
        # Background re-list — see _refresh_until_listed.  Started only while
        # no working tool list is held; cancelled by disconnect().
        self._refresh_task: "asyncio.Task | None" = None
        # OAuth: the refresh token was revoked / never granted and the device
        # flow needs a human.  While set, nothing auto-repairs the server (a
        # device-flow prompt must never be raised by a background retry, F5).
        # Cleared by a fresh mcp_set / mcp_remove.
        self._needs_user_auth: bool = False

    # ── Published state ─────────────────────────────────────────────────

    @property
    def needs_user_auth(self) -> bool:
        return self._needs_user_auth

    @property
    def error(self) -> str | None:
        return self._last_error

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    @property
    def era(self) -> str | None:
        """``"modern"`` / ``"legacy"`` — the negotiated generation, or None."""
        if self._session is None:
            return None
        return peer_era(self._session)

    @property
    def tools_ok(self) -> bool:
        """True when a working tool list is held — this link's health verdict."""
        return self._tools_fetched_at is not None and self._last_error is None

    def has_tools(self) -> bool:
        """True when a tool list is held (it may be stale, or from a dead peer)."""
        return self._tools_fetched_at is not None

    def list_tools(self) -> list[dict]:
        return list(self._tools)

    def snapshot(self) -> dict:
        """This server's raw live facts — one ``__check`` row.

        Facts, no verdict: the harness interprets them into health levels
        (DESIGN.md §Health — a plugin ``__check`` has no levels of its own),
        and it never connects, so ``tools_age_s`` is whatever the last real
        read left behind.  The config view is ``list_configured``'s; a fact
        belongs in exactly one place.
        """
        return {
            "name": self.config.name,
            "transport": self.config.transport,
            "era": self.era,
            "enabled": self.config.enabled,
            "tools_ok": self.tools_ok,
            "tool_count": len(self._tools),
            "tools_age_s": (
                None if self._tools_fetched_at is None
                else round(_time.monotonic() - self._tools_fetched_at, 1)
            ),
            "last_error": self._last_error,
            "needs_user_auth": self._needs_user_auth,
            #: Whether this peer's transport is established.  The fact that
            #: tells a settled failure from a pending one: no list + no
            #: transport is a server that could NOT come up (unavailable),
            #: while no list + a live transport is one that has simply not
            #: been read yet — a slow first ``tools/list`` whose retry is
            #: already armed.  Without it the two are the same row.
            "reachable": self._session is not None,
            "source": self.config.source,
            # The category the harness splits its health report by — derived
            # from the config section, so the host never has to read a tag out
            # of ``source`` (that one names a download origin).
            "rest_api": self.config.rest_api,
        }

    def _record_error(self, err: BaseException) -> None:
        """Record *err* as this server's failure — the fact ``__check`` reports.

        The stdio child's own stderr rides along: a spawn that dies at import
        time says why there and nowhere else.
        """
        stderr_tail = "".join(self._stderr_buffer[-20:]).strip()
        self._last_error = (
            f"{err}\n\n[server stderr]\n{stderr_tail}" if stderr_tail else str(err)
        )

    def _needs_fetch(self) -> bool:
        """Whether the held list must be re-read before it can be trusted.

        The peer's own ``ttlMs`` decides, when it gives one (the modern wire
        requires it): a list still inside its window is served as-is, which is
        what keeps a chatty peer's notification bursts from re-fetching a
        1239-tool payload on every event.  A legacy peer sends no hint, and
        its change events are explicit, so its snapshot stands until one
        arrives.
        """
        if self._tools_fetched_at is None or self._tools_stale:
            return True
        if self._tools_ttl_ms <= 0:
            return False
        age_ms = (_time.monotonic() - self._tools_fetched_at) * 1000.0
        return age_ms >= self._tools_ttl_ms

    def _mark_tools_stale(self, reason: str) -> None:
        """Flag the held list as possibly superseded (a change event, a rebuild)."""
        self._tools_stale = True
        logger.debug("mcp_tools_stale server=%s reason=%s", self.config.name, reason)

    # ── OAuth ──────────────────────────────────────────────────────────

    async def _ensure_oauth_token(self) -> None:
        """Obtain or refresh an OAuth token and inject it into connection headers.

        Called before transport connect when ``config.auth.type == "oauth"``.
        Mutates ``self.config.headers`` in place — the transport layer
        picks up the token automatically.

        When the token is gone AND the refresh failed, the ONLY way forward is
        the interactive device flow.  That needs a human at the desktop — it
        must run once, on a real (re)connect attempt the user drives, never on
        a background retry cycle.  So after a failed device flow the connection
        is marked :attr:`needs_user_auth` and nothing auto-repairs it (F5).
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
                    # do NOT re-run it (a retry would otherwise pop a fresh
                    # desktop prompt every cycle, F5).  Surface the state for
                    # call_tool / mcp_set_enabled instead.
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
                    # Still needs the user — keep the flag so nothing retries.
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

        # errlog must be a REAL file: the SDK hands it verbatim to the child
        # as its stderr handle (anyio → asyncio → subprocess → msvcrt
        # get_osfhandle needs a real fd; an in-memory TextIO raises
        # ``io.UnsupportedOperation`` on Windows and POSIX alike).  A temp
        # file also means a stalled drain can never wedge the child on a full
        # pipe — it writes straight to disk, we poll the appended bytes.
        self._stderr_dump = tempfile.TemporaryFile(mode="w+b")
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        read_stream, write_stream = await self._exit_stack.enter_async_context(
            # SDK types errlog as TextIO; a real (binary) file is what it
            # actually needs to hand the child a stderr handle.
            stdio_client(params, errlog=self._stderr_dump),  # type: ignore[arg-type]
        )
        self._session = await self._exit_stack.enter_async_context(
            self._new_session(read_stream, write_stream),
        )

    async def _drain_stderr(self) -> None:
        """Poll the errlog temp file into the ring buffer + DEBUG log.

        The child holds a duplicated handle to the same OS file and appends
        as it logs (never overwrites), so this monotonic read at our own
        offset is always current — no stale buffering.  Encapsulates the
        relay's guarantees: the buffer is bounded, over-long bytes are
        capped per line, and secrets are scrubbed before logging.
        """
        dump = self._stderr_dump
        if dump is None:
            return
        # Raw unbuffered layer — sees each poll's on-disk bytes directly.
        raw = getattr(dump, "raw", dump)
        position = 0
        from slife.logfmt import sanitize_secrets
        try:
            while True:
                await asyncio.sleep(_STDERR_POLL_INTERVAL)
                try:
                    raw.seek(position)
                    chunk = raw.read()
                except (OSError, ValueError):
                    # File closed/seek off EOF (e.g. poll raced teardown).
                    return
                if not chunk:
                    continue
                position += len(chunk)
                text = chunk.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                self._stderr_buffer.append(text + "\n")
                if len(self._stderr_buffer) > _STDERR_BUFFER_LIMIT:
                    del self._stderr_buffer[: len(self._stderr_buffer) - _STDERR_BUFFER_LIMIT]
                logger.debug(
                    "mcp_stderr server=%s line=%s",
                    self.config.name, sanitize_secrets(text),
                )
        except asyncio.CancelledError:
            pass

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
        except asyncio.TimeoutError:
            # The OUTER establishment asyncio.timeout has already fired — every
            # subsequent await inside that context re-raises immediately, so
            # the Streamable-HTTP fallback below could never succeed.  Re-raise
            # so ensure_session() handles a genuinely slow/hung SSE endpoint as
            # a failure, not as "SSE unsupported, try the other transport".
            raise
        except Exception:
            # SSE not supported — release anything the failed enter opened and
            # retry as Streamable HTTP.  The SDK reuses a pre-built httpx2
            # client, so build one lazily here (with the OAuth/resolved
            # headers riding along); the SSE-success path never allocates it.
            if self._exit_stack is not None:
                # Lenient on the fallback: whatever aclose raises (the SDK's
                # task-group teardown, or a plain error from the failed SSE
                # attempt) must not abort the Streamable HTTP retry.  The
                # strict helper already swallows timeout/cancel-scope/
                # ExceptionGroup; a remaining RuntimeError is swallowed here.
                try:
                    await close_exit_stack_bounded(self._exit_stack, label="sse_fallback")
                except RuntimeError:
                    pass
                self._exit_stack = AsyncExitStack()
            if self._http_client is None:
                # Shared construction (timeouts + proxy-free).  No read/write
                # timeout of our own — enforcement lives in the agent loop's
                # tool_timeout (per the timeout architecture).  Only
                # connect/pool are bounded so a dead endpoint can't hang the
                # establishment; a request carries the caller's own bound.
                self._http_client = make_local_http_client(headers=headers)
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
            client_info=_Impl(name="mcp-gateway", version=__version__),
        )

    async def _handle_notification(self, message) -> None:
        """Take what the SDK surfaces on the session channel.

        Two kinds of thing arrive here.  A LEGACY peer's notifications — the
        modern era forbids pushing one the client did not ask for, so a modern
        peer's changes ride the listen stream instead
        (:meth:`_start_tools_watch`).  And, at either era, a transport-level
        exception, which is how the SDK reports that the link under us died
        (``mcp``'s ``IncomingMessage`` is exactly those two cases).

        That exception is the ONLY death signal a modern peer gives, and this
        handler used to drop it — leaving a dead session indistinguishable from
        a live one until something happened to call it.  It is not handled
        inline: this runs inside the session's own task group, and tearing that
        session down from within it desyncs the SDK's cancel-scope stack (the
        failure mode DESIGN.md records).  The fault is recorded, the teardown
        is deferred to the next establishment under the connect lock, and the
        host is told to re-reconcile so the catalog marks the failure now.
        """
        if self._disconnecting:
            return
        method = getattr(message, "method", None)
        if isinstance(method, str):
            if method == "notifications/tools/list_changed":
                # The peer says its list is superseded.  A level trigger: the
                # host re-reads the list rather than trusting a payload, so a
                # burst collapses into the reconcile's own coalescing.
                self._mark_tools_stale("tools/list_changed")
                await self._notify_tools_changed()
            return
        if isinstance(message, BaseException):
            self._link_dead = True
            self._record_error(message)
            logger.warning(
                "mcp_transport_fault server=%s err=%s action=rebuild",
                self.config.name, message,
            )
            await self._notify_tools_changed()
            self._start_refresh_task()

    async def _notify_tools_changed(self) -> None:
        """Tell listeners this server's tool surface may have changed.

        One funnel for every reason it can: a successful re-list, a
        ``tools/list_changed`` event at either era, a dead transport.  The
        listener's job is to re-read (``on_tools_changed`` → the host's
        reconcile → ``__mcp_list_tools`` → the shared catalog), never to trust a
        payload — which is what makes the missed-during-a-gap case harmless.
        """
        if self._on_tools_changed is None:
            return
        try:
            await self._on_tools_changed(self.config.name)
        except Exception as exc:
            logger.warning(
                "mcp_tools_changed_handler_failed server=%s err=%s",
                self.config.name, exc,
            )

    def _start_tools_watch(self) -> None:
        """Keep a ``tools/list_changed`` listen stream open (modern peers).

        A no-op for a legacy peer — it raises ``ListenNotSupportedError``
        inside the supervisor, which then returns; its notifications keep
        riding the session channel.
        """
        if self._watch_task is not None and not self._watch_task.done():
            return
        if self._session is None:
            return
        self._watch_task = asyncio.create_task(
            watch_tools_changed(self._session, self._on_listen_event,
                                link=self.config.name),
            name=f"mcp-listen:{self.config.name}",
        )

    async def _on_listen_event(self) -> None:
        """A listen-stream event — see :meth:`_handle_notification`."""
        self._mark_tools_stale("listen event")
        await self._notify_tools_changed()

    # ── Session lifecycle ───────────────────────────────────────────────

    async def ensure_session(self) -> bool:
        """Establish a transport + one negotiated ``ClientSession``.

        Idempotent and lock-serialized.  Never raises for an unreachable peer:
        the verdict belongs to whatever the caller wanted the session for (a
        tool list, a tool call), and the failure is recorded here for both.
        :class:`NeedsUserAuthError` is the exception — that is not an
        unreachable peer but a decision only a human can make, so it travels
        to the caller unchanged (F5).

        Returns True when a session is live.
        """
        async with self._connect_lock:
            if self._disconnecting:
                return False
            if self._link_dead:
                # The deferred half of _handle_notification: the corpse is
                # dropped here, in a task that is not the dying session's own.
                self._link_dead = False
                await self._cleanup_resources()
            if self._session is not None:
                return True

            # ── OAuth pre-check ───────────────────────────────────────
            try:
                if self.config.auth and self.config.auth.get("type") == "oauth":
                    await self._ensure_oauth_token()
                if self._disconnecting:
                    return False  # disconnect() ran mid-OAuth

                t0 = _time.monotonic()
                logger.info(
                    "mcp_connect server=%s transport=%s",
                    self.config.name, self.config.transport,
                )
                self._exit_stack = AsyncExitStack()
                # Transport establishment is the one client-owned wait (spawn
                # + socket/SSE setup).  asyncio.timeout, not wait_for: on
                # Windows/Proactor a stuck transport op can defeat wait_for's
                # cancellation and block past the deadline.
                async with asyncio.timeout(_timeouts.timeouts.ready.connect_startup):
                    if self.config.transport == "stdio":
                        await self._connect_stdio()
                    else:
                        await self._connect_http()

                    assert self._session is not None
                    # Era negotiation, not a bare handshake: an external
                    # server may be either generation, and the SDK's `auto`
                    # policy decides from the peer's own answer — modern peers
                    # are adopted through `server/discover` (session-less,
                    # per-request `_meta`), legacy peers keep the initialize
                    # handshake.  See slife/mcp/era.py.
                    self._era = await negotiate_era(self._session)
            except asyncio.CancelledError:
                # A cancelled establishment must not leave a half-open
                # transport behind for the next attempt to trip over.
                await self._cleanup_resources()
                raise
            except NeedsUserAuthError:
                await self._cleanup_resources()
                raise
            except Exception as e:
                self._record_error(e)
                logger.warning("mcp_connect_failed server=%s err=%s", self.config.name, e)
                await self._cleanup_resources()
                return False

            # A fresh session is a fresh peer: its tool list must be re-read
            # (the process may have restarted with a different surface).
            self._mark_tools_stale("new session")
            elapsed = (_time.monotonic() - t0) * 1000
            logger.info("mcp_session_ready server=%s took_ms=%.0f", self.config.name, elapsed)

            # A modern peer pushes later changes ONLY on a listen stream; a
            # legacy one keeps the session channel (_handle_notification).
            if peer_era(self._session) == "modern":
                self._start_tools_watch()

            # Run post-connect setup (best-effort, never blocks on failure)
            await self._post_connect_setup()
            return True

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
                capture_output=True, text=True, timeout=30,  # noqa-timeout — one-off dep bring-up (sync subprocess, not asyncio)
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
                capture_output=True, text=True, timeout=60,  # noqa-timeout — one-off dep bring-up (sync subprocess, not asyncio)
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

    # ── The tool list: the health check, and its record ─────────────────

    def _absorb(self, result: Any) -> None:
        """Store a successful listing (+ the peer's cache hint) as the snapshot."""
        self._tools = [
            {
                "name": t.name,
                "description": t.description or "",
                # input_schema is the canonical attr in mcp-types ≥2.0;
                # the proxy contract is the wire name (camelCase).
                "inputSchema": t.input_schema,
            }
            for t in result.tools
        ]
        self._tools_fetched_at = _time.monotonic()
        # The modern wire REQUIRES ttlMs; a legacy peer sends none and the
        # model default is 0, which reads as "no hint" here either way.
        self._tools_ttl_ms = int(getattr(result, "ttl_ms", 0) or 0)
        self._tools_stale = False
        self._last_error = None
        if getattr(result, "next_cursor", None):
            # One call, one page — the cursor is not followed (see the module
            # docstring).  A peer that paginates therefore contributes its
            # first page only, which must be visible in the log rather than
            # show up as tools that mysteriously do not exist.
            logger.warning(
                "mcp_tools_list_truncated server=%s tools=%d — the peer "
                "returned next_cursor; the remainder of its listing is not read",
                self.config.name, len(self._tools),
            )

    async def refresh_tools(self, *, force: bool = False) -> bool:
        """Read this server's tool list; True when a working list is held.

        This is the health check — there is no separate probe, because this is
        the call the host's reconcile makes anyway to feed the catalog.  A
        success stores the snapshot; a failure records it (:attr:`error`) and
        leaves the background re-list running.

        A list still inside the peer's ``ttlMs`` window is served from the
        snapshot without a request; ``force`` overrides that for the callers
        that know better (a rebuild, a repair pass).
        """
        if not self.config.enabled:
            # The rule ``call_tool`` already enforces, and the only operation
            # that was missing it: a switched-off server has no transport.
            # READING IS CONNECTING, so the read is where a switched-off server
            # used to come up anyway — one ``mcp_list_tools`` (or the host's
            # reconcile asking on the model's behalf) was enough to spawn it.
            self._record_error(ValueError(f"Server '{self.config.name}' is disabled"))
            return False
        if not force and not self._needs_fetch():
            return self.tools_ok

        for attempt in (1, 2):
            try:
                if not await self.ensure_session():
                    # ensure_session already recorded why.
                    self._start_refresh_task()
                    return False
                session = self._session
                assert session is not None  # post-condition of a True ensure_session
                async with asyncio.timeout(_timeouts.timeouts.ready.list_tools):
                    result = await session.list_tools()
            except asyncio.CancelledError:
                # A cancelled read (a host tool-timeout on mcp_set) must still
                # recover: the list was never read, so arm the retry — unless
                # this is a deliberate teardown, which disconnect() marks
                # first (and which refuses the arm for exactly this reason).
                self._start_refresh_task()
                raise
            except NeedsUserAuthError:
                raise
            except Exception as e:
                if attempt == 1 and _is_link_down(e):
                    # The dispatcher refused a send on a transport already
                    # gone — the request never reached the peer, which is what
                    # makes ONE rebuild-and-retry safe (the same rule
                    # ``MCPClient._request_with_recovery`` follows).
                    self._link_dead = True
                    continue
                self._record_error(e)
                logger.warning(
                    "mcp_tools_list_failed server=%s err=%s", self.config.name, e,
                )
                self._start_refresh_task()
                return False

            self._absorb(result)
            logger.info(
                "mcp_tools_listed server=%s tools=%d ttl_ms=%d",
                self.config.name, len(self._tools), self._tools_ttl_ms,
            )
            # A list is the moment the catalog's rows are known stale: tell
            # the host to re-read (it owns the catalog, not this process).
            await self._notify_tools_changed()
            return True

        raise AssertionError("unreachable")  # pragma: no cover

    def arm_refresh(self) -> None:
        """Arm the background re-list from outside the read path.

        The boot's spawn-only shape (:meth:`ConnectionPool.add_server`) is the
        one caller: it never reads, so it never sees the failure that arms the
        repair, and a peer that was down at boot would otherwise stay listless
        with nothing left to ask it.
        """
        self._start_refresh_task()

    def _start_refresh_task(self) -> None:
        """Ensure the background re-list runs — see :meth:`_refresh_until_listed`.

        Every closed path already calls this, so the retry is armed by the
        failure itself rather than by a monitor that has to guess when to look.
        """
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        if self._disconnecting or not self.config.enabled or self._needs_user_auth:
            return
        self._refresh_task = asyncio.create_task(
            self._refresh_until_listed(), name=f"mcp-refresh:{self.config.name}",
        )

    async def _refresh_until_listed(self) -> None:
        """Re-list until a working tool list is held, then stop.

        The ONLY background job this module has, and the one gap a
        request-driven policy cannot cover on its own: a server that is down
        when nobody is calling it has no tool list, and with no tool list it is
        absent from the catalog — so nothing would ever ask it again.  Forcing
        the read (``force=True``) is deliberate: this task exists precisely
        because the snapshot is not to be trusted, so the cache hint that
        would skip a fetch does not apply.

        A healthy server is never polled — the loop's own success ends it.
        """
        wait = _REFRESH_RETRY_INITIAL
        try:
            while True:
                if self._disconnecting or not self.config.enabled or self._needs_user_auth:
                    return
                if await self.refresh_tools(force=True):
                    return
                logger.debug(
                    "mcp_refresh_retry server=%s in=%.1fs", self.config.name, wait,
                )
                await asyncio.sleep(wait)
                wait = min(wait * _REFRESH_RETRY_MULTIPLIER, _REFRESH_RETRY_MAX)
        except NeedsUserAuthError:
            # The first attempt can discover that this server needs a human
            # (a device flow that did not complete).  The flag is set, nothing
            # may retry it (F5), and the state surfaces through __check.
            logger.info(
                "mcp_refresh_paused server=%s reason=needs_user_auth", self.config.name,
            )
        except asyncio.CancelledError:
            pass

    # ── Tool calls ──────────────────────────────────────────────────────

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call one tool on this server, rebuilding the link once if it died."""
        if self._needs_user_auth:
            raise NeedsUserAuthError(
                f"Server '{self.config.name}' needs OAuth re-authorization — "
                "use mcp_remove / mcp_set to re-add it and run the "
                "device flow again."
            )
        if not self.config.enabled:
            raise ValueError(f"Server '{self.config.name}' is not connected (disabled)")
        # Connecting IS the reachability check: a stateless peer either answers
        # or does not, and the call below is what finds out.
        if not await self.ensure_session():
            raise ValueError(
                f"Server '{self.config.name}' is not connected "
                f"({self._last_error or 'no session'})"
            )

        logger.debug("mcp_tool_call server=%s tool=%s", self.config.name, tool_name)

        try:
            result = await self._call_on_session(tool_name, arguments)
        except (ConnectionError, OSError):
            # The link died under the call.  Rebuild once and retry — the
            # failure is a transport one, so the peer cannot have run it.
            logger.warning(
                "mcp_tool_call_transport_error server=%s tool=%s action=rebuild",
                self.config.name, tool_name,
            )
            self._link_dead = True
            try:
                if not await self.ensure_session():
                    raise ConnectionError(self._last_error or "rebuild produced no session")
                result = await self._call_on_session(tool_name, arguments)
            except Exception as rebuild_error:
                self._record_error(rebuild_error)
                logger.warning(
                    "mcp_tool_call_rebuild_failed server=%s err=%s",
                    self.config.name, rebuild_error,
                )
                self._start_refresh_task()
                raise ConnectionError(
                    f"Server '{self.config.name}' connection lost and "
                    f"rebuild failed: {rebuild_error}"
                ) from rebuild_error
            logger.info(
                "mcp_tool_call_rebuild_ok server=%s tool=%s", self.config.name, tool_name,
            )
            # The peer is a new process/instance — its tool surface may differ.
            self._mark_tools_stale("rebuild after link loss")
            await self._notify_tools_changed()

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

    async def _call_on_session(self, tool_name: str, arguments: dict) -> Any:
        session = self._session
        if session is None:
            raise ConnectionError(
                f"Server '{self.config.name}' has no live session"
            )
        return await session.call_tool(tool_name, arguments or {})

    # ── Teardown ────────────────────────────────────────────────────────

    async def disconnect(self) -> None:
        logger.info("mcp_disconnect server=%s", self.config.name)
        # Flag any in-flight attempt to abort at its next check point, then
        # stop the retry BEFORE taking the connect lock: the task may be
        # waiting on that lock, and awaiting it while holding it would
        # deadlock (its cancellation is what releases the wait).
        self._disconnecting = True
        task = self._refresh_task
        self._refresh_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._connect_lock:
            await self._cleanup_resources()
            self._tools = []
            self._tools_fetched_at = None
            self._tools_ttl_ms = 0
            self._tools_stale = False
            self._last_error = None
            self._link_dead = False
        self._disconnecting = False
        logger.info("mcp_disconnected server=%s", self.config.name)

    async def _cleanup_resources(self) -> None:
        # Stop the listen supervisor first — its stream lives on the session
        # being torn down, and a re-listen racing the teardown would surface
        # as a spurious error from a dead transport.
        if self._watch_task is not None and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("mcp_listen_stop_failed server=%s", self.config.name, exc_info=True)
        self._watch_task = None
        if self._exit_stack is not None:
            await close_exit_stack_bounded(self._exit_stack)
            self._exit_stack = None
        self._session = None
        # stdio stderr capture: stop the drain and release the temp file (the
        # child's duplicate handle keeps writing harmlessly until it exits).
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        self._stderr_task = None
        if self._stderr_dump is not None:
            try:
                self._stderr_dump.close()
            except OSError:
                pass
            self._stderr_dump = None
            self._stderr_buffer.clear()
        self._sse_mode = False
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None


class ConnectionPool:
    """Manages a collection of MCP server connections."""

    def __init__(
        self, on_tools_changed: Callable[[str], Awaitable[None]] | None = None,
    ):
        self._connections: dict[str, MCPServerConnection] = {}
        # Fired whenever a server's tool surface may have changed — a fresh
        # list, a change event, a dead transport (see
        # MCPServerConnection._notify_tools_changed).  The wrapper wires this
        # to a tools/list_changed notification so the host re-syncs.
        self._on_tools_changed = on_tools_changed

    async def add_server(
        self, config: ServerConfig, *, connect: bool | None = None,
        read_tools: bool = True,
    ) -> MCPServerConnection:
        """Register a server; ``connect`` brings its transport up.

        ``read_tools=False`` is the BOOT shape: bring the transport up (spawn
        for stdio, session for http) and stop there — the tool list is left to
        the first reader, which is the host's reconcile, and it asks every
        server for one anyway.  Boot used to be spawn *and* list, which made
        opening a session the sum of every server's ``tools/list`` (a 1100-tool
        peer is not a fast one), for a list nobody had asked for yet.
        """
        if config.name in self._connections:
            logger.info("mcp_replace server=%s", config.name)
            await self.remove_server(config.name)
        conn = MCPServerConnection(config=config, on_tools_changed=self._on_tools_changed)
        self._connections[config.name] = conn
        # ``connect`` overrides the config default: the host's startup
        # eager-connect set registers enabled-but-skipped servers without an
        # attempt until they are enabled or used.
        should_connect = config.enabled if connect is None else connect
        if should_connect:
            if read_tools:
                # The read IS the connect: establishing the session and reading
                # the tool list are one operation, and its failure arms the retry.
                await conn.refresh_tools()
            elif not await conn.ensure_session():
                # Spawn-only: the transport is up (or its failure recorded).
                # A peer that was DOWN at boot has no tool list and, with the
                # boot read gone, nothing that would ask it again on its own —
                # so arm the same background repair a failed read arms.
                conn.arm_refresh()
        else:
            logger.info("mcp_server_not_connected name=%s enabled=%s", config.name, config.enabled)
        return conn

    async def remove_server(self, name: str) -> None:
        conn = self._connections.pop(name, None)
        if conn is None:
            return
        await conn.disconnect()

    async def disconnect_server(self, name: str) -> None:
        """Disconnect a server without removing it from the pool.

        Keeps the server config in the pool so it can be re-enabled later
        without re-adding from config.
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
        command/args or URL, enabled/disabled, and description.  It deliberately
        excludes live connection state (tool counts, errors) — that is reported
        by :meth:`list_servers` for the ``__check`` internal tool.  Secret-holding
        fields (``env``, ``headers``, ``auth``) are omitted so the listing never
        leaks tokens.

        Both families are returned, each row carrying ``rest_api`` — the caller
        that speaks for one family filters on it (``mcp_list`` /
        ``rest_api_list``).  Dropping the field is what let ``mcp_list`` report
        the REST APIs as MCP servers, with nothing in the output to tell them
        apart.
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
                "source": conn.config.source,
                "rest_api": conn.config.rest_api,
            }
            for name, conn in self._connections.items()
        ]

    def list_servers(self) -> list[dict]:
        """Live tool-list facts per server — the ``__check`` payload."""
        return [conn.snapshot() for conn in self._connections.values()]

    def list_all_tools(self, server_name: str) -> list[dict]:
        """List all tools from a specific server, regardless of active state."""
        conn = self._connections.get(server_name)
        if conn is None or not conn.has_tools():
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
