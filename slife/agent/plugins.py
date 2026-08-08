"""Plugin lifecycle management — typed container replacing getattr/setattr dynamism.

Each plugin (mcp, memdb, wechat) gets one ``PluginLifecycle`` instance that
holds its client, process, port, and optional poll-task.  This replaces the
``_{name}_client`` / ``_{name}_process`` / ``_{name}_port`` / ``_{name}_poll_task``
dynamic-attribute pattern that was scattered across AgentService.

Each ``PluginLifecycle`` can run a **watchdog** background task that monitors
the child process and auto-restarts it on unexpected exit, with exponential
backoff up to a configurable max restart count.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import TYPE_CHECKING, Callable, Awaitable

from slife.mcp.client import MCPClient

if TYPE_CHECKING:
    from slife.agent.service import AgentService

logger = logging.getLogger(__name__)

# ── Watchdog constants ────────────────────────────────────────────────────

_WATCHDOG_BACKOFF_INITIAL: float = 1.0
_WATCHDOG_BACKOFF_MAX: float = 30.0
_WATCHDOG_BACKOFF_MULTIPLIER: float = 2.0
_WATCHDOG_MAX_RESTARTS: int = 5


class PluginLifecycle:
    """Generic plugin child-process lifecycle manager.

    Each instance owns the client connection, subprocess wrapper, and port
    for one plugin backend.  Methods here correspond to the former
    ``_stop_plugin`` / ``_spawn_and_register_plugin`` patterns in
    ``AgentService``.
    """

    def __init__(self, name: str, service: AgentService) -> None:
        self.name = name
        self._service = service
        self.client: MCPClient | None = None
        self.process = None     # MCPWrapperProcess
        self.port: int = 0
        self.poll_task: asyncio.Task | None = None
        self.tunnel_task: "asyncio.Future | None" = None  # memfiles plugin: ngrok connect

        # ── Watchdog state ──────────────────────────────────────────
        self._watchdog_task: asyncio.Task | None = None
        self._stopping: bool = False
        self._restart_cb: "Callable[[], Awaitable[None]] | None" = None
        self._module: str | None = None
        self._harness_tools: set[str] | None = None
        self._max_restarts: int = _WATCHDOG_MAX_RESTARTS
        self._restart_count: int = 0

    # ── spawn ────────────────────────────────────────────────────────────

    async def spawn(
        self,
        module: str,
        harness_tools: set[str] | None = None,
    ) -> None:
        """Spawn a plugin child process, connect, and register its LLM-visible tools.

        Handles the common pattern: spawn MCPWrapperProcess → set env var →
        create client → list tools → filter harness-only tools →
        create_proxy_tools → register.

        Tools whose name starts with ``_`` are harness-only and are never
        exposed to the LLM.  The *harness_tools* parameter is **deprecated**
        — it still works for backward compatibility but the preferred
        mechanism is the ``_`` prefix naming convention.
        """
        from slife.mcp.process import MCPWrapperProcess
        from slife.mcp.tool_adapter import create_proxy_tools

        # Save params for watchdog restart
        self._module = module
        self._harness_tools = harness_tools

        logger.info("%s_spawn transport=streamable-http", self.name)
        process = MCPWrapperProcess(
            command=sys.executable,
            args=["-m", module],
        )
        await process.start()
        self.process = process
        self.port = process.port

        env_key = f"SLIFE_{self.name.upper()}_PORT"
        os.environ[env_key] = str(self.port)

        client = await process.create_client()
        self.client = client

        # Discover and register LLM-visible tools
        plugin_tools = await client.list_tools()
        logger.debug(
            "%s_tools names=%s", self.name,
            [t["name"] for t in plugin_tools],
        )

        # Harness-only tools are prefixed with _ (convention).
        # The deprecated harness_tools set is also respected.
        tagged = [
            {**t, "server": self.name}
            for t in plugin_tools
            if not t["name"].startswith("_")
            and (harness_tools is None or t["name"] not in harness_tools)
        ]

        proxy_tools = create_proxy_tools(client, tagged)
        for tool in proxy_tools:
            self._service.tool_registry.register(tool)
        logger.debug("%s_tools_registered count=%d", self.name, len(proxy_tools))

    # ── watchdog ────────────────────────────────────────────────────────

    def start_watchdog(
        self,
        restart_cb: "Callable[[], Awaitable[None]] | None" = None,
    ) -> None:
        """Start a background task that monitors the child process and
        auto-restarts it on unexpected exit.

        The watchdog waits on the subprocess, and on exit it unregisters
        the plugin's proxy tools (by ``{name}__`` prefix), then calls
        *restart_cb* (if supplied) or falls back to :meth:`spawn` with the
        saved *module* / *harness_tools*.

        Restarts use exponential backoff (1s → 2s → … → 30s) and stop
        after *max_restarts* consecutive failures.  A successful spawn
        resets the counter and backoff.

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
        """Monitor the child process; restart on unexpected exit."""
        backoff = _WATCHDOG_BACKOFF_INITIAL

        while not self._stopping:
            process = self.process
            if process is None:
                logger.debug("%s_watchdog_no_process — exiting", self.name)
                return

            subprocess = getattr(process, "_process", None)
            if subprocess is None:
                logger.debug("%s_watchdog_no_subprocess — exiting", self.name)
                return

            # Block until the child exits
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
                "%s_process_exited name=%s returncode=%s",
                self.name, returncode,
            )

            if self._restart_count >= self._max_restarts:
                logger.error(
                    "%s_watchdog_max_restarts name=%s count=%d — giving up",
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

            # ── Clean up dead tools ──────────────────────────────────
            try:
                removed = self._service.tool_registry.unregister_by_prefix(
                    f"{self.name}__",
                )
                if removed:
                    logger.info(
                        "%s_watchdog_unregistered_tools name=%s count=%d",
                        self.name, removed,
                    )
            except Exception:
                logger.debug(
                    "%s_watchdog_unregister_error", self.name, exc_info=True,
                )

            self.process = None
            self.client = None
            self.port = 0

            # ── Backoff ──────────────────────────────────────────────
            await asyncio.sleep(backoff)
            if self._stopping:
                return

            # ── Restart ──────────────────────────────────────────────
            self._restart_count += 1
            logger.info(
                "%s_watchdog_restart name=%s attempt=%d/%d",
                self.name, self._restart_count, self._max_restarts,
            )
            from slife.health import record
            record(
                "watchdog", "warning",
                key=self.name, value=f"restarting ({self._restart_count}/{self._max_restarts})",
                hint=(
                    f"{self.name} plugin exited (code {returncode}), "
                    f"restart attempt {self._restart_count}/{self._max_restarts} "
                    f"with {backoff:.1f}s backoff."
                ),
            )
            try:
                if self._restart_cb is not None:
                    await self._restart_cb()
                elif self._module is not None and self._harness_tools is not None:
                    await self.spawn(self._module, self._harness_tools)
                else:
                    logger.error(
                        "%s_watchdog_no_restart_info name=%s", self.name,
                    )
                    return
                # Success — reset counters
                backoff = _WATCHDOG_BACKOFF_INITIAL
                self._restart_count = 0
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
                logger.error(
                    "%s_watchdog_restart_failed name=%s backoff=%.1fs",
                    self.name, backoff, exc_info=True,
                )

    # ── connect via HTTP (subagent memfiles) ──────────────────────────────

    async def connect_http(self, port: int) -> None:
        """Connect to an already-running plugin via HTTP (subagent memfiles)."""
        client = MCPClient(tool_timeout=self._service.config.tool_timeout)
        await client.connect(f"http://127.0.0.1:{port}/mcp")
        self.client = client
        self.port = port

    # ── stop ─────────────────────────────────────────────────────────────

    async def stop(self, *, has_poll_task: bool = False) -> None:
        """Disconnect client and stop process.

        Args:
            has_poll_task: If True, cancel ``self.poll_task`` first.
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

        if has_poll_task and self.poll_task is not None:
            self.poll_task.cancel()
            try:
                await self.poll_task
            except asyncio.CancelledError:
                pass
            self.poll_task = None

        if self.client is not None and self.client.is_connected:
            try:
                await self.client.disconnect()
            except Exception as e:
                logger.debug("%s_disconnect_error err=%s", self.name, e)
            self.client = None

        if self.process is not None:
            try:
                await self.process.stop()
            except Exception as e:
                logger.debug("%s_process_stop_error err=%s", self.name, e)
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
        try:
            p.terminate()
        except Exception:
            pass
        try:
            p.wait(timeout=3.0)
        except Exception:
            pass
