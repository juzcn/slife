"""MCP client integration for Slife.

Provides:
  - MCPClient: connects to the slife-mcp wrapper via stdio
  - MCPProxyTool: adapts MCP tools to slife's Tool interface
  - MCPWrapperProcess: manages the wrapper child process lifecycle
"""

from slife.mcp.client import MCPClient
from slife.mcp.tool_adapter import MCPProxyTool, create_proxy_tools
from slife.mcp.process import MCPWrapperProcess

__all__ = [
    "MCPClient",
    "MCPProxyTool",
    "create_proxy_tools",
    "MCPWrapperProcess",
]
