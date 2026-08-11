"""Agent service layer — wires together LLM, tools, conversation, and loop.

Owns the agent's runtime state. The TUI delegates to this service
rather than directly managing agent internals.

If MCP is enabled in config, also manages the MCP wrapper connection
and registers MCP proxy tools.

If A2A is enabled in config, manages the P2P agent mesh: connects to
the MQTT broker, publishes presence, discovers peers, and routes tasks
through a unified Inbox.
"""

import asyncio
import json
import logging
import os
import sys
import time as _time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from slife.agent.system_prompt import build as build_system_prompt
from slife.config import Config
from slife.agent.llm_client import LLMClient, TokenUsage
from slife.agent.conversation import Conversation
from slife.agent.loop import AgentLoop, AgentEventHandler, AgentResult
from slife.agent.inbox import Inbox, ConversationStore
from slife.agent.plugins import PluginLifecycle, PluginStartStatus
from slife.a2a.identity import HUMAN
from slife.tools.factory import create_tools_from_config
from slife.mcp.tool_adapter import create_proxy_tools

logger = logging.getLogger(__name__)

# Module-level callbacks invoked when the active model is switched at
# runtime (e.g. by the model_switch tool).  Each callback receives the
# new model ref string (e.g. "deepseek/deepseek-v4-flash").
_on_model_switched: list[Callable[[str], None]] = []


class AgentService:
    """Wires together LLM client, tools, conversation, and agent loop.

    Owns the agent's runtime state. The TUI delegates to this service
    rather than directly managing agent internals.

    If MCP is enabled in config, also manages the MCP wrapper connection
    and registers MCP proxy tools.

    If A2A is enabled, manages the P2P mesh: Inbox, A2AClient, and
    per-source conversations.
    """

    def __init__(self, config: Config, is_subagent: bool = False):
        self.config = config
        # __post_init__ guarantees these are never None at runtime
        assert self.config.a2a_config is not None
        assert self.config.subagent_config is not None
        self.is_subagent = is_subagent

        # Build the shared ToolContext — replaces scattered module-level
        # singletons (get_registry, get_config, _rest_api_mcp_client, etc.)
        from slife.tools.context import ToolContext
        self._tool_ctx = ToolContext(config=config)
        self.tool_registry = create_tools_from_config(
            config.tools, config=config, is_subagent=is_subagent,
            ctx=self._tool_ctx,
        )
        # Backfill the registry reference (created by the factory)
        self._tool_ctx.registry = self.tool_registry
        # Also set the module-level singleton so check_mcp()
        # and other legacy callers of get_registry() can find it.
        # Subagents get their own filtered registry — don't overwrite
        # the main agent's reference with a subagent's.
        if not is_subagent:
            from slife.tools.registry import set_registry
            set_registry(self.tool_registry)
        self.llm_client = LLMClient(config.active_model)
        # Max tool result = tool_result_ceiling × context_window × 3 chars/token
        max_tool_result_chars = int(
            config.tool_result_ceiling
            * config.active_model.context_window
            * 3
        )
        # Pending A2A peer presence events (epoch, TUI line), drained into
        # the context footer on the next turn.  Unbounded by design — events
        # are consumed on every turn, so the steady-state size is "events
        # since last turn"; the guard below only protects against a
        # pathological long-idle + heavy-flapping session.
        self._presence_events: deque[tuple[float, str]] = deque()
        self.agent_loop = AgentLoop(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            max_iterations=config.max_iterations,
            max_tool_result_chars=max_tool_result_chars,
            tool_timeout=config.tool_timeout,
            context_window=config.active_model.context_window,
            context_floor=config.context_floor,
            context_ceiling=config.context_ceiling,
            memdb_enabled=not is_subagent,
            supports_vision=config.active_model.supports_vision,
            model_name=config.active_model.display_name,
            input_modalities=", ".join(config.active_model.input_modalities),
            presence_provider=self._drain_presence_events,
        )
        self.conversation = Conversation(
            system_prompt=build_system_prompt(self.config),
        )
        self._tool_ctx.conversation = self.conversation
        self.session_usage = TokenUsage()

        # ── Unified message queue (always active) ──────────────────
        # Every input — human keyboard, A2A MQTT, WeChat — flows
        # through the same inbox queue.  Processed serially.
        conversations = ConversationStore(
            system_prompt=build_system_prompt(self.config),
        )
        conversations._convs[HUMAN] = self.conversation

        self.inbox = Inbox(
            agent_loop=self.agent_loop,
            conversations=conversations,
            a2a_client=None,  # (legacy, unused)
            on_activity=self._notify_a2a_activity,  # always active for WeChat etc.
            on_turn_complete=self.save_to_memory,
        )
        self._inbox_task: asyncio.Task | None = None

        # ── Plugin lifecycle containers (replace dynamic setattr/getattr) ─
        self._plugins: dict[str, PluginLifecycle] = {
            "mcp": PluginLifecycle("mcp", self),
            "memdb": PluginLifecycle("memdb", self),
            "wechat": PluginLifecycle("wechat", self),
            "memfiles": PluginLifecycle("memfiles", self),
            "mqtt": PluginLifecycle("mqtt", self),
        }

        # A2A integration state
        self._subagent_manager = None
        self._on_a2a_callbacks: list = []  # callbacks for TUI notification

        # Register for runtime model-switch notifications so the
        # LLM client and agent loop stay in sync with the active model.
        _on_model_switched.append(self.reload_active_model)

    @property
    def model_display_name(self) -> str:
        """Human-readable name of the active model."""
        return self.config.active_model.display_name

    @property
    def thinking_enabled(self) -> bool:
        """Whether thinking/reasoning mode is active."""
        return self.config.active_model.thinking_enabled

    def reload_active_model(self, new_ref: str) -> None:
        """Reload runtime state after the active model is switched.

        Rebuilds the LLM client, updates the agent loop's model-specific
        settings, and refreshes the conversation system prompt so the
        next turn uses the new model.
        """
        old_ref = self.config.active_model_ref
        self.config.active_model_ref = new_ref
        model = self.config.active_model

        logger.info(
            "model_reloaded from=%s to=%s display=%s",
            old_ref, new_ref, model.display_name,
        )

        # Rebuild LLM client with the new model config
        self.llm_client = LLMClient(model)

        # Update agent loop with new model's capabilities
        self.agent_loop.llm_client = self.llm_client
        self.agent_loop.supports_vision = model.supports_vision
        self.agent_loop.model_name = model.display_name
        self.agent_loop.input_modalities = ", ".join(model.input_modalities)
        self.agent_loop.context_window = model.context_window

        # Rebuild system prompt with updated model info
        self.conversation.update_context_footer("")
        new_system = build_system_prompt(self.config)
        if self.conversation.messages and self.conversation.messages[0]["role"] == "system":
            self.conversation.messages[0]["content"] = new_system
            self.conversation._base_system_prompt = new_system

    @property
    def mcp_enabled(self) -> bool:
        """Whether MCP wrapper integration is active."""
        c = self._plugins["mcp"].client
        return c is not None and c.is_connected

    @property
    def a2a_enabled(self) -> bool:
        """Whether the A2A P2P mesh is active (mqtt plugin connected)."""
        client = self._plugins["mqtt"].client
        return client is not None and client.is_connected

    @property
    def subagent_manager(self):
        """The SubagentManager, if A2A is enabled and subagent support is active."""
        return self._subagent_manager

    def clear(self) -> None:
        """Reset conversation history and session usage."""
        self.conversation.clear()
        self.session_usage = TokenUsage()

    # ── Plugin auto-discovery & lifecycle ───────────────────────────────

    async def start_plugin_server(
        self, name: str, module: str,
    ) -> PluginStartStatus:
        """Spawn a plugin child process, connect, and register its tools.

        Single entry point for ALL plugins — built-in (mcp, memdb,
        wechat) and third-party.  The plugin's ``server.py`` must expose
        a ``main()`` that calls ``run_plugin_server(mcp)``.

        Dispatches internally for MCP (configurable wrapper command) and
        WeChat (poll loop); everything else uses the generic spawn path.
        Returns ``PluginStartStatus.STARTED`` on success, ``SKIPPED`` for
        expected no-ops (not configured / dependency absent — e.g. mqtt
        without a running broker), ``FAILED`` on controlled failure, and
        raises on unexpected errors.
        """
        # ── MCP wrapper: custom command, auto-connects external servers ──
        if name == "mcp":
            if not self.config.mcp_config:
                logger.debug("mcp_skipped reason=no_config")
                return PluginStartStatus.SKIPPED
            await self.start_mcp()
            return (
                PluginStartStatus.STARTED
                if self.mcp_enabled else PluginStartStatus.FAILED
            )

        # ── WeChat: needs poll loop after registration ──────────────────
        if name == "wechat":
            return await self.start_wechat()

        # ── Memfiles: fully generic plugin — spawn, connect, register.
        # The plugin owns the ngrok tunnel and file serving; the harness
        # only exposes the plugin's MCP client for health checks.
        if name == "memfiles":
            started = await self._spawn_plugin_generic(name, module)
            if started:
                self._tool_ctx.memfiles_client = self._plugins["memfiles"].client
                # Publish the port so subagents can inherit it and reuse
                # the main agent's memfiles plugin (no second ngrok tunnel).
                os.environ["SLIFE_MEMFILES_PORT"] = str(self._plugins["memfiles"].port)
                self._start_generic_watchdog(name, module)
            return (
                PluginStartStatus.STARTED if started else PluginStartStatus.FAILED
            )

        # ── A2A: mesh channel plugin — probe + config env + poll loop ──
        if name == "mqtt":
            return await self.start_mqtt()

        # ── Generic: spawn python -m <module>, connect, register tools ──
        started = await self._spawn_plugin_generic(name, module)
        if started:
            self._start_generic_watchdog(name, module)
        return (
            PluginStartStatus.STARTED if started else PluginStartStatus.FAILED
        )

    def _start_generic_watchdog(self, name: str, module: str) -> None:
        """Attach a crash watchdog to a generically-spawned plugin.

        Only for plugins managed via ``self._plugins`` (built-ins like
        ``a2a``).  The restart callback re-invokes the generic spawn —
        ``_spawn_plugin_generic`` itself never starts a watchdog, so a
        restart never stacks a second monitor.
        """
        if name not in self._plugins:
            return

        async def _restart() -> None:
            await self._spawn_plugin_generic(name, module)
            if name == "memfiles":
                # _spawn_plugin_generic replaced self._plugins["memfiles"].client,
                # but the harness's ToolContext still points at the dead client —
                # re-point it or check_memfiles reports the restarted plugin offline.
                self._tool_ctx.memfiles_client = self._plugins[name].client

        self._plugins[name].start_watchdog(restart_cb=_restart)

    async def _spawn_plugin_generic(self, name: str, module: str) -> bool:
        """Spawn a plugin child, connect, and register its ``<name>__*`` tools."""
        from slife.mcp.process import MCPWrapperProcess

        logger.info("plugin_spawn name=%s module=%s", name, module)

        process = MCPWrapperProcess(
            command=sys.executable,
            args=["-m", module],
        )
        await process.start()
        client = await process.create_client(tool_timeout=self.config.tool_timeout)

        # Discover tools
        plugin_tools = await client.list_tools()
        logger.debug("plugin_tools name=%s count=%d names=%s",
                     name, len(plugin_tools),
                     [t["name"] for t in plugin_tools])

        # Register as proxy tools — filter out harness-only tools.
        # Canonical marker: a plugin tool named ``__*`` (double underscore) is
        # harness-internal — called programmatically via call_tool(), never
        # exposed to the LLM.  (Single ``_`` = harness but LLM-visible, e.g.
        # the native `_sys_note`/`_sys_trim`.)  The "harness-only" description
        # is kept as a secondary safety check.
        tagged = [
            {**t, "server": name}
            for t in plugin_tools
            if not t.get("name", "").startswith("__")
            and "harness-only" not in t.get("description", "").lower()
        ]
        if len(tagged) < len(plugin_tools):
            logger.debug(
                "plugin_tools_filtered name=%s kept=%d dropped=%d",
                name, len(tagged), len(plugin_tools) - len(tagged),
            )
        proxy_tools = create_proxy_tools(client, tagged)
        for tool in proxy_tools:
            self.tool_registry.register(tool)

        logger.info("plugin_ready name=%s tools=%d",
                     name, len(self.tool_registry.list_tools()))

        # Store for cleanup — use PluginLifecycle for known plugins, setattr for auto-discovered
        if name in self._plugins:
            self._plugins[name].client = client
            self._plugins[name].process = process
            self._plugins[name].port = process.port
        else:
            setattr(self, f"_{name}_client", client)
            setattr(self, f"_{name}_process", process)
            setattr(self, f"_{name}_port", process.port)
        os.environ[f"SLIFE_{name.upper()}_PORT"] = str(process.port)

        return True

    # ── MCP lifecycle ──────────────────────────────────────────────────

    async def start_mcp(self) -> None:
        """Start the MCP wrapper as a child process and register its tools."""
        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None  # guaranteed by Config.__post_init__

        logger.info("mcp_init_start")
        try:
            await self._connect_mcp_wrapper()
            await self._register_plugin_tools(
                "mcp",
                on_server_added=self._persist_server,
                on_server_removed=self._unpersist_server,
                on_server_updated=self._on_server_updated,
            )
            from slife.health import record
            record(
                "mcp_wrapper", "ok",
                key="status", value="connected",
                hint="MCP wrapper started and management tools registered.",
            )
        except Exception as e:
            logger.error("mcp_wrapper_init_failed err=%s", e)
            from slife.health import record
            record(
                "mcp_wrapper", "error",
                key="status", value="failed",
                hint=f"MCP wrapper failed to start: {e}. "
                     "MCP tools (filesystem, search, etc.) are unavailable.",
            )
            raise
        # Fire-and-forget: external MCP servers connect in background
        # so the UI appears immediately. Tools register as each server
        # connects — no need to block startup on slow/hung servers.
        asyncio.create_task(self._auto_connect_mcp_servers())
        asyncio.create_task(self._auto_connect_rest_apis())

        # Watchdog: on MCP wrapper crash, respawn + reconnect external servers
        async def _restart_mcp():
            await self._connect_mcp_wrapper()
            await self._register_plugin_tools(
                "mcp",
                on_server_added=self._persist_server,
                on_server_removed=self._unpersist_server,
                on_server_updated=self._on_server_updated,
            )
            asyncio.create_task(self._auto_connect_mcp_servers())
            asyncio.create_task(self._auto_connect_rest_apis())

        self._plugins["mcp"].start_watchdog(restart_cb=_restart_mcp)

        logger.info("mcp_init_done tools=%d", len(self.tool_registry.list_tools()))

    # ── HTTP-connect helpers (subagents share the main agent's plugins) ──

    async def _connect_plugin_http(self, name: str, port: int) -> None:
        """Connect to an already-running plugin via Streamable HTTP.

        Shared by :meth:`connect_mcp_http`, :meth:`connect_memdb_http`,
        and :meth:`connect_wechat_http`.
        """
        logger.info("%s_http_connect port=%s", name, port)
        await self._plugins[name].connect_http(port)

    async def connect_mcp_http(self, port: int) -> None:
        """Connect to an already-running MCP wrapper via Streamable HTTP.

        Used by subagents to share the main agent's plugin servers
        instead of spawning their own.

        Both main agents and subagents eagerly discover external MCP
        tools and register them as direct tools (e.g.
        ``tavily-mcp__tavily_search``).  Subagents use
        :meth:`_discover_existing_mcp_tools` which only lists tools
        from already-connected servers — it never spawns new processes.
        """
        await self._connect_plugin_http("mcp", port)
        await self._register_plugin_tools(
            "mcp",
            on_server_added=self._persist_server,
            on_server_removed=self._unpersist_server,
            on_server_updated=self._on_server_updated,
        )
        if not self.is_subagent:
            await self._auto_connect_mcp_servers()
            await self._auto_connect_rest_apis()
        else:
            await self._discover_existing_mcp_tools()
        logger.info("mcp_http_connect_done tools=%d", len(self.tool_registry.list_tools()))

    async def connect_memdb_http(self, port: int) -> None:
        """Connect to an already-running memdb server via Streamable HTTP."""
        await self._connect_plugin_http("memdb", port)
        await self._register_plugin_tools("memdb")
        logger.info("memdb_http_connect_done tools=%d", len(self.tool_registry.list_tools()))

    async def connect_wechat_http(self, port: int) -> None:
        """Connect to an already-running wechat server via Streamable HTTP."""
        await self._connect_plugin_http("wechat", port)
        await self._register_plugin_tools("wechat")
        logger.info("wechat_http_connect_done tools=%d", len(self.tool_registry.list_tools()))

    async def connect_memfiles_http(self, port: int) -> None:
        """Connect to the main agent's memfiles plugin via Streamable HTTP.

        Used by subagents to reuse the main agent's file-cabinet plugin
        instead of spawning their own (which would also fight over the
        single free-tier ngrok tunnel).  Registers the ``memfiles__*``
        tools and exposes the client for health checks.
        """
        await self._connect_plugin_http("memfiles", port)
        await self._register_plugin_tools("memfiles")
        self._tool_ctx.memfiles_client = self._plugins["memfiles"].client
        logger.info("memfiles_http_connect_done tools=%d", len(self.tool_registry.list_tools()))

    async def connect_mqtt_http(self, port: int) -> None:
        """Connect to the main agent's mqtt plugin via Streamable HTTP.

        Used by subagents to reuse the main agent's mesh channel.  The plugin
        exposes only ``_``-prefixed harness tools (the A2A surface is native
        ``a2a_*`` tools that forward here); subagents can send but never drain
        the inbound queue (that stays with the main agent).
        """
        await self._connect_plugin_http("mqtt", port)
        await self._register_plugin_tools("mqtt")
        # Expose the mesh client so the native a2a_* tools can reach remote
        # mesh peers through it.
        self._tool_ctx.a2a_mcp_client = self._plugins["mqtt"].client
        logger.info("mqtt_http_connect_done tools=%d", len(self.tool_registry.list_tools()))

    # ── MCP private helpers ──────────────────────────────────────────

    async def _connect_mcp_wrapper(self) -> None:
        """Spawn the MCP wrapper as a child process via Streamable HTTP."""
        from slife.mcp.process import MCPWrapperProcess

        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None

        logger.info("mcp_wrapper_spawn transport=streamable-http")
        self._mcp_process = MCPWrapperProcess(
            command=mcp_cfg.wrapper_command,
            args=mcp_cfg.wrapper_args,
        )
        await self._mcp_process.start()
        self._plugins["mcp"].process = self._mcp_process
        self._plugins["mcp"].port = self._mcp_process.port
        os.environ["SLIFE_MCP_PORT"] = str(self._plugins["mcp"].port)
        self._plugins["mcp"].client = await self._mcp_process.create_client()

    async def _register_plugin_tools(self, name: str, **kwargs) -> None:
        """Discover and register a connected plugin's tools as proxy tools.

        Tags each tool with the plugin's ``server`` name (the ``server__tool``
        prefix), filters out harness-only tools (names starting with ``__``),
        creates proxy tools, and registers them.

        Args:
            name: Plugin short name (``"mcp"``, ``"memdb"``, ``"wechat"``,
                ``"mqtt"`` — its tools are all ``_``-prefixed, so none are
                registered here; the A2A surface is the native ``a2a_*`` tools).
            **kwargs: Forwarded to :func:`create_proxy_tools` (e.g.
                ``on_server_added``, ``on_server_removed``).
        """
        client = self._plugins[name].client
        assert client is not None
        tools = await client.list_tools()
        logger.debug(
            "%s_tools names=%s", name,
            [t["name"] for t in tools],
        )

        # Harness-only: ``__`` (double-underscore) plugin tools are internal
        # and filtered out for all agents.
        tagged = [
            {**t, "server": name}
            for t in tools
            if not t["name"].startswith("__")
        ]

        proxy_tools = create_proxy_tools(self._plugins[name].client, tagged, **kwargs)
        for tool in proxy_tools:
            self.tool_registry.register(tool)
        logger.debug("%s_tools_registered count=%d", name, len(proxy_tools))

        # MCP-specific: let REST API tools call mcp_set / mcp_remove
        if name == "mcp":
            self._tool_ctx.mcp_client = self._plugins["mcp"].client

    async def _auto_connect_mcp_servers(self) -> None:
        """Auto-connect to pre-configured MCP servers and discover
        their tools.

        Servers are connected in parallel — each spawns its own
        subprocess independently, so total time is max(single-server)
        rather than sum(all-servers).  For 5 servers this cuts
        startup from ~18 s to ~9 s.
        """
        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None
        assert self._plugins["mcp"].client is not None
        servers = mcp_cfg.servers
        if not servers:
            return

        logger.info("mcp_auto_connect servers=%d", len(servers))
        mcp_client = self._plugins["mcp"].client  # narrow for closure

        async def _connect_one(name: str, cfg: dict) -> None:
            try:
                if cfg.get("enabled") is False:
                    # Load into pool but don't connect or register tools.
                    # Server stays in pool (disabled) — no connection made.
                    # Use mcp_set_enabled(name=..., enabled=true) to enable later.
                    logger.debug("mcp_server_loading_disabled name=%s", name)
                    result = await mcp_client.call_tool(
                        "mcp_set",
                        {
                            "name": name,
                            "command": cfg.get("command", ""),
                            "args": cfg.get("args", []),
                            "env": cfg.get("env"),
                            "url": cfg.get("url", ""),
                            "headers": cfg.get("headers"),
                            "auth": cfg.get("auth"),
                            "enabled": False,
                        },
                    )
                    logger.debug(
                        "mcp_server_loaded_disabled name=%s result=%.200s",
                        name, result,
                    )
                    return
                # ── os_paths: auto-detect OS-accessible paths ──────────
                # When a file MCP has ``os_paths: true``, inject every
                # OS-accessible path as a ``--allow-path`` arg so the LLM
                # can reach anything the OS user can.  Permissions are
                # enforced by the OS kernel — not by the MCP config.
                args = list(cfg.get("args", []))
                if cfg.get("os_paths"):
                    from slife.os_detect import get_os_accessible_paths
                    os_paths = get_os_accessible_paths()
                    for p in os_paths:
                        args.extend(["--allow-path", p])
                    logger.debug(
                        "mcp_os_paths server=%s paths=%s", name, os_paths,
                    )

                result = await mcp_client.call_tool(
                    "mcp_set",
                    {
                        "name": name,
                        "command": cfg.get("command", ""),
                        "args": args,
                        "env": cfg.get("env"),
                        "url": cfg.get("url", ""),
                        "headers": cfg.get("headers"),
                        "auth": cfg.get("auth"),
                    },
                )
                logger.debug(
                    "mcp_server_connected name=%s result=%.200s",
                    name, result,
                )
                from slife.health import record
                record(
                    "mcp_server", "ok",
                    key=name, value="connected",
                    hint=f"MCP server '{name}' connected.",
                )
                # Register tools immediately — enabled implies eager.
                await self._discover_and_register_external_tools(server_name=name)
            except Exception as e:
                logger.error("mcp_auto_connect_failed server=%s err=%s", name, e)
                from slife.health import record
                record(
                    "mcp_server", "error",
                    key=name, value="connect_failed",
                    hint=f"MCP server '{name}' failed to connect: {e}",
                )

        await asyncio.gather(
            *(_connect_one(name, cfg) for name, cfg in servers.items())
        )

    async def _auto_connect_rest_apis(self) -> None:
        """Auto-connect REST APIs from the ``rest_apis`` config section.

        Each entry is connected via ``mcp_set`` with
        anyapi-mcp-server as the backend.  The LLM never sees npx or
        anyapi-mcp-server — it only sees the generated tools prefixed
        with the API name.
        """
        if not self._plugins["mcp"].client:
            return

        rest_apis = self.config.rest_apis
        if not rest_apis:
            return

        mcp_client = self._plugins["mcp"].client
        logger.info("rest_api_auto_connect count=%d", len(rest_apis))

        async def _connect_one(name: str, cfg: dict) -> None:
            try:
                if cfg.get("enabled") is False:
                    logger.debug("rest_api_skipped name=%s reason=disabled", name)
                    return
                spec_url = cfg.get("spec_url", "")
                base_url = cfg.get("base_url", "")
                api_key = cfg.get("api_key", "")
                description = cfg.get("description", "")

                if not spec_url or not base_url:
                    logger.warning(
                        "rest_api_skip name=%s reason=missing_spec_or_base", name,
                    )
                    return

                mcp_args = [
                    "-y", "anyapi-mcp-server",
                    "--name", name,
                    "--spec", spec_url,
                    "--base-url", base_url,
                ]
                if api_key:
                    mcp_args.extend([
                        "--header", f"Authorization: Bearer ${{{api_key}}}",
                    ])

                result = await mcp_client.call_tool(
                    "mcp_set",
                    {
                        "name": name,
                        "command": "npx",
                        "args": mcp_args,
                        "description": description,
                    },
                )
                logger.debug(
                    "rest_api_connected name=%s result=%.200s", name, result,
                )
            except Exception as e:
                logger.warning("rest_api_connect_failed name=%s err=%s", name, e)

        await asyncio.gather(
            *(_connect_one(name, cfg) for name, cfg in rest_apis.items())
        )

    async def _discover_existing_mcp_tools(self) -> None:
        """Discover tools from already-connected external MCP servers.

        Used by subagents — they share the main agent's MCP gateway, so
        external servers are already spawned.  Unlike
        :meth:`_auto_connect_mcp_servers`, this never calls
        ``mcp_set`` — it only lists tools from connected servers
        and registers them as proxy tools.

        Each ``mcp_list_tools`` call uses its own POST SSE stream,
        so concurrent requests are safe with the standard MCP library
        transport.
        """
        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None
        assert self._plugins["mcp"].client is not None
        servers = mcp_cfg.servers
        if not servers:
            return

        logger.info("mcp_discover_existing servers=%d", len(servers))

        async def _discover_one(name: str, cfg: dict) -> None:
            try:
                if cfg.get("enabled") is False:
                    logger.debug("mcp_server_skipped name=%s reason=disabled", name)
                    return
                await self._discover_and_register_external_tools(server_name=name)
            except Exception as e:
                logger.debug("mcp_discover_existing_failed server=%s err=%s", name, e)

        await asyncio.gather(
            *(_discover_one(name, cfg) for name, cfg in servers.items())
        )

        logger.info(
            "mcp_discover_existing_done tools=%d",
            len(self.tool_registry.list_tools()),
        )

    # ── MCP tool discovery & registration ────────────────────────────

    async def _discover_and_register_external_tools(self, server_name: str) -> None:
        """Discover tools from a specific MCP server and register as proxy tools."""
        assert self._plugins["mcp"].client is not None
        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None

        try:
            tools_json = await self._plugins["mcp"].client.call_tool(
                "mcp_list_tools", {"server": server_name}
            )
            tools_data = json.loads(tools_json)
            external = tools_data.get("tools", [])

            if external:
                proxy_tools = create_proxy_tools(
                    self._plugins["mcp"].client, external,
                    on_server_added=self._persist_server,
                    on_server_removed=self._unpersist_server,
                    on_server_updated=self._on_server_updated,
                )
                for tool in proxy_tools:
                    self.tool_registry.register(tool)
                logger.debug(
                    "mcp_tools_registered server=%s count=%d",
                    server_name, len(proxy_tools),
                )
            else:
                logger.debug("mcp_no_tools server=%s", server_name)
        except Exception as e:
            logger.error("mcp_discover_failed server=%s err=%s", server_name, e)
            from slife.health import record
            record(
                "mcp_server", "warning",
                key=server_name, value="discovery_failed",
                hint=f"MCP server '{server_name}' connected but tool discovery failed: {e}",
            )

    async def _persist_server(self, name: str, command: str, args: list[str], env: dict | None = None, description: str = "", source: dict | None = None, url: str = "", headers: dict[str, str] | None = None):
        """Callback: persist a newly-added (or updated) MCP server to config
        file and immediately discover and register its tools.

        If a server with the same name already exists, its old proxy tools
        are unregistered first — this handles reconfiguration of servers
        like anyapi-mcp-server when the user provides new args.
        """
        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None
        existing = mcp_cfg.servers.get(name)
        if existing:
            logger.debug(
                "mcp_server_update name=%s", name
            )
            self.tool_registry.unregister_by_prefix(f"{name}__")

        self.config.save_mcp_server(name, command, args, env, description, source, url=url, headers=headers)
        # Discover and register the new server's tools right away
        await self._discover_and_register_external_tools(server_name=name)

    async def _unpersist_server(self, name: str):
        """Callback: remove server from config and unregister its tools."""
        self.config.remove_mcp_server(name)
        removed = self.tool_registry.unregister_by_prefix(f"{name}__")
        if removed:
            logger.debug("mcp_tools_unregistered server=%s count=%d", name, removed)

    async def _on_server_updated(self, name: str, enabled: bool):
        """Callback: persist the enabled flag and update tool registration.

        - enabled=False → unregister tools, persist enabled=False.
        - enabled=True  → persist enabled=True, re-discover + register tools.
        """
        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None

        if not enabled:
            # Unregister tools from agent loop, persist disabled
            removed = self.tool_registry.unregister_by_prefix(f"{name}__")
            if removed:
                logger.debug("mcp_tools_unregistered server=%s count=%d", name, removed)
            self.config.set_server_enabled(name, False)
            return

        # enabled=True — persist, re-discover and register tools
        self.tool_registry.unregister_by_prefix(f"{name}__")
        await self._discover_and_register_external_tools(server_name=name)
        self.config.set_server_enabled(name, True)

    # ── Stop helpers ────────────────────────────────────────────────────

    async def _stop_plugin(self, name: str, *, has_poll_task: bool = False) -> None:
        """Disconnect client and stop process for plugin *name*.

        Args:
            name: Plugin short name (``"mcp"``, ``"memdb"``, ``"wechat"``).
            has_poll_task: If True, cancel the plugin's poll task first.
        """
        await self._plugins[name].stop(has_poll_task=has_poll_task)

    async def stop_mcp(self) -> None:
        """Shut down the MCP wrapper and clean up."""
        await self._stop_plugin("mcp")

    async def stop_memdb(self) -> None:
        """Disconnect and shut down the memdb service."""
        await self._stop_plugin("memdb")

    async def stop_wechat(self) -> None:
        """Shut down the WeChat plugin and clean up."""
        await self._stop_plugin("wechat", has_poll_task=True)

    async def stop_memfiles(self) -> None:
        """Stop the memfiles plugin.

        The plugin's own lifespan disconnects the ngrok tunnel on shutdown,
        so the harness only stops the child process.
        """
        await self._stop_plugin("memfiles")

    def kill_child_processes(self) -> None:
        """Synchronous best-effort child process cleanup.

        Called from the finally block in main() — no event loop required.
        Directly terminates known subprocesses so they don't become
        orphans holding log file handles on Windows.

        Scans for all ``_<name>_process`` attributes — works with
        auto-discovered plugins, not just the three built-ins.
        """
        # Kill known plugin processes first
        for plugin in self._plugins.values():
            plugin.kill()

        # Scan for auto-discovered plugins
        for attr_name in dir(self):
            if not attr_name.endswith("_process") or not attr_name.startswith("_"):
                continue
            wrapper = getattr(self, attr_name, None)
            if wrapper is None:
                continue
            p = getattr(wrapper, "_process", None)
            if p is None:
                continue
            try:
                p.terminate()
            except Exception:
                pass
            try:
                p.wait(timeout=3.0)  # type: ignore[call-arg]
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

        # Subagent manager cleanup
        mgr = self._subagent_manager
        if mgr is not None:
            for name in list(mgr._subagents.keys()):
                proc = mgr._subagents.get(name)
                if proc is not None and proc._process is not None:
                    try:
                        proc._process.terminate()
                    except Exception:
                        pass
                    try:
                        proc._process.wait(timeout=2.0)  # type: ignore[call-arg]
                    except Exception:
                        try:
                            proc._process.kill()
                        except Exception:
                            pass

    # ── Memory lifecycle ──────────────────────────────────────────────

    @property
    def memdb_enabled(self) -> bool:
        """Whether the memdb service is connected."""
        return self._plugins["memdb"].client is not None and self._plugins["memdb"].client.is_connected

    @property
    def wechat_enabled(self) -> bool:
        """Whether the WeChat plugin is connected."""
        return self._plugins["wechat"].client is not None and self._plugins["wechat"].client.is_connected

    # ── Plugin spawn + register helper ────────────────────────────────

    async def _spawn_and_register_plugin(
        self,
        name: str,
        module: str,
        harness_tools: set[str] | None = None,
    ) -> None:
        """Spawn a plugin child process, connect, and register its LLM-visible tools.

        Delegates to ``PluginLifecycle.spawn()`` for the actual work.

        *harness_tools* is **deprecated** — harness-only tools are now
        identified by the ``_`` prefix naming convention.
        """
        await self._plugins[name].spawn(module, harness_tools)


    async def start_memdb(self) -> bool:
        """Connect to slife-memdb and register tools. Returns True on success."""
        mem_cfg = self.config.memdb_config
        assert mem_cfg is not None  # guaranteed by Config.__post_init__

        logger.info("memdb_init_start")
        try:
            await self._spawn_and_register_plugin(
                "memdb",
                "slife.plugins.memdb.server",
            )
            self._plugins["memdb"].start_watchdog()
            logger.info("memdb_init_done tools=%d", len(self.tool_registry.list_tools()))
            from slife.health import record
            record(
                "memdb_service", "ok",
                key="status", value="connected",
                hint="memdb service started and tools registered.",
            )
            return True
        except Exception as e:
            logger.warning("memdb_init_failed err=%s fallback=continue_without_memdb", e)
            from slife.health import record
            record(
                "memdb_service", "error",
                key="status", value="failed",
                hint=f"memdb service failed to start: {e}. "
                     "Turn storage and search are unavailable.",
            )
            return False

    # ── WeChat lifecycle ───────────────────────────────────────────────

    async def start_wechat(self) -> PluginStartStatus:
        """Start the WeChat plugin if enabled in config.

        Returns ``STARTED`` on success, ``SKIPPED`` when WeChat is not
        enabled (expected), ``FAILED`` on start error.
        """
        wechat_cfg = self.config.wechat_config
        if wechat_cfg is None or not wechat_cfg.enabled:
            logger.debug("wechat_not_enabled")
            return PluginStartStatus.SKIPPED

        logger.info("wechat_init_start")
        try:
            await self._spawn_and_register_plugin(
                "wechat",
                "slife.plugins.wechat.server",
            )

            # Auto-restore session at startup (triggers server-side poll loop)
            wechat_client = self._plugins["wechat"].client
            assert wechat_client is not None
            try:
                await wechat_client.call_tool("check_status", {})
                logger.debug("wechat_auto_restore_triggered")
            except Exception:
                pass

            # Start background poll loop — injects WeChat messages into the inbox
            self._plugins["wechat"].poll_task = asyncio.create_task(self._wechat_poll_loop())

            # Watchdog: on crash, respawn + restore poll loop
            async def _restart_wechat():
                await self._spawn_and_register_plugin(
                    "wechat",
                    "slife.plugins.wechat.server",
                    )
                wc = self._plugins["wechat"].client
                if wc is not None:
                    try:
                        await wc.call_tool("check_status", {})
                    except Exception:
                        pass
                self._plugins["wechat"].poll_task = asyncio.create_task(
                    self._wechat_poll_loop(),
                )

            self._plugins["wechat"].start_watchdog(restart_cb=_restart_wechat)

            logger.info("wechat_init_done tools=%d", len(self.tool_registry.list_tools()))
            from slife.health import record
            record(
                "wechat_service", "ok",
                key="status", value="connected",
                hint="WeChat plugin started and tools registered.",
            )
            return PluginStartStatus.STARTED
        except Exception as e:
            logger.warning("wechat_init_failed err=%s fallback=continue_without_wechat", e)
            from slife.health import record
            record(
                "wechat_service", "error",
                key="status", value="failed",
                hint=f"WeChat plugin failed to start: {e}. "
                     "WeChat messaging is unavailable.",
            )
            return PluginStartStatus.FAILED

    async def _wechat_poll_loop(self, interval: float = 5.0) -> None:
        """Poll the wechat plugin for new messages and inject them into the inbox.

        Uses harness-only tools (wechat_drain_incoming, wechat_dispatch_reply)
        so all wechat-specific logic — typing indicators, message format —
        stays inside the plugin process.  The harness only sees generic
        messages with an on_reply callback.
        """
        import json as _json
        from slife.a2a.identity import AgentMessage, WECHAT

        logger.info("wechat_poll_loop_start interval=%.1fs", interval)

        while self.wechat_enabled:
            try:
                assert self._plugins["wechat"].client is not None

                result = await self._plugins["wechat"].client.call_tool(
                    "__wechat_drain_incoming", {},
                )
                data = _json.loads(result)
                msgs = data.get("messages", [])

                for m in msgs:
                    from_id = m.get("to_user_id", "")
                    ctx_token = m.get("context_token", "")
                    text = m.get("text", "")

                    if not text.strip():
                        continue

                    wc = self._plugins["wechat"].client  # local ref for closure

                    async def _reply(reply_text: str,
                                     uid=from_id, tok=ctx_token) -> None:
                        try:
                            await wc.call_tool("__wechat_dispatch_reply", {
                                "to_user_id": uid,
                                "context_token": tok,
                                "text": reply_text,
                            })
                            logger.debug("wechat_out to=%s len=%d", uid, len(reply_text))
                        except Exception as e:
                            logger.debug("wechat_reply_error err=%s", e)

                    msg = AgentMessage(
                        source=WECHAT,
                        content=text,
                        metadata={"channel": "wechat"},
                        on_reply=_reply,
                    )
                    await self.inbox.post(msg)
                    logger.debug("wechat_in from=%s text=%.100s", from_id, text)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("wechat_poll_error err=%s", e)

            await asyncio.sleep(interval)

        logger.info("wechat_poll_loop_stop")

    async def save_to_memory(
        self,
        user_message: str = "",
        token_count: int | None = None,
        conversation: "Conversation | None" = None,
        channel: str = "",
    ) -> None:
        """Save the just-completed turn as a new row in memory.

        Args:
            user_message: The user's input text.
            token_count: Cumulative token usage for the turn.
            conversation: The conversation to extract messages from.
                Defaults to self.conversation (the TUI conversation).
            channel: Source channel — 'human', 'wechat', or remote agent id.
        """
        # Accumulate turn's billed tokens into the session total.
        if token_count:
            self.session_usage.total_tokens += token_count

        if not self.memdb_enabled:
            return

        conv = conversation if conversation is not None else self.conversation

        # Invariant: never persist an inconsistent turn.  Repair orphaned
        # tool_calls and close the turn if needed BEFORE extracting — the
        # same ensure used on load and before each user message.
        conv._ensure_turn_consistent()

        # Extract turn messages: everything after the matching user message.
        # Must handle both plain text (content is a str) and multimodal
        # messages (content is a list of {type, text/image_url} parts).
        all_messages = list(conv.messages)
        turn_messages: list[dict] = []
        for i in range(len(all_messages) - 1, -1, -1):
            msg = all_messages[i]
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str) and content == user_message:
                turn_messages = all_messages[i + 1:]
                break
            if isinstance(content, list):
                text = "".join(
                    p.get("text", "") for p in content if p.get("type") == "text"
                )
                if text == user_message:
                    turn_messages = all_messages[i + 1:]
                    break

        # Context trimming now happens in AgentLoop._maybe_trim_context()
        # before each LLM call, with a visible _trim_context notification
        # inserted into the conversation.  Each turn is still saved here
        # via memory_save_turn, so trimmed turns remain searchable.

        assert self._plugins["memdb"].client is not None  # guarded by memdb_enabled
        try:
            await asyncio.wait_for(
                self._plugins["memdb"].client.call_tool(
                    "__memory_save_turn",
                    {
                        "user_message": user_message,
                        "messages": turn_messages,
                        "token_count": token_count or 0,
                        "who_helped": (self.config.a2a_config and self.config.a2a_config.agent_name) or "",
                        "what_model": self.config.active_model.ref,
                        "channel": channel,
                    },
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.warning("memdb_save_timeout reason=first_save_loads_embedding_model")
        except Exception as e:
            logger.warning("memdb_save_error err=%s", e)

    async def get_recent_turns(self, limit: int = 50) -> list[dict]:
        """Load recent turns for restore. Returns [] if no turns.

        Reads directly from the SQLite database — does NOT go through
        the MCP tool call / Streamable HTTP transport.  Independent of
        whether the memory plugin has been started.
        """
        try:
            from slife.plugins.memdb.store import SessionStore
            db_path = self._get_memory_db_path()
            if db_path and db_path.is_file():
                store = SessionStore(db_path)
                await store.setup(embedding_dim=0)
                turns = await store.get_recent_turns(limit=limit)
                if turns:
                    return turns
        except Exception as e:
            logger.debug("get_recent_turns_direct_db_error err=%s", e)

        return []

    def _get_memory_db_path(self) -> Path | None:
        """Return the memory database path."""
        import os
        from slife.paths import get_data_dir

        env_path = os.environ.get("SLIFE_MEMDB_DB")
        if env_path:
            return Path(env_path)
        agent_id = os.environ.get("SLIFE_AGENT_ID", "slife")
        return get_data_dir() / f"{agent_id}.db"

    # ── Inbox lifecycle (always active) ────────────────────────────────

    async def start_inbox(self) -> None:
        """Start the inbox background processor.

        Called during app startup before A2A/WeChat so the queue is
        ready to accept messages from any input channel.
        """
        if self._inbox_task is not None:
            return
        self._inbox_task = asyncio.create_task(self.inbox.run())
        logger.info("inbox_started")

    async def stop_inbox(self) -> None:
        """Stop the inbox background processor."""
        if self._inbox_task is None:
            return
        self._inbox_task.cancel()
        try:
            await self._inbox_task
        except asyncio.CancelledError:
            pass
        self._inbox_task = None
        logger.info("inbox_stopped")

    # ── A2A lifecycle ──────────────────────────────────────────────────

    async def start_mqtt(self) -> PluginStartStatus:
        """Start the A2A mesh as a plugin (thin client: connect + drain).

        All A2A/MQTT logic lives in the mqtt plugin process — the harness
        only spawns it, registers the ``a2a__*`` tools, and polls
        ``__a2a_drain_incoming`` for inbound tasks/presence.  Probes
        Mosquitto first; returns ``SKIPPED`` when the broker is
        unreachable or A2A is not configured (expected, not an error).
        Idempotent.
        """
        if self._plugins["mqtt"].process is not None:
            return PluginStartStatus.STARTED  # already started

        a2a_cfg = self.config.a2a_config
        if a2a_cfg is None or not a2a_cfg.enabled:
            logger.debug("a2a_disabled")
            return PluginStartStatus.SKIPPED

        from slife.a2a.broker import probe_broker
        if not await probe_broker(a2a_cfg.broker_host, a2a_cfg.broker_port):
            logger.info(
                "a2a_broker_not_found host=%s port=%d action=a2a_disabled",
                a2a_cfg.broker_host, a2a_cfg.broker_port,
            )
            a2a_cfg.enabled = False
            return PluginStartStatus.SKIPPED
        a2a_cfg.enabled = True

        # Pass the a2a config to the plugin process via env.
        from dataclasses import asdict
        os.environ["SLIFE_MQTT_CONFIG"] = json.dumps(
            asdict(a2a_cfg), ensure_ascii=False,
        )

        started = await self._spawn_plugin_generic(
            "mqtt", "slife.plugins.mqtt.server",
        )
        if not started:
            return PluginStartStatus.FAILED

        # Expose the mesh client so the native a2a_* tools can reach remote
        # peers (when the plugin is down this stays None → mesh tools report
        # "not connected", subagent/stdin paths still work).
        self._tool_ctx.a2a_mcp_client = self._plugins["mqtt"].client
        self._plugins["mqtt"].poll_task = asyncio.create_task(self._mqtt_poll_loop())

        # Crash watchdog — respawn the plugin and restart the drain loop.
        async def _restart_mqtt() -> None:
            await self._spawn_plugin_generic("mqtt", "slife.plugins.mqtt.server")
            self._tool_ctx.a2a_mcp_client = self._plugins["mqtt"].client
            self._plugins["mqtt"].poll_task = asyncio.create_task(self._mqtt_poll_loop())

        self._plugins["mqtt"].start_watchdog(restart_cb=_restart_mqtt)

        from slife.health import record
        record(
            "a2a", "ok",
            key="status", value="connected",
            hint="A2A P2P mesh connected (plugin).",
        )
        logger.info("mqtt_plugin_started")
        return PluginStartStatus.STARTED

    async def _mqtt_poll_loop(self, interval: float = 1.0) -> None:
        """Drain inbound a2a tasks/presence from the plugin into the inbox.

        The harness stays a thin client: it only drains the plugin's
        ``__a2a_drain_incoming`` and feeds the unified inbox.  Replies are
        routed back through the plugin via ``__a2a_dispatch_result``.
        """
        import json as _json
        from slife.a2a.identity import AgentId, AgentMessage
        from slife.a2a.card import AgentCard, format_presence_line

        logger.info("a2a_poll_loop_start interval=%.1fs", interval)

        while True:
            try:
                client = self._plugins["mqtt"].client
                if client is None:
                    break
                result = await client.call_tool("__a2a_drain_incoming", {})
                data = _json.loads(result)

                a2a_client = client  # narrowed MCPClient for the reply closure
                for ev in data.get("tasks", []):
                    async def _reply(
                        reply_text: str,
                        rt=ev.get("reply_to", ""),
                        cid=ev.get("correlation_id", ""),
                    ) -> None:
                        try:
                            assert a2a_client is not None
                            await a2a_client.call_tool("__a2a_dispatch_result", {
                                "reply_to": rt, "corr_id": cid, "text": reply_text,
                            })
                        except Exception:
                            pass

                    msg = AgentMessage(
                        source=AgentId(ev.get("source", "unknown")),
                        content=ev.get("content", ""),
                        reply_to=ev.get("reply_to", ""),
                        correlation_id=ev.get("correlation_id", ""),
                        on_reply=_reply,
                    )
                    await self.inbox.post(msg)
                    logger.debug(
                        "a2a_in source=%s task=%.80s",
                        msg.source, ev.get("content", ""),
                    )

                for pev in data.get("presence", []):
                    card = AgentCard(
                        agent_id=AgentId(pev.get("card", {}).get("agent_id", "?")),
                        display_name=pev.get("card", {}).get("display_name", ""),
                        status=pev.get("card", {}).get("status", "idle"),
                    )
                    text = format_presence_line(card, pev.get("event", ""))
                    if text is not None:
                        self._presence_events.append((_time.time(), text))
                    await self._notify_a2a_activity(
                        "agent_change", event=pev.get("event", ""), card=card,
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("a2a_poll_error err=%s", e)

            await asyncio.sleep(interval)

        logger.info("a2a_poll_loop_stop")


    def set_inbox_handler_factory(self, factory) -> None:
        """Register a factory that creates TUI handlers for inbox messages.

        Called by the TUI layer so remote A2A tasks always have a handler
        available, even before the first human message is typed.
        """
        if self.inbox is not None:
            self.inbox._conversations.set_default_handler_factory(factory)

        logger.info("a2a_init_done tools=%d", len(self.tool_registry.list_tools()))

    async def stop_mqtt(self) -> None:
        """Leave the P2P mesh — stop the drain loop and the a2a plugin.

        Does NOT stop the inbox — the queue is independent of A2A
        and may still be used by human input / WeChat.
        """
        # PluginLifecycle.stop() sets _stopping first so the watchdog does
        # not spuriously restart the plugin on a graceful shutdown.
        await self._stop_plugin("mqtt", has_poll_task=True)

        # Clear inbox a2a references
        if self.inbox is not None:
            self.inbox._a2a_client = None
            self.inbox._on_activity = None

        # Clear module-level transport reference
        from slife.a2a.client import clear_client
        clear_client()

        logger.info("a2a_shutdown")

    # ── Subagent lifecycle ─────────────────────────────────────────────

    async def start_subagent(self) -> None:
        """Set up local subagent spawning (stdin/stdout pipes).

        Recursion is allowed — a subagent can spawn its own descendants.

        Independent of A2A over MQTT — both transports coexist.
        """
        logger.info("subagent_init_start")

        from slife.subagent.process import SubagentManager, set_manager
        self._subagent_manager = SubagentManager(self.config)

        # Set module-level transport reference so native subagent tools
        # (Slife.tools.a2a) can access the live manager at call time.
        set_manager(self._subagent_manager)

        # When a subagent completes an async task, push the result into
        # the inbox so the user sees it without having to poll.
        async def _on_subagent_done(agent_id: str, task_id: str, result: str) -> None:
            from slife.a2a.identity import AgentMessage, SUBAGENT
            msg = AgentMessage(
                source=SUBAGENT,
                content=(
                    f"Subagent **{agent_id}** completed async task "
                    f"(ID: `{task_id}`):\n\n"
                    f"{result}"
                ),
            )
            await self.inbox.post(msg)

        self._subagent_manager.on_task_complete = _on_subagent_done

        logger.info("subagent_init_done tools=%d", len(self.tool_registry.list_tools()))
        from slife.health import record
        record(
            "subagent", "ok",
            key="status", value="ready",
            hint=f"Subagent manager ready (max_subagents={(self.config.subagent_config or {}).get('max_subagents', '?')}).",
        )

    async def stop_subagent(self) -> None:
        """Stop all local subagents and clean up."""
        if self._subagent_manager:
            try:
                await self._subagent_manager.stop_all()
            except Exception as e:
                logger.debug("subagent_stop_all_error err=%s", e)
            self._subagent_manager = None

        # Clear module-level transport reference
        from slife.subagent.process import clear_manager
        clear_manager()

        logger.info("subagent_shutdown")

    def _drain_presence_events(self) -> list[tuple[float, str]]:
        """Return pending presence events and clear the buffer.

        Called by ``AgentLoop`` at the start of each turn (read-once):
        events that happened since the last turn are injected into the
        context footer exactly once.  If the buffer ever grows
        pathologically large it is trimmed here, not silently at
        render time — the oldest entries are dropped with a warning.
        """
        if not self._presence_events:
            return []
        if len(self._presence_events) > 1000:
            logger.warning(
                "presence_events_overflow dropped=%d",
                len(self._presence_events) - 1000,
            )
        events = list(self._presence_events)[-1000:]
        self._presence_events.clear()
        return events

    async def _notify_a2a_activity(self, kind: str, **kwargs) -> None:
        """Fire all registered A2A activity callbacks."""
        for cb in self._on_a2a_callbacks:
            try:
                await cb(kind, **kwargs)
            except Exception:
                pass

    def on_a2a_activity(self, callback) -> None:
        """Register a callback for A2A events (TUI notification).

        Callback signature: ``async def cb(kind: str, **kwargs)``
        where *kind* is ``"agent_change"``, ``"task_received"``, or
        ``"task_completed"``.
        """
        self._on_a2a_callbacks.append(callback)

    # ── Message processing ────────────────────────────────────────────

    async def process_message(
        self,
        user_input: str,
        images: list[str] | None,
        handler: AgentEventHandler,
    ) -> AgentResult:
        """Run the agent loop for a user message via streaming.

        All messages (human keyboard, A2A, WeChat) go through the
        unified inbox queue — processed serially, never cancelled.
        """
        from slife.a2a.identity import AgentMessage

        msg = AgentMessage(
            source=HUMAN,
            content=user_input,
            images=images if images else [],
            handler=handler,
        )
        await self.inbox.post(msg)

        # Return a placeholder — TUIHandler will update the UI
        # as streaming events arrive.  The actual result is not
        # available synchronously with the inbox model.
        return AgentResult(text="", usage=TokenUsage())
