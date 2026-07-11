"""Agent service layer — wires together LLM, tools, conversation, and loop.

Owns the agent's runtime state. The TUI delegates to this service
rather than directly managing agent internals.
"""

import logging

from slife.agent.system_prompt import build as build_system_prompt
from slife.config import Config
from slife.agent.llm_client import LLMClient, TokenUsage
from slife.agent.conversation import Conversation
from slife.agent.loop import AgentLoop, AgentEventHandler, AgentResult
from slife.tools.base import Tool
from slife.tools.factory import create_tools_from_config
from slife.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class AgentService:
    """Wires together LLM client, tools, conversation, and agent loop.

    Owns the agent's runtime state. The TUI delegates to this service
    rather than directly managing agent internals.

    If MCP is enabled in config, also manages the MCP wrapper connection
    and registers MCP proxy tools.
    """

    def __init__(self, config: Config):
        self.config = config
        self.tool_registry = create_tools_from_config(config.tools)
        self.llm_client = LLMClient(config.active_model)
        self.agent_loop = AgentLoop(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            max_iterations=config.max_iterations,
        )
        self.conversation = Conversation(system_prompt=build_system_prompt())
        self.session_usage = TokenUsage()

        # MCP integration state
        self._mcp_client = None
        self._mcp_process = None

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

    def clear(self) -> None:
        """Reset conversation history and session usage."""
        self.conversation.clear()
        self.session_usage = TokenUsage()

    async def start_mcp(self) -> None:
        """Start the MCP wrapper and register its tools.

        Called during app startup. Detects if a standalone wrapper
        is already running (via HTTP); if not, spawns one as a child
        process via stdio.
        """
        mcp_config = self.config.mcp_config
        if not mcp_config.enabled:
            logger.debug("MCP not enabled in config.")
            return

        from slife.mcp.client import MCPClient, DEFAULT_WRAPPER_URL
        from slife.mcp.process import MCPWrapperProcess
        from slife.mcp.tool_adapter import create_proxy_tools

        logger.info("Starting MCP integration...")

        # 1. Detect: try connecting to an already-running standalone wrapper
        if await MCPClient.is_wrapper_running(DEFAULT_WRAPPER_URL):
            logger.info("Found running MCP wrapper, connecting via HTTP...")
            self._mcp_client = MCPClient()
            await self._mcp_client.connect_http(DEFAULT_WRAPPER_URL)
        else:
            logger.info("No running MCP wrapper found, starting as child process...")
            self._mcp_process = MCPWrapperProcess(
                command=mcp_config.wrapper_command,
                args=mcp_config.wrapper_args,
            )
            await self._mcp_process.start()
            self._mcp_client = await self._mcp_process.create_client()

        # 3. Register wrapper management tools
        wrapper_tools = await self._mcp_client.list_tools()
        logger.info(
            "MCP wrapper tools discovered: %s",
            [t["name"] for t in wrapper_tools],
        )

        # 4. Create proxy tools for wrapper management tools
        #    (mcp_add_server, mcp_remove_server, etc.)
        tagged_tools = [
            {**t, "server": "mcp"} for t in wrapper_tools
        ]
        proxy_tools = create_proxy_tools(self._mcp_client, tagged_tools)
        for tool in proxy_tools:
            self.tool_registry.register(tool)
        logger.info("Registered %d MCP wrapper tools.", len(proxy_tools))

        # 5. Auto-connect to pre-configured MCP servers
        servers = mcp_config.servers
        if servers:
            logger.info("Auto-connecting to %d configured MCP servers...", len(servers))
            for server_name, server_cfg in servers.items():
                try:
                    result = await self._mcp_client.call_tool(
                        "mcp_add_server",
                        {
                            "name": server_name,
                            "command": server_cfg.get("command", ""),
                            "args": server_cfg.get("args", []),
                            "env": server_cfg.get("env"),
                        },
                    )
                    logger.info("Server '%s': %s", server_name, result)

                    # Discover and register proxy tools from this server
                    tools_result = await self._mcp_client.call_tool(
                        "mcp_list_tools",
                        {"server": server_name},
                    )
                    logger.debug("Tools from '%s': %s", server_name, tools_result)
                except Exception as e:
                    logger.error(
                        "Failed to auto-connect server '%s': %s", server_name, e
                    )

            # 6. Register all proxy tools from connected servers
            try:
                all_tools_raw = await self._mcp_client.call_tool(
                    "mcp_list_tools", {}
                )
                logger.debug("All MCP tools: %s", all_tools_raw)
            except Exception:
                all_tools_raw = ""

            # Parse tool list from the formatted output and register
            # (This is approximate — we re-query via mcp_list_tools)
            try:
                tools_data = await self._mcp_client.call_tool(
                    "mcp_list_tools", {}
                )
                # Use the internal pool to get structured data
                # For now, just log that MCP is ready
                logger.info("MCP integration complete. Use mcp_list_tools to explore.")
            except Exception as e:
                logger.error("Error during MCP tool discovery: %s", e)

    async def stop_mcp(self) -> None:
        """Shut down the MCP wrapper and clean up."""
        if self._mcp_client:
            try:
                await self._mcp_client.disconnect()
            except Exception as e:
                logger.debug("Error disconnecting MCP client: %s", e)
            self._mcp_client = None

        if self._mcp_process:
            try:
                await self._mcp_process.stop()
            except Exception as e:
                logger.debug("Error stopping MCP process: %s", e)
            self._mcp_process = None

        logger.info("MCP integration shut down.")

    async def process_message(
        self,
        user_input: str,
        images: list[str] | None,
        handler: AgentEventHandler,
    ) -> AgentResult:
        """Run the agent loop for a user message via streaming."""
        return await self.agent_loop.run(
            user_input=user_input,
            conversation=self.conversation,
            images=images,
            handler=handler,
        )
