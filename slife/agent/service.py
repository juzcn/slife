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

    def kill_child_processes(self) -> None:
        """Synchronous best-effort child process cleanup.

        Called from the finally block in main() — no event loop required.
        Directly terminates known subprocesses so they don't become
        orphans holding log file handles on Windows.
        """
        for proc_attr, label in [
            ("_mcp_process", "mcp"),
            ("_memory_process", "memory"),
        ]:
            wrapper = getattr(self, proc_attr, None)
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
                p.wait(timeout=3.0)
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
                        proc._process.wait(timeout=2.0)
                    except Exception:
                        try:
                            proc._process.kill()
                        except Exception:
                            pass

    # ── Memory lifecycle ──────────────────────────────────────────────

    @property
    def memory_enabled(self) -> bool:
        """Whether the memory service is connected."""
        return self._memory_client is not None and self._memory_client.is_connected

    async def start_memory(self) -> bool:
        """Connect to slife-memory and register tools. Returns True on success."""
        mem_cfg = self.config.memory_config
        if mem_cfg is None or not mem_cfg.enabled:
            logger.debug("memory_not_enabled")
            return False

        logger.info("memory_init start")
        try:
            await self._connect_memory()
            await self._register_memory_tools()
            logger.info("memory_init_done tools=%d", len(self.tool_registry.list_tools()))
            return True
        except Exception as e:
            logger.warning("memory_init_failed err=%s — continuing without memory", e)
            return False
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

        Harness-only tools (save_turn, get_recent_turns) are excluded —
        they are called programmatically, not by the LLM.
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
            "memory_save_turn",
            "memory_get_last_session",
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
        """Disconnect and shut down the memory service. No diary to close."""
        if self._memory_client and self._memory_client.is_connected:
            try:
                await self._memory_client.disconnect()
            except Exception as e:
                logger.debug("memory_disconnect_error err=%s", e)
            self._memory_client = None

        if self._memory_process:
            try:
                await self._memory_process.stop()
            except Exception as e:
                logger.debug("memory_process_stop_error err=%s", e)
            self._memory_process = None

        logger.info("memory_shutdown")

    async def save_to_memory(
        self, user_message: str = "", token_count: int | None = None,
    ) -> None:
        """Save the just-completed turn as a new row in memory."""
        if not self.memory_enabled:
            return

        # Extract turn messages: everything after the matching user message
        all_messages = list(self.conversation.messages)
        turn_messages: list[dict] = []
        for i in range(len(all_messages) - 1, -1, -1):
            msg = all_messages[i]
            if msg.get("role") == "user" and msg.get("content") == user_message:
                turn_messages = all_messages[i + 1:]
                break

        # Trim active context
        context_window = self.config.active_model.context_window
        self.conversation.trim_context(
            context_window=context_window,
            floor=self.config.context_floor,
            ceiling=self.config.context_ceiling,
        )

        try:
            await self._memory_client.call_tool(
                "memory_save_turn",
                {
                    "author": self.config.user,
                    "user_message": user_message,
                    "messages": turn_messages,
                    "token_count": token_count or 0,
                    "who_helped": self.config.a2a_config.agent_name or "",
                    "what_model": self.config.active_model.ref,
                },
            )
        except Exception as e:
            logger.debug("memory_save_error err=%s", e)

    async def get_recent_turns(self, limit: int = 50) -> list[dict]:
        """Load recent turns for restore. Returns [] if no turns."""
        if not self.memory_enabled:
            return []

        try:
            result = await self._memory_client.call_tool(
                "memory_get_recent_turns",
                {"author": self.config.user, "limit": limit},
            )
            data = json.loads(result)
            return data.get("turns", [])
        except Exception as e:
            logger.debug("get_recent_turns_error err=%s", e)
            return []

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
        # (Slife.tools.a2a) can discover the live client at call time.
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
        # (Slife.tools.a2a) can access the live manager at call time.
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

        # Save turn to memory — every turn is independent, no session concept
        await self.save_to_memory(
            user_message=user_input,
            token_count=self.session_usage.total_tokens,
        )

        return result
