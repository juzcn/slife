"""Plugin lifecycle management — typed container replacing getattr/setattr dynamism.

Each plugin (mcp, memory, wechat) gets one ``PluginLifecycle`` instance that
holds its client, process, port, and optional poll-task.  This replaces the
``_{name}_client`` / ``_{name}_process`` / ``_{name}_port`` / ``_{name}_poll_task``
dynamic-attribute pattern that was scattered across AgentService.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import TYPE_CHECKING

from slife.mcp.client import MCPClient

if TYPE_CHECKING:
    from slife.agent.service import AgentService

logger = logging.getLogger(__name__)


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

    # ── spawn ────────────────────────────────────────────────────────────

    async def spawn(
        self,
        module: str,
        harness_tools: set[str],
    ) -> None:
        """Spawn a plugin child process, connect, and register its LLM-visible tools.

        Handles the common pattern: spawn MCPWrapperProcess → set env var →
        create client → list tools → filter harness-only tools →
        create_proxy_tools → register.
        """
        from slife.mcp.process import MCPWrapperProcess
        from slife.mcp.tool_adapter import create_proxy_tools

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

        tagged = [
            {**t, "server": self.name}
            for t in plugin_tools
            if t["name"] not in harness_tools
        ]

        proxy_tools = create_proxy_tools(client, tagged)
        for tool in proxy_tools:
            self._service.tool_registry.register(tool)
        logger.debug("%s_tools_registered count=%d", self.name, len(proxy_tools))

    # ── connect via HTTP (subagent sharing) ──────────────────────────────

    async def connect_http(self, port: int) -> None:
        """Connect to an already-running plugin via HTTP (subagent sharing)."""
        client = MCPClient(tool_timeout=self._service.config.tool_timeout)
        await client.connect(f"http://127.0.0.1:{port}")
        self.client = client
        self.port = port

    # ── stop ─────────────────────────────────────────────────────────────

    async def stop(self, *, has_poll_task: bool = False) -> None:
        """Disconnect client and stop process.

        Args:
            has_poll_task: If True, cancel ``self.poll_task`` first.
        """
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
