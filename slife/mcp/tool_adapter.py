"""MCP tool adapter — bridges MCP tools into Slife's Tool interface.

Enables MCP tools (discovered via the slife-mcp wrapper) to be registered
in slife's ToolRegistry and called like native tools.
"""

import json
import logging
from enum import Enum, auto
from typing import ClassVar

from slife.tools.base import Tool

logger = logging.getLogger(__name__)

# ── Proxy routing ──────────────────────────────────────────────────────


class ProxyRoute(Enum):
    """How an :class:`MCPProxyTool` routes execution to its backend."""

    WRAPPER = auto()
    """MCP wrapper tools — direct call + config persistence callbacks."""

    DIRECT = auto()
    """Built-in plugin tools (memdb, wechat) — direct call on own client."""

    EXTERNAL = auto()
    """External MCP server tools — route through ``__mcp_call_tool``."""


# ── Built-in server / tool name constants ─────────────────────────────

_MCP_SERVER = "mcp"           # built-in MCP management server
_MEMDB_SERVER = "memdb"       # built-in memdb service
_WECHAT_SERVER = "wechat"     # built-in WeChat messaging plugin
_MEMFILES_SERVER = "memfiles"   # built-in file cabinet plugin (private)
_SHAREFILE_SERVER = "sharefile"  # built-in public file sharing plugin
_A2A_SERVER = "a2a"           # built-in A2A mesh plugin (MQTT binding)
_MEDIA_SERVER = "media"       # built-in media generation plugin (image/video/TTS/ASR)
_MCP_SET = "mcp_set"
_MCP_SET_ENABLED = "mcp_set_enabled"
_MCP_REMOVE = "mcp_remove"
_MCP_CALL_TOOL = "__mcp_call_tool"


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
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    # Excluded from auto-discovery — instances are created manually by
    # create_proxy_tools() with per-server configuration.
    _skip_auto_register: ClassVar[bool] = True

    def __init__(self, mcp_client, tool_info: dict, route: ProxyRoute = ProxyRoute.EXTERNAL, on_server_added=None, on_server_removed=None, on_server_updated=None):
        """
        Args:
            mcp_client: MCPClient instance connected to the slife-mcp wrapper.
            tool_info: Dict with server, name, description, inputSchema.
            route: How execution is dispatched —
                :attr:`ProxyRoute.WRAPPER` for the MCP management wrapper,
                :attr:`ProxyRoute.DIRECT` for built-in plugins,
                :attr:`ProxyRoute.EXTERNAL` for external MCP servers.
            on_server_added: Optional async callback(name, command, args, env, description, source)
                invoked when mcp_set succeeds, for config persistence.
            on_server_removed: Optional async callback(name)
                invoked when mcp_remove succeeds, for config persistence.
            on_server_updated: Optional async callback(name, enabled)
                invoked when mcp_set_enabled toggles a server, to persist the
                change and update tool registration.
        """
        self._mcp_client = mcp_client
        self._server = tool_info["server"]
        self._tool_name = tool_info["name"]
        self._route = route
        self._on_server_added = on_server_added
        self._on_server_removed = on_server_removed
        self._on_server_updated = on_server_updated

        # Tool name: built-in plugin tools (DIRECT/WRAPPER) register under
        # their bare name (semantic, self-describing — e.g. "turn_search",
        # "note_save", "mcp_set", "wechat_login"); they are first-class like
        # native tools and the `{server}_` prefix inside the name is preserved
        # as-is where the plugin chose it.  External MCP server tools ALWAYS
        # keep the full "{server}__{tool}" namespace: applying a bare-name
        # rule to them would let a server-supplied tool shadow a native tool
        # (e.g. an external server named `check` advertising `check_mcp`) and
        # broke `{name}__` unregistration.
        if self._route == ProxyRoute.EXTERNAL:
            full_name = f"{self._server}__{self._tool_name}"
        else:
            full_name = self._tool_name

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

    # to_openai_function() is inherited from Tool — it already resolves
    # self.name / self.description / self.parameters at instance level,
    # so the override is unnecessary even for instance-level attributes.

    async def execute(self, **kwargs) -> str:
        """Execute the tool by calling through the appropriate MCP client.

        Dispatch is determined by :attr:`_route` — no magic-string
        matching on the server name.
        """
        if self._route == ProxyRoute.WRAPPER:
            # MCP wrapper management tools — direct call with config
            # persistence callbacks.
            source = kwargs.pop("source", None)
            if not isinstance(source, dict):
                source = None

            result = await self._mcp_client.call_tool(self._tool_name, kwargs)

            await self._handle_set(result, source, **kwargs)
            await self._handle_remove(result, **kwargs)
        elif self._route == ProxyRoute.DIRECT:
            # Built-in plugin tools (memdb, wechat) — call directly
            # on the plugin's own MCP client.
            result = await self._mcp_client.call_tool(self._tool_name, kwargs)
        else:
            # ProxyRoute.EXTERNAL — route through the MCP wrapper's
            # __mcp_call_tool to reach the external server.
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

    async def _handle_set(self, result: str, source: dict | None, **kwargs) -> None:
        """Persist mcp_set / mcp_set_enabled side effects."""
        if self._tool_name not in (_MCP_SET, _MCP_SET_ENABLED) or not (self._on_server_added or self._on_server_updated):
            return
        try:
            parsed = json.loads(result)
            status = parsed.get("status", "")
            server_name = kwargs.get("name", "")
            if status == "connected" and self._tool_name == _MCP_SET and self._on_server_added:
                # mcp_set defined a server — persist full config + register tools.
                await self._on_server_added(
                    name=server_name,
                    command=kwargs.get("command", ""),
                    args=kwargs.get("args", []),
                    env=kwargs.get("env"),
                    description=kwargs.get("description", ""),
                    source=source,
                    url=kwargs.get("url", ""),
                    headers=kwargs.get("headers"),
                )
                logger.debug("mcp_persisted server=%s", server_name)
            elif status == "connected" and self._on_server_updated:
                # mcp_set_enabled re-enabled — persist + register tools.
                await self._on_server_updated(name=server_name, enabled=True)
            elif status == "disabled" and self._on_server_updated:
                # enabled=False — persist disabled and unregister tools.
                await self._on_server_updated(name=server_name, enabled=False)
                logger.debug("mcp_disabled server=%s", server_name)
            else:
                logger.info(
                    "mcp_not_persisted server=%s status=%s error=%s",
                    server_name,
                    status,
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

    async def _handle_remove(self, result: str, **kwargs) -> None:
        """Persist MCP server removals to config."""
        if self._tool_name != _MCP_REMOVE or not self._on_server_removed:
            return
        try:
            parsed = json.loads(result)
            if parsed.get("status") == "removed":
                await self._on_server_removed(name=kwargs.get("name", ""))
                logger.debug("mcp_removed server=%s", kwargs.get("name", "?"))
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

def _route_for_server(server: str) -> ProxyRoute:
    """Return the execution route for *server*.

    Single place to decide how a plugin's tools are dispatched —
    no more magic-string matching scattered across the codebase.
    """
    # Built-in plugins that have their own standalone MCP client
    if server in (_MEMDB_SERVER, _WECHAT_SERVER, _MEMFILES_SERVER,
                  _SHAREFILE_SERVER, _A2A_SERVER, _MEDIA_SERVER):
        return ProxyRoute.DIRECT
    # MCP wrapper — has extra config persistence hooks
    if server == _MCP_SERVER:
        return ProxyRoute.WRAPPER
    return ProxyRoute.EXTERNAL


def create_proxy_tools(
    mcp_client, tools: list[dict], on_server_added=None, on_server_removed=None, on_server_updated=None,
) -> list[MCPProxyTool]:
    """Create MCPProxyTool instances from a list of tool info dicts.

    Args:
        mcp_client: MCPClient instance.
        tools: List of tool info dicts, each with:
            server, name, description, inputSchema.
        on_server_added: Optional async callback(name, command, args, env, description, source)
            invoked when mcp_set succeeds.
        on_server_removed: Optional async callback(name)
            invoked when mcp_remove succeeds.
        on_server_updated: Optional async callback(name, enabled)
            invoked when mcp_set_enabled toggles a server.

    Returns:
        List of MCPProxyTool instances ready for ToolRegistry registration.
    """
    return [
        MCPProxyTool(
            mcp_client, t,
            route=_route_for_server(t["server"]),
            on_server_added=on_server_added,
            on_server_removed=on_server_removed,
            on_server_updated=on_server_updated,
        )
        for t in tools
    ]
