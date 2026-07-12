"""Agent service layer — wires together LLM, tools, conversation, and loop.

Owns the agent's runtime state. The TUI delegates to this service
rather than directly managing agent internals.
"""

import json
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
        self.tool_registry = create_tools_from_config(config.tools, config=config)
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
        if not self.config.mcp_config.enabled:
            logger.debug("MCP not enabled in config.")
            return

        logger.info("Starting MCP integration...")
        await self._connect_mcp_wrapper()
        await self._register_mcp_wrapper_tools()
        await self._auto_connect_mcp_servers()
        logger.info("MCP integration complete. %d total tools registered.",
                     len(self.tool_registry.list_tools()))

    # ── MCP private helpers ──────────────────────────────────────────

    async def _connect_mcp_wrapper(self) -> None:
        """Detect or spawn the MCP wrapper and establish a connection.

        Always probes wrapper_url first. If an HTTP wrapper is
        already running, connects via HTTP. Otherwise spawns the
        wrapper as a child process via stdio.
        """
        from slife.mcp.client import MCPClient
        from slife.mcp.process import MCPWrapperProcess

        mcp_cfg = self.config.mcp_config

        if await MCPClient.is_wrapper_running(mcp_cfg.wrapper_url):
            logger.info("Found running MCP wrapper at %s, connecting via HTTP...", mcp_cfg.wrapper_url)
            self._mcp_client = MCPClient()
            await self._mcp_client.connect_http(mcp_cfg.wrapper_url)
        else:
            logger.info("No running MCP wrapper found, starting as child process...")
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

        wrapper_tools = await self._mcp_client.list_tools()
        logger.info(
            "MCP wrapper tools discovered: %s",
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
        )
        for tool in proxy_tools:
            self.tool_registry.register(tool)
        logger.info("Registered %d MCP wrapper tools.", len(proxy_tools))

    async def _auto_connect_mcp_servers(self) -> None:
        """Auto-connect to pre-configured MCP servers and discover
        their tools."""
        servers = self.config.mcp_config.servers
        if not servers:
            return

        logger.info("Auto-connecting to %d configured MCP servers...", len(servers))
        for name, cfg in servers.items():
            try:
                result = await self._mcp_client.call_tool(
                    "mcp_add_server",
                    {
                        "name": name,
                        "command": cfg.get("command", ""),
                        "args": cfg.get("args", []),
                        "env": cfg.get("env"),
                    },
                )
                logger.info("Server '%s': %s", name, result)
            except Exception as e:
                logger.error("Failed to auto-connect server '%s': %s", name, e)

        # Discover and register proxy tools for all external servers
        await self._discover_and_register_external_tools()

    # ── MCP tool discovery & registration ────────────────────────────

    async def _discover_and_register_external_tools(self, server_name: str | None = None) -> None:
        """Discover tools from connected MCP servers and register as proxy tools.

        If server_name is provided, only discovers tools for that server.
        Otherwise discovers tools from all connected servers.
        """
        from slife.mcp.tool_adapter import create_proxy_tools

        try:
            tools_json = await self._mcp_client.call_tool(
                "mcp_list_tools", {"server": server_name} if server_name else {}
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
                logger.info(
                    "Registered %d MCP external tools%s.",
                    len(proxy_tools),
                    f" from server '{server_name}'" if server_name else "",
                )
            else:
                if server_name:
                    logger.debug("No tools discovered for server '%s'.", server_name)
                else:
                    logger.info("No external MCP tools discovered.")
        except Exception as e:
            logger.error("Error during MCP tool discovery: %s", e)

    async def _persist_server(self, name: str, command: str, args: list[str], env: dict | None = None, description: str = ""):
        """Callback: persist a newly-added (or updated) MCP server to config
        file and immediately discover and register its tools.

        If a server with the same name already exists, its old proxy tools
        are unregistered first — this handles reconfiguration of servers
        like anyapi-mcp-server when the user provides new args.
        """
        existing = self.config.mcp_config.servers.get(name)
        if existing:
            logger.info(
                "Server '%s' already configured — updating with new args.", name
            )
            self.tool_registry.unregister_by_prefix(f"{name}__")

        self.config.save_mcp_server(name, command, args, env, description)
        # Discover and register the new server's tools right away
        await self._discover_and_register_external_tools(server_name=name)

    async def _unpersist_server(self, name: str):
        """Callback: remove a server from config file and unregister its
        proxy tools from the current session."""
        self.config.remove_mcp_server(name)
        # Unregister any proxy tools belonging to this server
        removed = self.tool_registry.unregister_by_prefix(f"{name}__")
        if removed:
            logger.info("Unregistered %d tools from server '%s'.", removed, name)

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
