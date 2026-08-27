"""slife-side MCP adapters.

The MCP machinery (client, oauth, wrapper process, connection pool) moved
to the standalone ``mcp-plugin`` package; what remains in slife is the
boundary adapter that wraps MCP tools as slife native ``Tool`` objects
(:mod:`slife.mcp.tool_adapter`).
"""

from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute, create_proxy_tools

__all__ = [
    "MCPProxyTool",
    "ProxyRoute",
    "create_proxy_tools",
]