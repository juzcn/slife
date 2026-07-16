"""Slife built-in plugins — MCP stdio services.

Each plugin is a FastMCP server that runs as a child process,
connected via MCPClient stdio transport. Tools are auto-discovered
and registered as MCPProxyTool instances.

Built-in plugins:
  - slife.plugins.mcp:    MCP proxy — manage external MCP server connections
  - slife.plugins.memory: Diary database — permanent conversation storage
"""
