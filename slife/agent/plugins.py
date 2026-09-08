"""Plugin lifecycle management — typed container replacing getattr/setattr dynamism.

Each plugin (mcp, memdb, wechat) gets one ``PluginLifecycle`` instance that
holds its client, process, port, and optional poll-task.  This replaces the
``_{name}_client`` / ``_{name}_process`` / ``_{name}_port`` / ``_{name}_poll_task``
dynamic-attribute pattern that was scattered across AgentService.

Each ``PluginLifecycle`` can run a **watchdog** background task that monitors
the child process and auto-restarts it on unexpected exit, with exponential
backoff up to a configurable max restart count.

Readiness (MCP plugin contract): a plugin is READY when the MCP
``initialize`` handshake completes — ``MCPClient.connect()`` runs it
(``await session.initialize()``), and a plugin server only answers
``initialize`` after its own initialization (lifespan) succeeded, so a
returned handshake means the plugin can serve.  The per-plugin serving
requirement is encoded server-side in the lifespan (e.g. memdb/memfiles
require their store); it is never probed by a harness tool.
Subordinate/external dependencies (mcp's external servers, sharefile's
tunnel, embedding backends under a store) are reported separately and never
gate readiness.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time as _time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from slife.plugins.mcp_gateway.client import MCPClient
from slife.platform import terminate_process_sync
from slife.plugins.spec import PLUGIN_SPECS, PluginSpec, spec_for

if TYPE_CHECKING:
    from slife.agent.service import AgentService
    from slife.plugins.mcp_gateway.process import MCPWrapperProcess

logger = logging.getLogger(__name__)

# ── Watchdog constants ────────────────────────────────────────────────────

_WATCHDOG_BACKOFF_INITIAL: float = 1.0
_WATCHDOG_BACKOFF_MAX: float = 30.0
_WATCHDOG_BACKOFF_MULTIPLIER: float = 2.0
_WATCHDOG_MAX_RESTARTS: int = 5

#: Hang guard for a plugin spawn — NOT a readiness mechanism.  Local plugin
#: spawns finish in seconds; this only bounds a genuinely hung child (a
#: Streamable HTTP session stuck before the lifespan finishes serving), so
#: startup convergence still fires and the user gets a failure message
#: instead of a dead TUI.  On timeout the spawn coroutine keeps running in
#: the background (matching the pre-existing memdb behaviour).
PLUGIN_SPAWN_TIMEOUT: float = 30.0

#: A restarted child is only considered *stable* once it has stayed up this
#: long.  The watchdog resets its consecutive-failure counter / backoff only
#: when the child that just exited ran for at least this window — otherwise a
#: child that comes up, passes the handshake and dies a second later would
#: reset the counter every cycle and restart forever (a ~1s boot-loop that
#: `_max_restarts` never trips).  Equal to the spawn hang-guard: a child
#: dying within the spawn window never "stabilised".
_WATCHDOG_STABLE_UPTIME: float = PLUGIN_SPAWN_TIMEOUT


def plugin_port_env(name: str) -> str:
    """Return the canonical ``SLIFE_{NAME}_PORT`` env key for a plugin.

    The key is the plugin name uppercased with dashes normalised to
    underscores (``local-embed`` → ``SLIFE_LOCAL_EMBED_PORT``), so it stays a
    valid, conventional env-var name and matches how subagents read plugin
    ports (``headless.py`` uses the underscore form for every plugin).  Every
    writer (spawn, generic spawn) and any future reader must go through this
    helper — one definition of the contract.
    """
    return f"SLIFE_{name.upper().replace('-', '_')}_PORT"


class PluginStartStatus(enum.Enum):
    """Outcome of a plugin startup attempt.

    ``SKIPPED`` is an *expected* no-op — the plugin is not configured or
    a dependency is absent (e.g. the MQTT plugin with no broker running).
    It must never be surfaced as an error; ``FAILED`` is an unexpected
    start error.
    """

    STARTED = "started"
    SKIPPED = "skipped"
    FAILED = "failed"


#: Readiness states.  ``READY_PENDING`` → ``READY_READY`` once the MCP
#: ``initialize`` handshake completes (the plugin can serve) — see the
#: module docstring.  ``SKIPPED`` plugins stay PENDING; failed startups
#: surface via ``PluginStartStatus.FAILED``.
READY_PENDING = "pending"
READY_READY = "ready"
READY_FAILED = "failed"


@dataclass
class PluginBehavior:
    """Optional per-plugin harness behavior, bound once by
    ``AgentService._resolve_plugin_behaviors`` from the spec's declared
    method-names onto the live service.

    Kept on the *service*, not in the spec, because enable gates and
    after-ready glue touch service internals (config, inbox, tool_ctx); the
    spec stays import-safe for tool_adapter / system.py / the mcp child.
    """

    enable: Callable[[], Awaitable[bool]] | None = None
    after_ready: Callable[["PluginLifecycle"], Awaitable[None]] | None = None


def client_info_extra_for(name: str) -> dict | None:
    """Initialize host extras passed to a plugin connection.

    The mechanism is uniform — every plugin connects as a Streamable MCP
    server through the same path, and host params ride in the standard
    ``initialize`` request (mcp ≥2.0 carries them in the session's
    ``capabilities.extensions`` map — identifier → settings — per the
    official SDK; the old ``clientInfo.other`` smuggling was dropped from
    the wire models).  The *content* is per-plugin and lives in this one
    mapping: today only the mcp gateway consumes host params (the active
    embedding endpoint, so its tool catalog embeds against the agent's
    endpoint), a future plugin adds its own entry.
    """
    spec = PLUGIN_SPECS.get(name)
    if spec is None or not spec.host_params:
        return None
    try:
        from slife.plugins.memdb.embedding_config import get_active_endpoint
        ep = get_active_endpoint()
        if ep.get("base_url"):
            return {"embeddings": {
                "base_url": ep["base_url"],
                "api_key": ep.get("api_key", ""),
                "model": ep.get("model", ""),
            }}
    except Exception:
        logger.debug("mcp_client_info_extra_failed", exc_info=True)
    return None


class PluginLifecycle:
    """Generic plugin child-process lifecycle manager.

    Each instance owns the client connection, subprocess wrapper, and port
    for one plugin backend.  Spawns go through ``AgentService._spawn_plugin_generic``
    (the single unified path for every plugin) — the watchdog's restart fallback
    calls the same method.
    """

    _service: AgentService

    def __init__(self, name: str, service: AgentService) -> None:
        self.name = name
        self._service = service
        self.client: MCPClient | None = None
        self.process: "MCPWrapperProcess | None" = None
        self.port: int = 0
        self.poll_task: asyncio.Task | None = None
        # Optional plugin-side background task, reseeded on watchdog restart
        # (e.g. wechat's best-effort session-restore trigger) — supervised
        # like poll_task, so a pending network call can't linger against a
        # dead or replaced client.
        self.restore_task: asyncio.Task | None = None

        # Exact tool names this plugin registered (bare names — no {name}__
        # prefix), so dead-process cleanup and stop can unregister them by
        # exact name instead of by prefix.
        self.registered_tools: set[str] = set()

        # ── Watchdog state ──────────────────────────────────────────
        self._watchdog_task: asyncio.Task | None = None
        self._stopping: bool = False
        self._restart_cb: Callable[[], Awaitable[None]] | None = None
        self._module: str | None = None
        self._max_restarts: int = _WATCHDOG_MAX_RESTARTS
        self._restart_count: int = 0

        # ── Readiness (MCP plugin contract) ─────────────────────────
        # Filled by mark_initialized() once the spawn's MCP initialize
        # handshake completed; terminal state is READY_READY (SKIPPED
        # stays PENDING; spawn failure → FAILED via PluginStartStatus).
        self.ready: bool = False
        self.ready_state: str = READY_PENDING
        self.ready_detail: str = ""

    # ── readiness (plugin contract) ─────────────────────────────────────

    def mark_initialized(self) -> None:
        """Record that the plugin's MCP ``initialize`` handshake completed.

        Readiness (MCP plugin contract): a plugin is ready exactly when its
        ``session.initialize()`` handshake succeeded — the server only
        answers it after its own initialization (lifespan) completed.  The
        per-plugin serving requirement is encoded server-side in the
        lifespan, never probed here.  Called once the client connected;
        informational in itself, made explicit so the readiness state shows
        in logs and the TUI.
        """
        self.ready = True
        self.ready_state = READY_READY
        self.ready_detail = "initialized (MCP handshake)"
        logger.info(
            "%s_ready ready=%s state=%s detail=%s",
            self.name, self.ready, self.ready_state, self.ready_detail,
        )

    # ── watchdog ────────────────────────────────────────────────────────

    def start_watchdog(
        self,
        restart_cb: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Start a background task that monitors the child process and
        auto-restarts it on unexpected exit.

        The watchdog waits on the subprocess, and on exit it unregisters
        the plugin's proxy tools (by ``{name}__`` prefix), then calls
        *restart_cb* (if supplied) or falls back to the service's generic
        spawn (``_spawn_plugin_generic``) with the saved *module*.

        Restarts use exponential backoff (1s → 2s → … → 30s) and stop
        after *max_restarts* consecutive failures.  Each restart is bounded
        by the spawn hang-guard (a child stuck before its lifespan serves is
        a failed attempt, never a block).  The consecutive-failure counter is
        reset only when a restarted child proves stable (runs ≥
        ``_WATCHDOG_STABLE_UPTIME``) before its next exit — so a ~1s
        boot-loop trips ``_max_restarts`` instead of restarting forever.

        Idempotent — if a watchdog is already running it returns
        immediately.
        """
        if self._watchdog_task is not None:
            if self._watchdog_task.done():
                # Task completed (process exited or was stopped) — clean up
                # the reference so we can start a fresh watchdog below.
                self._watchdog_task = None
            else:
                logger.debug("%s_watchdog_already_running", self.name)
                return

        if restart_cb is not None:
            self._restart_cb = restart_cb

        self._stopping = False
        self._restart_count = 0
        self._watchdog_task = asyncio.ensure_future(self._watchdog_loop())
        logger.info("%s_watchdog_started", self.name)
        from slife.health import record
        record(
            "watchdog", "ok",
            key=self.name, value="active",
            hint=f"Watchdog monitoring {self.name} plugin process.",
        )

    async def _watchdog_loop(self) -> None:
        """Monitor the child process; restart on unexpected exit.

        The loop alternates between *waiting* on a live child and
        *restarting* it.  A failed restart does **not** end the watchdog —
        it backs off (1s → 2s → … → 30s) and retries until
        ``_max_restarts`` consecutive failures, then gives up.  The
        fallback path (``spawn`` from the saved ``_module``) also works for
        plugins started without a ``restart_cb``, e.g. memdb.

        """
        backoff = _WATCHDOG_BACKOFF_INITIAL

        while not self._stopping:
            # ── Wait for a live child to exit ─────────────────────────
            # Anchor the moment a watched child started (only set when a live
            # process is actually waited on below) — the exit path uses it to
            # tell a stable run (≥ _WATCHDOG_STABLE_UPTIME) from a ~1s boot-loop
            # when deciding whether to reset the consecutive-failure counters.
            wait_started = _time.monotonic()
            process = self.process
            if process is not None:
                subprocess = getattr(process, "_process", None)
                if subprocess is None:
                    logger.debug("%s_watchdog_no_subprocess — exiting", self.name)
                    return
                try:
                    returncode = await subprocess.wait()
                except Exception:
                    logger.debug(
                        "%s_watchdog_wait_error — exiting", self.name, exc_info=True,
                    )
                    return
                if self._stopping:
                    return
                logger.warning(
                    "%s_process_exited returncode=%s",
                    self.name, returncode,
                )

                # ── Clean up dead tools ──────────────────────────────
                # Plugins register bare semantic tool names, so unregister by
                # the exact set recorded at registration (no prefix to match).
                try:
                    removed = 0
                    for tool_name in list(self.registered_tools):
                        if self._service.tool_registry.unregister(tool_name):
                            removed += 1
                    self.registered_tools.clear()
                    if removed:
                        logger.info(
                            "%s_watchdog_unregistered_tools count=%d",
                            self.name, removed,
                        )
                except Exception:
                    logger.debug(
                        "%s_watchdog_unregister_error", self.name, exc_info=True,
                    )

                # External MCP proxies registered by this lifecycle's gateway
                # glue (``{server}__{tool}`` proxies, on-demand mcp_tool_load
                # proxies) are NOT in ``registered_tools`` — that set tracks
                # only the wrapper's own bare-name tools.  Unregister every
                # registry tool still bound to this (about-to-be-disconnected)
                # client, so a dead gateway can't leave them lingering in the
                # LLM tool list bound to a dead client — permanently if the
                # watchdog exhausts restarts.  A successful restart re-adds
                # them via the normal glue.
                try:
                    removed_proxies = 0
                    for tool in list(self._service.tool_registry.list_tools()):
                        if getattr(tool, "_mcp_client", None) is self.client:
                            if self._service.tool_registry.unregister(tool.name):
                                removed_proxies += 1
                    if removed_proxies:
                        logger.info(
                            "%s_watchdog_unregistered_proxies count=%d",
                            self.name, removed_proxies,
                        )
                except Exception:
                    logger.debug(
                        "%s_watchdog_proxy_unregister_error", self.name,
                        exc_info=True,
                    )

                # ── Tear down the dead client, don't just drop it ─────
                # Leaving it referenced (or merely clearing the attribute)
                # strands its SDK post_writer / SSE-reader tasks against a
                # dead port — they hammer it with ConnectTimeouts and the
                # SDK's cancel-scope teardown bug surfaces as unretrieved
                # task exceptions.  disconnect() closes the exit stack
                # (bounded by _cleanup's 2s aclose timeout) so the watchdog
                # restart isn't delayed, and marks in-flight calls so they
                # fail fast with a clear "not connected" error.
                old_client = self.client
                self.process = None
                self.client = None
                self.port = 0
                if old_client is not None:
                    try:
                        await old_client.disconnect()
                    except Exception as e:
                        logger.debug(
                            "%s_watchdog_client_disconnect_error err=%s",
                            self.name, e, exc_info=True,
                        )

            # A fresh process is running — go back to waiting on it.
            if self.process is not None:
                continue

            # A restart only "succeeded" once the child proved stable.  If the
            # child that just exited ran for at least the stable window, this
            # crash is a fresh incident — reset the consecutive-failure
            # counters and backoff.  A child that died sooner (a boot-loop)
            # keeps its failure counted, so `_max_restarts` eventually trips
            # instead of being reset every cycle (B2).
            if (_time.monotonic() - wait_started) >= _WATCHDOG_STABLE_UPTIME:
                self._restart_count = 0
                backoff = _WATCHDOG_BACKOFF_INITIAL

            # ── Give up after max consecutive failures ───────────────
            if self._restart_count >= self._max_restarts:
                logger.error(
                    "%s_watchdog_max_restarts count=%d — giving up",
                    self.name, self._restart_count,
                )
                from slife.health import record
                record(
                    "watchdog", "error",
                    key=self.name, value="exhausted",
                    hint=(
                        f"{self.name} plugin crashed {self._restart_count} times "
                        f"(max {self._max_restarts}) — watchdog gave up. "
                        f"Restart slife to recover."
                    ),
                )
                return

            # No live process and no way to restart one — nothing to do.
            if self._restart_cb is None and self._module is None:
                logger.debug("%s_watchdog_no_restart_info — exiting", self.name)
                return

            # ── Restart (backoff applies between failed attempts) ────
            self._restart_count += 1
            logger.info(
                "%s_watchdog_restart attempt=%d/%d",
                self.name, self._restart_count, self._max_restarts,
            )
            from slife.health import record
            record(
                "watchdog", "warning",
                key=self.name, value=f"restarting ({self._restart_count}/{self._max_restarts})",
                hint=(
                    f"{self.name} plugin exited, "
                    f"restart attempt {self._restart_count}/{self._max_restarts} "
                    f"with {backoff:.1f}s backoff."
                ),
            )
            try:
                # The restart await is bounded by the same spawn hang-guard as
                # the initial spawn: a child stuck before its lifespan serves
                # must not block the watchdog forever — a timeout is a failed
                # attempt (backoff), exactly like a raised restart.
                async with asyncio.timeout(PLUGIN_SPAWN_TIMEOUT):
                    # The guard above guarantees at least one of
                    # restart_cb / _module; when restart_cb is absent, _module
                    # is set (so the fallback can restart the plugin).
                    if self._restart_cb is not None:
                        await self._restart_cb()
                    else:
                        # When restart_cb is absent, _module is guaranteed set
                        # by the guard above (local copy lets Pylance narrow).
                        module = self._module
                        if module is None:
                            logger.error("%s_watchdog_no_module", self.name)
                            return
                        # Same fully-wired spawn as `_restart_cb` paths — NOT
                        # a private half-spawn: tool timeout, list-tools retry,
                        # `SLIFE_PLUGIN_NAME`, notifications/tools/list_changed
                        # resub, readiness marking.  (The vestigial
                        # `PluginLifecycle.spawn` copy was deleted for exactly
                        # this divergence.)
                        await self._service._spawn_plugin_generic(
                            self.name, module,
                        )
                # The restart call returned — but counters/backoff are NOT
                # reset here.  A restart only "succeeded" once the child has
                # stayed up for the stable window (see the give-up section);
                # resetting right after spawn lets a ~1s boot-loop restart
                # forever (B2).
                from slife.health import record
                record(
                    "watchdog", "ok",
                    key=self.name, value="active",
                    hint=f"{self.name} plugin restarted successfully — watchdog monitoring.",
                )
            except Exception:
                backoff = min(
                    backoff * _WATCHDOG_BACKOFF_MULTIPLIER,
                    _WATCHDOG_BACKOFF_MAX,
                )
                logger.exception(
                    "%s_watchdog_restart_failed backoff=%.1fs",
                    self.name, backoff,
                )
                if not self._stopping:
                    await asyncio.sleep(backoff)

    # ── connect via HTTP (subagents share the main agent's plugins) ───────

    async def connect_http(self, port: int) -> None:
        """Connect to an already-running plugin via Streamable HTTP (subagents).

        Reconnect-safe: a prior client (e.g. one left dangling after an MCP
        wrapper restart) is disconnected first so its SDK tasks don't keep
        hammering the old, dead port.
        """
        old = self.client
        self.client = None
        if old is not None:
            try:
                await old.disconnect()
            except Exception as e:
                logger.debug(
                    "%s_http_reconnect_old_disconnect_error err=%s",
                    self.name, e, exc_info=True,
                )
        client = MCPClient(
            tool_timeout=self._service.config.tool_timeout,
            client_info_extra=client_info_extra_for(self.name),
        )
        await client.connect(f"http://127.0.0.1:{port}/mcp")
        self.client = client
        self.port = port
        # Readiness (MCP plugin contract): the connect ran the initialize
        # handshake — record the plugin as ready for subagent sharing too.
        self.mark_initialized()

    # ── stop ─────────────────────────────────────────────────────────────

    async def cancel_tasks(self) -> None:
        """Cancel and reap this lifecycle's supervised background tasks
        (``poll_task`` / ``restore_task``).

        Called before every respawn so a watchdog restart never stacks a
        second poll / drain / restore loop against a fresh client, and by
        :meth:`stop`.  Never touches the child process or client.
        """
        for attr in ("poll_task", "restore_task"):
            task = getattr(self, attr, None)
            if task is None:
                continue
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            setattr(self, attr, None)

    async def stop(self) -> None:
        """Disconnect client and stop process.

        Always cancels this lifecycle's supervised background tasks
        (``poll_task`` / ``restore_task``) first — a plugin that owns a
        poll/drain loop declares it on the lifecycle, so the caller never
        needs a flag.
        """
        # Signal watchdog to stop *before* touching the process —
        # otherwise the watchdog's ``await subprocess.wait()`` races
        # with our ``process.stop()`` and may trigger a spurious restart.
        self._stopping = True
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None

        await self.cancel_tasks()

        if self.client is not None and self.client.is_connected:
            try:
                await self.client.disconnect()
            except Exception as e:
                logger.debug("%s_disconnect_error err=%s", self.name, e, exc_info=True)
            self.client = None

        if self.process is not None:
            try:
                await self.process.stop()
            except Exception as e:
                logger.debug("%s_process_stop_error err=%s", self.name, e, exc_info=True)
            self.process = None

        logger.info("%s_shutdown", self.name)

    # ── kill (sync, no event loop) ───────────────────────────────────────

    def kill(self) -> None:
        """Synchronous best-effort child process termination.

        Called from ``finally`` blocks — no event loop required.
        """
        if self.process is None:
            return
        p = getattr(self.process, "_process", None)
        if p is None:
            return
        terminate_process_sync(p, label=self.name)


class PluginRegistry:
    """The single runtime registry of child plugins.

    Built **once** from the central contract (:data:`PLUGIN_SPECS`): every
    declared plugin gets a :class:`PluginLifecycle` eagerly, so the registry
    is complete before any start / connect and no lifecycle is ever created
    by a name-branch.  An auto-discovered package without a spec row is added
    lazily (via :meth:`ensure`) with a generic spec.

    ``AgentService._plugins`` aliases :attr:`lifecycles` — the same dict
    object, so existing readers/writers keep working — but the dict must
    never be rebound after construction (register new plugins through
    :meth:`ensure`).
    """

    def __init__(self, service: "AgentService") -> None:
        self._service = service
        self.specs: dict[str, PluginSpec] = dict(PLUGIN_SPECS)
        self.lifecycles: dict[str, PluginLifecycle] = {
            spec.name: PluginLifecycle(spec.name, service)
            for spec in PLUGIN_SPECS.values()
        }

    def spec(self, name: str, module: str | None = None) -> PluginSpec:
        """Return the spec for *name*, registering a generic default for an
        undeclared plugin the first time it is seen."""
        if name not in self.specs:
            self.specs[name] = spec_for(name, module)
        return self.specs[name]

    def ensure(self, name: str, module: str | None = None) -> PluginLifecycle:
        """Return the lifecycle for *name*, creating it (with a generic spec)
        if this is an undeclared/auto-discovered plugin."""
        self.spec(name, module)
        if name not in self.lifecycles:
            self.lifecycles[name] = PluginLifecycle(name, self._service)
        return self.lifecycles[name]

    def items(self) -> list[tuple[PluginSpec, PluginLifecycle]]:
        """Every registered plugin as ``(spec, lifecycle)`` pairs, in spec
        (deterministic start) order."""
        return [
            (spec, self.lifecycles[spec.name])
            for spec in self.specs.values()
            if spec.name in self.lifecycles
        ]
