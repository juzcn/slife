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
import sys

from slife.agent.system_prompt import build as build_system_prompt
from slife.config import Config
from slife.agent.llm_client import LLMClient, TokenUsage
from slife.agent.conversation import Conversation
from slife.agent.loop import AgentLoop, AgentEventHandler, AgentResult
from slife.tools.factory import create_tools_from_config
from slife.mcp.client import MCPClient

logger = logging.getLogger(__name__)


class AgentService:
    """Wires together LLM client, tools, conversation, and agent loop.

    Owns the agent's runtime state. The TUI delegates to this service
    rather than directly managing agent internals.

    If MCP is enabled in config, also manages the MCP wrapper connection
    and registers MCP proxy tools.

    If A2A is enabled, manages the P2P mesh: Inbox, A2AClient, and
    per-source conversations.
    """

    def __init__(self, config: Config):
        self.config = config
        self.tool_registry = create_tools_from_config(config.tools, config=config)
        self.llm_client = LLMClient(config.active_model)
        self.agent_loop = AgentLoop(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            max_iterations=config.max_iterations,
        )
        self.conversation = Conversation(
            system_prompt=build_system_prompt(
                agent_name=self.config.a2a_config.agent_name or None,
            ),
        )
        self.session_usage = TokenUsage()

        # MCP integration state
        self._mcp_client: MCPClient | None = None
        self._mcp_process = None

        # Memory integration state
        self._memory_client: MCPClient | None = None
        self._memory_process = None
        self._diary_rowid: int | None = None  # active diary rowid for updates
        self._diary_info: dict | None = None  # cached result from memory_open_diary
        self._last_diary: dict | None = None  # last completed session (for restore)
        self._trim_count: int = 0              # cumulative messages trimmed (for exact restore)

        # A2A integration state
        self._a2a_client = None
        self._a2a_broker = None
        self._subagent_manager = None
        self.inbox = None
        self._on_a2a_callbacks: list = []  # callbacks for TUI notification

    @property
    def model_display_name(self) -> str:
        """Human-readable name of the active model."""
        return self.config.active_model.display_name

    @property
    def thinking_enabled(self) -> bool:
        """Whether thinking/reasoning mode is active."""
        return self.config.active_model.thinking_enabled

    @property
    def mcp_enabled(self) -> bool:
        """Whether MCP wrapper integration is active."""
        return self._mcp_client is not None and self._mcp_client.is_connected

    @property
    def a2a_enabled(self) -> bool:
        """Whether A2A P2P mesh is active."""
        return self._a2a_client is not None and self._a2a_client.is_connected

    @property
    def subagent_manager(self):
        """The SubagentManager, if A2A is enabled and subagent support is active."""
        return self._subagent_manager

    def clear(self) -> None:
        """Reset conversation history and session usage."""
        self.conversation.clear()
        self.session_usage = TokenUsage()

    # ── MCP lifecycle ──────────────────────────────────────────────────

    async def start_mcp(self) -> None:
        """Start the MCP wrapper and register its tools.

        Called during app startup. Detects if a standalone wrapper
        is already running (via HTTP); if not, spawns one as a child
        process via stdio.
        """
        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None  # guaranteed by Config.__post_init__
        if not mcp_cfg.enabled:
            logger.debug("mcp_not_enabled")
            return

        logger.info("mcp_init start")
        await self._connect_mcp_wrapper()
        await self._register_mcp_wrapper_tools()
        await self._auto_connect_mcp_servers()
        logger.info("mcp_init_done tools=%d", len(self.tool_registry.list_tools()))

    # ── MCP private helpers ──────────────────────────────────────────

    async def _connect_mcp_wrapper(self) -> None:
        """Detect or spawn the MCP wrapper and establish a connection.

        Always probes wrapper_url first. If an HTTP wrapper is
        already running, connects via HTTP. Otherwise spawns the
        wrapper as a child process via stdio.
        """
        from slife.mcp.process import MCPWrapperProcess

        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None

        if await MCPClient.is_wrapper_running(mcp_cfg.wrapper_url):
            logger.info("mcp_wrapper_found url=%s transport=http", mcp_cfg.wrapper_url)
            self._mcp_client = MCPClient()
            await self._mcp_client.connect_http(mcp_cfg.wrapper_url)
        else:
            logger.info("mcp_wrapper_spawn transport=stdio")
            self._mcp_process = MCPWrapperProcess(
                command=mcp_cfg.wrapper_command,
                args=mcp_cfg.wrapper_args,
            )
            await self._mcp_process.start()
            self._mcp_client = await self._mcp_process.create_client()

    async def _register_mcp_wrapper_tools(self) -> None:
        """Discover and register wrapper management tools as proxy tools.

        Excludes mcp_call_tool — external MCP tools are registered
        directly with full schemas, so the LLM never needs to call
        mcp_call_tool manually. (It still exists on the wrapper for
        internal routing by MCPProxyTool.execute.)
        """
        from slife.mcp.tool_adapter import create_proxy_tools

        assert self._mcp_client is not None
        wrapper_tools = await self._mcp_client.list_tools()
        logger.debug(
            "mcp_wrapper_tools names=%s",
            [t["name"] for t in wrapper_tools],
        )

        tagged = [
            {**t, "server": "mcp"}
            for t in wrapper_tools
            if t["name"] != "mcp_call_tool"
        ]

        proxy_tools = create_proxy_tools(
            self._mcp_client, tagged,
            on_server_added=self._persist_server,
            on_server_removed=self._unpersist_server,
            on_server_disclosure_changed=self._on_server_disclosure_changed,
        )
        for tool in proxy_tools:
            self.tool_registry.register(tool)
        logger.debug("mcp_wrapper_tools_registered count=%d", len(proxy_tools))

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
        assert self._mcp_client is not None
        servers = mcp_cfg.servers
        if not servers:
            return

        logger.info("mcp_auto_connect servers=%d", len(servers))
        mcp_client = self._mcp_client  # narrow for closure

        async def _connect_one(name: str, cfg: dict) -> None:
            try:
                disclosure = cfg.get("disclosure", "eager")
                activate = disclosure != "lazy"
                result = await mcp_client.call_tool(
                    "mcp_add_server",
                    {
                        "name": name,
                        "command": cfg.get("command", ""),
                        "args": cfg.get("args", []),
                        "env": cfg.get("env"),
                        "activate": activate,
                    },
                )
                logger.debug("mcp_server_connected name=%s disclosure=%s result=%s", name, disclosure, result)
                # Eager servers: discover and register tools immediately.
                # Lazy servers: connected but tools not registered yet.
                if activate:
                    await self._discover_and_register_external_tools(server_name=name)
            except Exception as e:
                logger.error("mcp_auto_connect_failed server=%s err=%s", name, e)

        await asyncio.gather(
            *(_connect_one(name, cfg) for name, cfg in servers.items())
        )

    # ── MCP tool discovery & registration ────────────────────────────

    async def _discover_and_register_external_tools(self, server_name: str) -> None:
        """Discover tools from a specific MCP server and register as proxy tools."""
        from slife.mcp.tool_adapter import create_proxy_tools

        assert self._mcp_client is not None
        try:
            tools_json = await self._mcp_client.call_tool(
                "mcp_list_tools", {"server": server_name}
            )
            tools_data = json.loads(tools_json)
            external = tools_data.get("tools", [])

            if external:
                proxy_tools = create_proxy_tools(
                    self._mcp_client, external,
                    on_server_added=self._persist_server,
                    on_server_removed=self._unpersist_server,
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

    async def _persist_server(self, name: str, command: str, args: list[str], env: dict | None = None, description: str = "", source: dict | None = None):
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

        self.config.save_mcp_server(name, command, args, env, description, source)
        # Discover and register the new server's tools right away
        await self._discover_and_register_external_tools(server_name=name)

    async def _unpersist_server(self, name: str):
        """Callback: remove server from config and unregister its tools."""
        self.config.remove_mcp_server(name)
        removed = self.tool_registry.unregister_by_prefix(f"{name}__")
        if removed:
            logger.debug("mcp_tools_unregistered server=%s count=%d", name, removed)

    async def _on_server_disclosure_changed(self, name: str, disclosure: str):
        """Callback: persist disclosure change and update tool registration.

        eager → immediately discover and register tools.
        lazy → immediately unregister tools to save context.
        """
        logger.info("mcp_disclosure name=%s disclosure=%s", name, disclosure)
        self.config.set_server_disclosure(name, disclosure)

        if disclosure == "eager":
            await self._discover_and_register_external_tools(server_name=name)
        else:
            removed = self.tool_registry.unregister_by_prefix(f"{name}__")
            if removed:
                logger.debug("mcp_tools_unregistered server=%s count=%d", name, removed)

    async def stop_mcp(self) -> None:
        """Shut down the MCP wrapper and clean up."""
        if self._mcp_client:
            try:
                await self._mcp_client.disconnect()
            except Exception as e:
                logger.debug("mcp_disconnect_error err=%s", e)
            self._mcp_client = None

        if self._mcp_process:
            try:
                await self._mcp_process.stop()
            except Exception as e:
                logger.debug("mcp_process_stop_error err=%s", e)
            self._mcp_process = None

        logger.info("mcp_shutdown")

    # ── Memory lifecycle ──────────────────────────────────────────────

    @property
    def memory_enabled(self) -> bool:
        """Whether the memory service is connected."""
        return self._memory_client is not None and self._memory_client.is_connected

    async def start_memory(self) -> int | None:
        """Connect to the slife-memory service and open a diary.

        Returns the diary rowid if successful, None if memory is disabled
        or unavailable.

        Called during app startup. Detects if a standalone slife-memory
        is already running (via HTTP); if not, spawns one as a child
        process via stdio.
        """
        mem_cfg = self.config.memory_config
        if mem_cfg is None or not mem_cfg.enabled:
            logger.debug("memory_not_enabled")
            return None

        logger.info("memory_init start")

        try:
            await self._connect_memory()
            await self._register_memory_tools()

            # Open a diary — detects interrupted sessions automatically
            result = await self._memory_client.call_tool(
                "memory_open_diary",
                {
                    "author": self.config.user,
                    "who_helped": self.config.a2a_config.agent_name or "",
                    "what_model": self.config.active_model.ref,
                    "system_prompt": self.conversation.messages[0]["content"]
                    if self.conversation.messages
                    and self.conversation.messages[0]["role"] == "system"
                    else "",
                },
            )

            diary_info = json.loads(result)
            self._diary_rowid = diary_info.get("rowid")
            self._diary_info = diary_info  # cache for check_interrupted()
            self._last_diary = diary_info.get("last_diary")  # completed session to restore
            logger.info(
                "memory_init_done rowid=%s interrupted=%s last_diary=%s tools=%d",
                self._diary_rowid,
                diary_info.get("interrupted", False),
                self._last_diary.get("rowid") if self._last_diary else None,
                len(self.tool_registry.list_tools()),
            )
            return self._diary_rowid

        except Exception as e:
            logger.warning("memory_init_failed err=%s — continuing without memory", e)
            self._diary_rowid = None
            return None

    async def _connect_memory(self) -> None:
        """Detect or spawn the slife-memory service and establish a connection.

        Always probes memory_url first. If an HTTP service is already
        running, connects via HTTP. Otherwise spawns slife-memory as
        a child process via stdio.
        """
        from slife.mcp.process import MCPWrapperProcess

        mem_cfg = self.config.memory_config
        assert mem_cfg is not None

        if await MCPClient.is_wrapper_running(mem_cfg.url):
            logger.info("memory_found url=%s transport=http", mem_cfg.url)
            self._memory_client = MCPClient()
            await self._memory_client.connect_http(mem_cfg.url)
        else:
            logger.info("memory_spawn transport=stdio")
            self._memory_process = MCPWrapperProcess(
                command=sys.executable,
                args=["-m", "slife_memory.server"],
                server_module="slife_memory.server",
            )
            await self._memory_process.start()
            self._memory_client = await self._memory_process.create_client()

    async def _register_memory_tools(self) -> None:
        """Discover and register memory tools as proxy tools.

        Harness-only lifecycle tools (open_diary, close_diary, update_diary)
        are excluded — they are called programmatically by AgentService,
        not by the LLM.
        """
        from slife.mcp.tool_adapter import create_proxy_tools

        assert self._memory_client is not None
        memory_tools = await self._memory_client.list_tools()
        logger.debug(
            "memory_tools names=%s",
            [t["name"] for t in memory_tools],
        )

        # Harness lifecycle — never exposed to LLM
        _HARNESS_TOOLS = {
            "memory_open_diary",
            "memory_close_diary",
            "memory_update_diary",
        }

        tagged = [
            {**t, "server": "memory"}
            for t in memory_tools
            if t["name"] not in _HARNESS_TOOLS
        ]

        proxy_tools = create_proxy_tools(self._memory_client, tagged)
        for tool in proxy_tools:
            self.tool_registry.register(tool)
        logger.debug("memory_tools_registered count=%d", len(proxy_tools))

    async def stop_memory(self) -> None:
        """Close the active diary and shut down the memory service."""
        if self._memory_client and self._memory_client.is_connected:
            # Close the diary before disconnecting
            if self._diary_rowid is not None:
                try:
                    await self._memory_client.call_tool(
                        "memory_close_diary",
                        {"rowid": self._diary_rowid, "author": self.config.user},
                    )
                except Exception as e:
                    logger.debug("memory_close_error err=%s", e)

            try:
                await self._memory_client.disconnect()
            except Exception as e:
                logger.debug("memory_disconnect_error err=%s", e)
            self._memory_client = None
            self._diary_rowid = None

        if self._memory_process:
            try:
                await self._memory_process.stop()
            except Exception as e:
                logger.debug("memory_process_stop_error err=%s", e)
            self._memory_process = None

        logger.info("memory_shutdown")

    async def save_to_memory(
        self, turn_count: int | None = None, token_count: int | None = None,
    ) -> None:
        """Persist the current conversation to memory after a turn completes.

        Captures the full messages BEFORE trimming (memory is immutable),
        then trims the active context if it exceeds the configured ceiling.
        Records the cumulative *trim_count* so restart can restore the
        exact working context by skipping already-trimmed messages.
        """
        if not self.memory_enabled or self._diary_rowid is None:
            return

        # Snapshot full conversation before trimming (immutable record)
        full_messages = list(self.conversation.messages)

        # Trim the active context
        context_window = self.config.active_model.context_window
        trimmed = self.conversation.trim_context(
            context_window=context_window,
            floor=self.config.context_floor,
            ceiling=self.config.context_ceiling,
        )
        self._trim_count += trimmed

        # Save full messages + cumulative trim position
        try:
            await self._memory_client.call_tool(
                "memory_update_diary",
                {
                    "rowid": self._diary_rowid,
                    "author": self.config.user,
                    "messages": full_messages,
                    "turn_count": turn_count or 0,
                    "token_count": token_count or 0,
                    "trim_count": self._trim_count,
                },
            )
        except Exception as e:
            logger.debug("memory_save_error err=%s", e)

    async def check_interrupted(self) -> dict | None:
        """Check if there's a diary to restore.

        Returns the interrupted diary, or the last completed diary
        (slife is a permanent-memory agent — every restart should
        offer to continue the previous conversation).

        Returns None only when there is no prior session at all.
        Must be called after start_memory().

        Uses the result cached from start_memory()'s memory_open_diary
        call — does NOT call memory_open_diary again, because a second
        call would find the diary just opened by start_memory() and
        incorrectly report it as interrupted.
        """
        if not self.memory_enabled or self._diary_info is None:
            return None

        # Interrupted session (crash) — always restore
        if self._diary_info.get("interrupted"):
            return self._diary_info

        # Last completed session — offer to continue
        if self._last_diary:
            return {
                "rowid": self._last_diary["rowid"],
                "interrupted": False,
                "restore": True,
                "title": self._last_diary.get("title", ""),
                "created_at": self._last_diary.get("created_at", ""),
                "updated_at": self._last_diary.get("updated_at", ""),
                "status": self._last_diary.get("status", ""),
                "how_many_turns": self._last_diary.get("how_many_turns", 0),
                "how_many_tokens": self._last_diary.get("how_many_tokens", 0),
                "who_helped": self._last_diary.get("who_helped", ""),
                "what_model": self._last_diary.get("what_model", ""),
                "trim_count": self._last_diary.get("trim_count", 0),
            }

        return None

    # ── A2A lifecycle ──────────────────────────────────────────────────

    async def start_a2a(
        self, handler_factory: "Callable[[], Any] | None" = None,
    ) -> None:
        """Connect to MQTT broker for remote agent P2P mesh.

        Called during app startup after MCP initialization.
        Probes for an existing broker, spawns one if configured.
        Registers unified A2A tools covering both MQTT and local transports.

        Args:
            handler_factory: Optional callable that creates a TUI handler
                for each incoming A2A task.  When provided, remote tasks
                stream to the chat view just like human-typed messages.
        """
        a2a_cfg = self.config.a2a_config
        if a2a_cfg is None or not a2a_cfg.enabled:
            logger.debug("a2a_not_enabled")
            return

        logger.info("a2a_init start")

        # Ensure broker is available (probe → optional spawn)
        if a2a_cfg.broker_command:
            from slife.a2a.broker import BrokerManager
            self._a2a_broker = BrokerManager(
                command=a2a_cfg.broker_command,
                host=a2a_cfg.broker_host,
                port=a2a_cfg.broker_port,
            )
            try:
                await self._a2a_broker.ensure()
            except Exception as e:
                logger.warning("a2a_broker_ensure_failed err=%s", e)

        # Create and connect the A2A client
        from slife.a2a.client import A2AClient
        self._a2a_client = A2AClient(a2a_cfg)
        await self._a2a_client.connect()

        # Set up the unified Inbox
        from slife.agent.inbox import Inbox, ConversationStore
        from slife.a2a.identity import HUMAN

        conversations = ConversationStore(
            system_prompt=build_system_prompt(
                agent_name=a2a_cfg.agent_name or None,
            ),
        )
        conversations._convs[HUMAN] = self.conversation

        self.inbox = Inbox(
            agent_loop=self.agent_loop,
            conversations=conversations,
            a2a_client=self._a2a_client,
            on_activity=self._notify_a2a_activity,
        )

        # Register handler factory so remote tasks always have a TUI
        # handler — streams to chat like human-typed messages.
        if handler_factory is not None:
            conversations.set_default_handler_factory(handler_factory)

        # Wire A2A incoming tasks → Inbox
        self._a2a_client.on_incoming_task(self.inbox.post)

        # Start the inbox background processor
        self._inbox_task = asyncio.create_task(self.inbox.run())

        # Start agent-change notifier (log + TUI notifications)
        self._a2a_client.on_agent_change(self._on_agent_change)

        # Set module-level transport reference so native A2A tools
        # (slife.tools.a2a) can discover the live client at call time.
        from slife.a2a.client import set_client
        set_client(self._a2a_client)

    def set_inbox_handler_factory(self, factory) -> None:
        """Register a factory that creates TUI handlers for inbox messages.

        Called by the TUI layer so remote A2A tasks always have a handler
        available, even before the first human message is typed.
        """
        if self.inbox is not None:
            self.inbox._conversations.set_default_handler_factory(factory)

        logger.info("a2a_init_done tools=%d", len(self.tool_registry.list_tools()))

    async def stop_a2a(self) -> None:
        """Leave the P2P mesh and clean up."""
        # Cancel inbox processing
        if hasattr(self, "_inbox_task") and self._inbox_task:
            self._inbox_task.cancel()
            try:
                await self._inbox_task
            except asyncio.CancelledError:
                pass

        # Clear module-level transport reference
        from slife.a2a.client import clear_client
        clear_client()

        self._a2a_client = None
        self.inbox = None

        # Stop broker if we spawned it
        if self._a2a_broker:
            try:
                await self._a2a_broker.stop()
            except Exception as e:
                logger.debug("a2a_broker_stop_error err=%s", e)
            self._a2a_broker = None

        logger.info("a2a_shutdown")

    # ── Subagent lifecycle ─────────────────────────────────────────────

    async def start_subagent(self) -> None:
        """Set up local subagent spawning (stdin/stdout pipes).

        Skipped when running as a subagent ourselves (SLIFE_SUBAGENT_NAME
        is set) — prevents recursive nested spawning.

        Independent of A2A over MQTT — both transports coexist.
        """
        import os as _os
        if _os.environ.get("SLIFE_SUBAGENT_NAME"):
            logger.debug("subagent_skipped — running as subagent")
            return

        sub_cfg = self.config.subagent_config
        if sub_cfg is None:
            logger.debug("subagent_no_config")
            return

        logger.info("subagent_init start")

        from slife.subagent.process import SubagentManager, set_manager
        self._subagent_manager = SubagentManager(self.config)

        # Set module-level transport reference so native subagent tools
        # (slife.tools.a2a) can access the live manager at call time.
        set_manager(self._subagent_manager)

        logger.info("subagent_init_done tools=%d", len(self.tool_registry.list_tools()))

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

    async def _on_agent_change(self, card, event: str) -> None:
        """Log agent presence changes and notify TUI callbacks."""
        logger.info(
            "a2a_peer_%s id=%s name=%s", event, card.agent_id, card.display_name,
        )
        await self._notify_a2a_activity(
            "agent_change", card=card, event=event,
        )

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

        When the Inbox is active (A2A enabled), messages are routed
        through the inbox for serialisation.  Otherwise the legacy
        direct-call path is used.
        """
        if self.inbox is not None:
            # Route through the unified inbox
            from slife.a2a.identity import HUMAN, AgentMessage
            from slife.agent.inbox import ConversationStore

            # Register the TUI handler for human messages
            conversations = self.inbox._conversations
            conversations.register_handler(HUMAN, handler)

            msg = AgentMessage(
                source=HUMAN,
                content=user_input,
                images=images if images else [],
            )
            await self.inbox.post(msg)

            # Return a placeholder — TUIHandler will update the UI
            # as streaming events arrive.  The actual result is not
            # available synchronously with the inbox model.
            return AgentResult(text="", usage=TokenUsage())

        # Legacy direct path (A2A disabled)
        result = await self.agent_loop.run(
            user_input=user_input,
            conversation=self.conversation,
            images=images,
            handler=handler,
        )

        # Save to memory after each completed turn
        turn_count = sum(
            1 for m in self.conversation.messages if m.get("role") == "user"
        )
        await self.save_to_memory(
            turn_count=turn_count,
            token_count=self.session_usage.total_tokens,
        )

        return result
