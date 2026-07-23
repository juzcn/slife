"""MCP tool adapter — bridges MCP tools into Slife's Tool interface.

Enables MCP tools (discovered via the slife-mcp wrapper) to be registered
in slife's ToolRegistry and called like native tools.
"""

import json
import logging
from typing import ClassVar

from slife.tools.base import Tool

logger = logging.getLogger(__name__)

# ── Built-in server / tool name constants ─────────────────────────────

_MCP_SERVER = "mcp"           # built-in MCP management server
_MEMORY_SERVER = "memory"     # built-in memory service
_WECHAT_SERVER = "wechat"     # built-in WeChat messaging plugin
_MCP_ADD_SERVER = "mcp_add_server"
_MCP_SET_DISCLOSURE = "mcp_set_disclosure"
_MCP_REMOVE_SERVER = "mcp_remove_server"
_MCP_CALL_TOOL = "mcp_call_tool"


class MCPProxyTool(Tool):
    """Adapts a single MCP tool to Slife's Tool ABC.

    An instance represents one tool from a connected external MCP server,
    made available to the LLM via slife's standard tool system.

    The tool name is prefixed with the server name to avoid collisions
    (e.g. 'filesystem__read_file').

    Class-level attributes are placeholders — real values are set at
    instance level via object.__setattr__ for each instance.
    """

    # Placeholder class attrs to pass Tool.__init_subclass__ validation.
    # Real values are set per-instance in __init__.
    name = "_mcp_proxy"
    description = "MCP proxy tool (placeholder)"
    parameters: dict = {"type": "object", "properties": {}}

    # Excluded from auto-discovery — instances are created manually by
    # create_proxy_tools() with per-server configuration.
    _skip_auto_register: ClassVar[bool] = True

    def __init__(self, mcp_client, tool_info: dict, on_server_added=None, on_server_removed=None, on_server_disclosure_changed=None, require_approval: bool = False):
        """
        Args:
            mcp_client: MCPClient instance connected to the slife-mcp wrapper.
            tool_info: Dict with server, name, description, inputSchema.
            on_server_added: Optional async callback(name, command, args, env, description, source)
                invoked when mcp_add_server succeeds, for config persistence.
            on_server_removed: Optional async callback(name)
                invoked when mcp_remove_server succeeds, for config persistence.
            on_server_disclosure_changed: Optional async callback(name, disclosure)
                invoked when mcp_set_disclosure succeeds, to persist and update tools.
            require_approval: If True, the agent loop will request user
                confirmation before executing this tool.
        """
        self._mcp_client = mcp_client
        self._server = tool_info["server"]
        self._tool_name = tool_info["name"]
        self._on_server_added = on_server_added
        self._on_server_removed = on_server_removed
        self._on_server_disclosure_changed = on_server_disclosure_changed

        # Namespaced tool name: "server__toolname"
        full_name = f"{self._server}__{self._tool_name}"

        # Override class-level attrs at instance level with real values
        object.__setattr__(self, "name", full_name)
        object.__setattr__(self, "requires_approval", require_approval)

        desc = tool_info.get("description", "")
        server_prefix = f"[{self._server}] "
        object.__setattr__(self, "description", server_prefix + desc)

        schema = tool_info.get("inputSchema", {})
        # Ensure valid JSON Schema object type
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema = {
                "type": "object",
                "properties": schema.get("properties", {}),
                "required": schema.get("required", []),
            }
        object.__setattr__(self, "parameters", schema)

    def to_openai_function(self) -> dict:
        """Convert to OpenAI function definition using instance-level values.

        Overrides the base classmethod because MCPProxyTool sets its
        name/description/parameters at instance level (not class level).
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def execute(self, **kwargs) -> str:
        """Execute the tool by calling through the appropriate MCP client.

        Three paths:
          - Wrapper tools (built-in MCP management): call directly, with
            config persistence callbacks.
          - Memory tools (built-in memory service): call directly on the
            standalone memory MCP client — no routing layer needed.
          - External MCP server tools: route via mcp_call_tool on the
            slife-mcp wrapper.
        """
        if self._server == _MCP_SERVER:
            # Strip source from kwargs — wrapper doesn't need it,
            # it's only for the persistence callback.
            source = kwargs.pop("source", None)
            if not isinstance(source, dict):
                source = None

            # Wrapper management tool — call directly
            result = await self._mcp_client.call_tool(self._tool_name, kwargs)

            # Side-effect callbacks for config persistence
            await self._handle_add_server(result, source, **kwargs)
            await self._handle_set_disclosure(result, **kwargs)
            await self._handle_remove_server(result, **kwargs)
        elif self._server == _MEMORY_SERVER:
            # Memory tools — call directly on the memory MCP client.
            # The memory service is standalone (not behind the MCP wrapper),
            # so there's no mcp_call_tool routing layer.
            result = await self._mcp_client.call_tool(self._tool_name, kwargs)
        elif self._server == _WECHAT_SERVER:
            # WeChat tools — call directly on the wechat MCP client.
            # The wechat service is standalone (not behind the MCP wrapper),
            # so there's no mcp_call_tool routing layer.
            result = await self._mcp_client.call_tool(self._tool_name, kwargs)
        else:
            # External MCP server tool — route through mcp_call_tool
            result = await self._mcp_client.call_tool(
                _MCP_CALL_TOOL,
                {
                    "server": self._server,
                    "tool_name": self._tool_name,
                    "arguments": json.dumps(kwargs, ensure_ascii=False),
                },
            )
        return result

    # ── Callback helpers ────────────────────────────────────────────

    async def _handle_add_server(self, result: str, source: dict | None, **kwargs) -> None:
        """Persist newly added MCP servers to config."""
        if self._tool_name != _MCP_ADD_SERVER or not self._on_server_added:
            return
        try:
            parsed = json.loads(result)
            if parsed.get("status") == "connected":
                await self._on_server_added(
                    name=kwargs.get("name", ""),
                    command=kwargs.get("command", ""),
                    args=kwargs.get("args", []),
                    env=kwargs.get("env"),
                    description=kwargs.get("description", ""),
                    source=source,
                    url=kwargs.get("url", ""),
                    headers=kwargs.get("headers"),
                )
            else:
                logger.info(
                    "mcp_not_persisted server=%s status=%s error=%s",
                    kwargs.get("name", "?"),
                    parsed.get("status", "?"),
                    parsed.get("error", "?"),
                )
        except json.JSONDecodeError:
            logger.warning(
                "mcp_persist_parse_fail server=%s result=%.200s",
                kwargs.get("name", "?"), result[:200],
            )
        except Exception:
            logger.exception(
                "mcp_persist_callback_failed server=%s",
                kwargs.get("name", "?"),
            )

    async def _handle_set_disclosure(self, result: str, **kwargs) -> None:
        """Persist disclosure changes and trigger eager tool registration."""
        if self._tool_name != _MCP_SET_DISCLOSURE or not self._on_server_disclosure_changed:
            return
        try:
            parsed = json.loads(result)
            new_disclosure = parsed.get("disclosure", "")
            if new_disclosure in ("eager", "lazy"):
                await self._on_server_disclosure_changed(
                    name=kwargs.get("name", ""),
                    disclosure=new_disclosure,
                )
        except json.JSONDecodeError:
            logger.warning(
                "mcp_disclosure_parse_fail result=%.200s", result[:200],
            )
        except Exception:
            logger.exception("mcp_disclosure_callback_failed")

    async def _handle_remove_server(self, result: str, **kwargs) -> None:
        """Persist MCP server removals to config."""
        if self._tool_name != _MCP_REMOVE_SERVER or not self._on_server_removed:
            return
        try:
            parsed = json.loads(result)
            if parsed.get("status") == "removed":
                await self._on_server_removed(name=kwargs.get("name", ""))
            else:
                logger.info(
                    "mcp_not_unpersisted server=%s status=%s",
                    kwargs.get("name", "?"),
                    parsed.get("status", "?"),
                )
        except json.JSONDecodeError:
            logger.warning(
                "mcp_removal_parse_fail server=%s result=%.200s",
                kwargs.get("name", "?"), result[:200],
            )
        except Exception:
            logger.exception(
                "mcp_removal_callback_failed server=%s",
                kwargs.get("name", "?"),
            )


def create_proxy_tools(
    mcp_client, tools: list[dict], on_server_added=None, on_server_removed=None, on_server_disclosure_changed=None, require_approval: bool = False,
) -> list[MCPProxyTool]:
    """Create MCPProxyTool instances from a list of tool info dicts.

    Args:
        mcp_client: MCPClient instance.
        tools: List of tool info dicts, each with:
            server, name, description, inputSchema.
        on_server_added: Optional async callback(name, command, args, env, description, source)
            invoked when mcp_add_server succeeds.
        on_server_removed: Optional async callback(name)
            invoked when mcp_remove_server succeeds.
        on_server_disclosure_changed: Optional async callback(name, disclosure)
            invoked when mcp_set_disclosure succeeds.
        require_approval: If True, all tools in this batch require user
            approval before execution.

    Returns:
        List of MCPProxyTool instances ready for ToolRegistry registration.
    """
    return [
        MCPProxyTool(
            mcp_client, t,
            on_server_added=on_server_added,
            on_server_removed=on_server_removed,
            on_server_disclosure_changed=on_server_disclosure_changed,
            require_approval=require_approval,
        )
        for t in tools
    ]
