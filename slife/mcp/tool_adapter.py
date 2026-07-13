"""MCP tool adapter — bridges MCP tools into slife's Tool interface.

Enables MCP tools (discovered via the slife-mcp wrapper) to be registered
in slife's ToolRegistry and called like native tools.
"""

import json
import logging

from slife.tools.base import Tool

logger = logging.getLogger(__name__)


class MCPProxyTool(Tool):
    """Adapts a single MCP tool to slife's Tool ABC.

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

    def __init__(self, mcp_client, tool_info: dict, on_server_added=None, on_server_removed=None, on_server_disclosure_changed=None):
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

        logger.debug(
            "Created proxy tool: %s (server=%s, args=%d)",
            full_name,
            self._server,
            len(schema.get("properties", {})),
        )

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
        """Execute the tool by calling through the MCP wrapper.

        Two paths:
          - Wrapper tools (server="mcp"): call the tool directly on the wrapper.
          - External MCP server tools: route via mcp_call_tool.
        """
        logger.debug(
            "MCP proxy: %s/%s(%s)",
            self._server,
            self._tool_name,
            kwargs,
        )
        if self._server == "mcp":
            # Strip source from kwargs — wrapper doesn't need it,
            # it's only for the persistence callback.
            source = kwargs.pop("source", None) if isinstance(kwargs.get("source"), dict) else None

            # Wrapper management tool — call directly
            result = await self._mcp_client.call_tool(
                self._tool_name, kwargs
            )

            # Persist newly added servers to config file
            if self._tool_name == "mcp_add_server" and self._on_server_added:
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
                        )
                    else:
                        logger.info(
                            "MCP server '%s' not persisted: status=%s error=%s",
                            kwargs.get("name", "?"),
                            parsed.get("status", "?"),
                            parsed.get("error", "?"),
                        )
                except json.JSONDecodeError:
                    logger.warning(
                        "MCP server '%s': could not parse result for persistence: %s",
                        kwargs.get("name", "?"), result[:200],
                    )
                except Exception:
                    logger.exception(
                        "MCP server '%s': persistence callback failed",
                        kwargs.get("name", "?"),
                    )

            # Change disclosure mode → persist + register tools if eager
            if self._tool_name == "mcp_set_disclosure" and self._on_server_disclosure_changed:
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
                        "Disclosure change: could not parse result: %s",
                        result[:200],
                    )
                except Exception:
                    logger.exception("Disclosure change callback failed")

            # Persist server removals to config file
            if self._tool_name == "mcp_remove_server" and self._on_server_removed:
                try:
                    parsed = json.loads(result)
                    if parsed.get("status") == "removed":
                        await self._on_server_removed(name=kwargs.get("name", ""))
                    else:
                        logger.info(
                            "MCP server '%s' not unpersisted: status=%s",
                            kwargs.get("name", "?"),
                            parsed.get("status", "?"),
                        )
                except json.JSONDecodeError:
                    logger.warning(
                        "MCP server '%s': could not parse result for removal: %s",
                        kwargs.get("name", "?"), result[:200],
                    )
                except Exception:
                    logger.exception(
                        "MCP server '%s': removal persistence callback failed",
                        kwargs.get("name", "?"),
                    )
        else:
            # External MCP server tool — route through mcp_call_tool
            result = await self._mcp_client.call_tool(
                "mcp_call_tool",
                {
                    "server": self._server,
                    "tool_name": self._tool_name,
                    "arguments": json.dumps(kwargs, ensure_ascii=False),
                },
            )
        return result


def create_proxy_tools(
    mcp_client, tools: list[dict], on_server_added=None, on_server_removed=None, on_server_disclosure_changed=None
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

    Returns:
        List of MCPProxyTool instances ready for ToolRegistry registration.
    """
    return [
        MCPProxyTool(
            mcp_client, t,
            on_server_added=on_server_added,
            on_server_removed=on_server_removed,
            on_server_disclosure_changed=on_server_disclosure_changed,
        )
        for t in tools
    ]
