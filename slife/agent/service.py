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
from slife.agent.inbox import Inbox, ConversationStore
from slife.a2a.identity import HUMAN
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
        # Max tool result = tool_result_ceiling × context_window × 3 chars/token
        max_tool_result_chars = int(
            config.tool_result_ceiling
            * config.active_model.context_window
            * 3
        )
        self.agent_loop = AgentLoop(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            max_iterations=config.max_iterations,
            max_tool_result_chars=max_tool_result_chars,
        )
        self.conversation = Conversation(
            system_prompt=build_system_prompt(
                agent_id=self.config.agent_id,
                agent_name=self.config.a2a_config.agent_name,
            ),
        )
        self.session_usage = TokenUsage()

        # ── Unified message queue (always active) ──────────────────
        # Every input — human keyboard, A2A MQTT, WeChat — flows
        # through the same inbox queue.  Processed serially.
        conversations = ConversationStore(
            system_prompt=build_system_prompt(
                agent_id=self.config.agent_id,
                agent_name=self.config.a2a_config.agent_name,
            ),
        )
        conversations._convs[HUMAN] = self.conversation

        self.inbox = Inbox(
            agent_loop=self.agent_loop,
            conversations=conversations,
            a2a_client=None,  # injected by start_a2a when enabled
            on_activity=self._notify_a2a_activity,  # always active for WeChat etc.
            on_turn_complete=self.save_to_memory,
        )
        self._inbox_task: asyncio.Task | None = None

        # MCP integration state
        self._mcp_client: MCPClient | None = None
        self._mcp_process = None

        # Memory integration state
        self._memory_client: MCPClient | None = None
        self._memory_process = None

        # WeChat integration state
        self._wechat_client: MCPClient | None = None
        self._wechat_process = None

        # A2A integration state
        self._a2a_client = None
        self._subagent_manager = None
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
        """Start the MCP wrapper as a child process and register its tools."""
        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None  # guaranteed by Config.__post_init__

        logger.info("mcp_init start")
        try:
            await self._connect_mcp_wrapper()
            await self._register_mcp_wrapper_tools()
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
            return
        await self._auto_connect_mcp_servers()
        logger.info("mcp_init_done tools=%d", len(self.tool_registry.list_tools()))

    # ── MCP private helpers ──────────────────────────────────────────

    async def _connect_mcp_wrapper(self) -> None:
        """Spawn the MCP wrapper as a child process via stdio."""
        from slife.mcp.process import MCPWrapperProcess

        mcp_cfg = self.config.mcp_config
        assert mcp_cfg is not None

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
                if cfg.get("enabled") is False:
                    logger.debug("mcp_server_skipped name=%s reason=disabled", name)
                    return
                disclosure = cfg.get("disclosure", "eager")
                activate = disclosure != "lazy"
                result = await mcp_client.call_tool(
                    "mcp_add_server",
                    {
                        "name": name,
                        "command": cfg.get("command", ""),
                        "args": cfg.get("args", []),
                        "env": cfg.get("env"),
                        "url": cfg.get("url", ""),
                        "headers": cfg.get("headers"),
                        "activate": activate,
                    },
                )
                logger.debug("mcp_server_connected name=%s disclosure=%s result=%s", name, disclosure, result)
                from slife.health import record
                record(
                    "mcp_server", "ok",
                    key=name, value="connected",
                    hint=f"MCP server '{name}' connected (disclosure={disclosure}).",
                )
                # Eager servers: discover and register tools immediately.
                # Lazy servers: connected but tools not registered yet.
                if activate:
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
            ("_wechat_process", "wechat"),
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

    @property
    def wechat_enabled(self) -> bool:
        """Whether the WeChat plugin is connected."""
        return self._wechat_client is not None and self._wechat_client.is_connected

    async def start_memory(self) -> bool:
        """Connect to slife-memory and register tools. Returns True on success."""
        mem_cfg = self.config.memory_config
        assert mem_cfg is not None  # guaranteed by Config.__post_init__

        logger.info("memory_init start")
        try:
            await self._connect_memory()
            await self._register_memory_tools()
            logger.info("memory_init_done tools=%d", len(self.tool_registry.list_tools()))
            from slife.health import record
            record(
                "memory_service", "ok",
                key="status", value="connected",
                hint="Memory service started and tools registered.",
            )
            return True
        except Exception as e:
            logger.warning("memory_init_failed err=%s — continuing without memory", e)
            from slife.health import record
            record(
                "memory_service", "error",
                key="status", value="failed",
                hint=f"Memory service failed to start: {e}. "
                     "Turn storage and search are unavailable.",
            )
            return False

    async def _connect_memory(self) -> None:
        """Spawn the slife-memory service as a child process via stdio."""
        from slife.mcp.process import MCPWrapperProcess

        mem_cfg = self.config.memory_config
        assert mem_cfg is not None

        logger.info("memory_spawn transport=stdio")
        self._memory_process = MCPWrapperProcess(
            command=sys.executable,
            args=["-m", "slife.plugins.memory.server"],
            server_module="slife.plugins.memory.server",
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

    # ── WeChat lifecycle ───────────────────────────────────────────────

    async def start_wechat(self) -> bool:
        """Start the WeChat plugin if enabled in config. Returns True on success."""
        wechat_cfg = self.config.wechat_config
        if wechat_cfg is None or not wechat_cfg.enabled:
            logger.debug("wechat_not_enabled")
            return False

        logger.info("wechat_init start")
        try:
            await self._connect_wechat()
            await self._register_wechat_tools()
            logger.info("wechat_init_done tools=%d", len(self.tool_registry.list_tools()))
            from slife.health import record
            record(
                "wechat_service", "ok",
                key="status", value="connected",
                hint="WeChat plugin started and tools registered.",
            )
            return True
        except Exception as e:
            logger.warning("wechat_init_failed err=%s — continuing without WeChat", e)
            from slife.health import record
            record(
                "wechat_service", "error",
                key="status", value="failed",
                hint=f"WeChat plugin failed to start: {e}. "
                     "WeChat messaging is unavailable.",
            )
            return False

    async def _connect_wechat(self) -> None:
        """Spawn the slife-wechat service as a child process via stdio."""
        from slife.mcp.process import MCPWrapperProcess

        logger.info("wechat_spawn transport=stdio")
        self._wechat_process = MCPWrapperProcess(
            command=sys.executable,
            args=["-m", "slife.plugins.wechat.server"],
            server_module="slife.plugins.wechat.server",
        )
        await self._wechat_process.start()
        self._wechat_client = await self._wechat_process.create_client()

    async def _register_wechat_tools(self) -> None:
        """Discover and register wechat tools as proxy tools.

        Harness-only tools (drain_incoming, dispatch_reply) are excluded —
        they are called programmatically by the poll loop, not by the LLM.
        """
        from slife.mcp.tool_adapter import create_proxy_tools

        assert self._wechat_client is not None
        wechat_tools = await self._wechat_client.list_tools()
        logger.debug(
            "wechat_tools names=%s",
            [t["name"] for t in wechat_tools],
        )

        # Harness lifecycle — never exposed to LLM
        _HARNESS_TOOLS = {
            "wechat_drain_incoming",
            "wechat_dispatch_reply",
        }

        tagged = [
            {**t, "server": "wechat"}
            for t in wechat_tools
            if t["name"] not in _HARNESS_TOOLS
        ]

        proxy_tools = create_proxy_tools(self._wechat_client, tagged)
        for tool in proxy_tools:
            self.tool_registry.register(tool)
        logger.debug("wechat_tools_registered count=%d", len(proxy_tools))

        # Auto-restore session at startup (triggers server-side poll loop)
        try:
            await self._wechat_client.call_tool("check_status", {})
            logger.debug("wechat_auto_restore_triggered")
        except Exception:
            pass

        # Start background poll loop — injects WeChat messages into the inbox
        self._wechat_poll_task = asyncio.create_task(self._wechat_poll_loop())

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
                assert self._wechat_client is not None

                result = await self._wechat_client.call_tool(
                    "wechat_drain_incoming", {},
                )
                data = _json.loads(result)
                msgs = data.get("messages", [])

                for m in msgs:
                    from_id = m.get("to_user_id", "")
                    ctx_token = m.get("context_token", "")
                    text = m.get("text", "")

                    if not text.strip():
                        continue

                    wc = self._wechat_client  # local ref for closure

                    async def _reply(reply_text: str,
                                     uid=from_id, tok=ctx_token) -> None:
                        try:
                            await wc.call_tool("wechat_dispatch_reply", {
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

    async def stop_wechat(self) -> None:
        """Shut down the WeChat plugin and clean up."""
        if hasattr(self, "_wechat_poll_task") and self._wechat_poll_task:
            self._wechat_poll_task.cancel()
            try:
                await self._wechat_poll_task
            except asyncio.CancelledError:
                pass
            self._wechat_poll_task = None

        if self._wechat_client and self._wechat_client.is_connected:
            try:
                await self._wechat_client.disconnect()
            except Exception as e:
                logger.debug("wechat_disconnect_error err=%s", e)
            self._wechat_client = None

        if self._wechat_process:
            try:
                await self._wechat_process.stop()
            except Exception as e:
                logger.debug("wechat_process_stop_error err=%s", e)
            self._wechat_process = None

        logger.info("wechat_shutdown")

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
        if not self.memory_enabled:
            return

        conv = conversation if conversation is not None else self.conversation

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

        # Trim active context (only for the persistent TUI conversation)
        if conversation is None:
            context_window = self.config.active_model.context_window
            conv.trim_context(
                context_window=context_window,
                floor=self.config.context_floor,
                ceiling=self.config.context_ceiling,
            )

        try:
            await self._memory_client.call_tool(
                "memory_save_turn",
                {
                    "author": self.config.agent_id,
                    "user_message": user_message,
                    "messages": turn_messages,
                    "token_count": token_count or 0,
                    "who_helped": self.config.a2a_config.agent_name or "",
                    "what_model": self.config.active_model.ref,
                    "channel": channel,
                },
            )
        except Exception as e:
            logger.warning("memory_save_error err=%s", e)

    async def get_recent_turns(self, limit: int = 50) -> list[dict]:
        """Load recent turns for restore. Returns [] if no turns."""
        if not self.memory_enabled:
            return []

        try:
            result = await self._memory_client.call_tool(
                "memory_get_recent_turns",
                {"author": self.config.agent_id, "limit": limit},
            )
            data = json.loads(result)
            return data.get("turns", [])
        except Exception as e:
            logger.debug("get_recent_turns_error err=%s", e)
            return []

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

    async def start_a2a(
        self, handler_factory: "Callable[[], Any] | None" = None,
    ) -> None:
        """Connect to MQTT broker for remote agent P2P mesh.

        Called during app startup after MCP initialization.
        Probes for a running Mosquitto broker — if none is found,
        A2A is silently disabled.  Mosquitto must be pre-started
        by the user (slife never spawns it).

        Args:
            handler_factory: Optional callable that creates a TUI handler
                for each incoming A2A task.  When provided, remote tasks
                stream to the chat view just like human-typed messages.
        """
        a2a_cfg = self.config.a2a_config
        if a2a_cfg is None or not a2a_cfg.enabled:
            logger.debug("a2a_disabled")
            return

        logger.info("a2a_init start")

        # Probe for pre-existing Mosquitto — user must start it first
        from slife.a2a.broker import probe_broker
        if not await probe_broker(a2a_cfg.broker_host, a2a_cfg.broker_port):
            logger.info(
                "a2a_broker_not_found host=%s port=%d — A2A disabled",
                a2a_cfg.broker_host, a2a_cfg.broker_port,
            )
            return

        # Broker found — enable A2A
        a2a_cfg.enabled = True

        # Create and connect the A2A client
        from slife.a2a.client import A2AClient, DuplicateAgentError
        self._a2a_client = A2AClient(a2a_cfg)
        try:
            await self._a2a_client.connect()
            from slife.health import record
            record(
                "a2a", "ok",
                key="status", value="connected",
                hint="A2A P2P mesh connected.",
            )
        except DuplicateAgentError as e:
            # Gracefully exit on duplicate agent-id — two instances
            # with the same identity cannot coexist on the MQTT mesh.
            print(f"\n  ✗ {e}\n", file=sys.stderr)
            raise SystemExit(1)
        except Exception as e:
            logger.warning("a2a_connect_failed err=%s", e)
            from slife.health import record
            record(
                "a2a", "warning",
                key="status", value="connect_failed",
                hint=f"A2A client failed to connect: {e}. "
                     "P2P agent mesh is unavailable.",
            )
            a2a_cfg.enabled = False
            return

        # Wire the existing inbox to A2A
        # (Inbox was already created in __init__; now inject the
        # live A2A client and activity callback.)
        self.inbox._a2a_client = self._a2a_client
        self.inbox._on_activity = self._notify_a2a_activity

        # Register handler factory so remote tasks always have a TUI
        # handler — streams to chat like human-typed messages.
        if handler_factory is not None:
            self.inbox._conversations.set_default_handler_factory(handler_factory)

        # Wire A2A incoming tasks → Inbox
        self._a2a_client.on_incoming_task(self.inbox.post)

        # NOTE: inbox background task is already running (started by
        # start_inbox() during on_mount).  No need to restart it here.

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
        """Leave the P2P mesh and clean up.

        Does NOT stop the inbox — the queue is independent of A2A
        and may still be used by human input / WeChat.
        """
        # Disconnect A2A client from inbox
        if self.inbox is not None:
            self.inbox._a2a_client = None
            self.inbox._on_activity = None

        # Clear module-level transport reference
        from slife.a2a.client import clear_client
        clear_client()

        # Disconnect the A2A client
        if self._a2a_client:
            try:
                await self._a2a_client.disconnect()
            except Exception as e:
                logger.debug("a2a_disconnect_error err=%s", e)
        self._a2a_client = None

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
        from slife.health import record
        record(
            "subagent", "ok",
            key="status", value="ready",
            hint=f"Subagent manager ready (max_subagents={self.config.subagent_config['max_subagents']}).",
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
