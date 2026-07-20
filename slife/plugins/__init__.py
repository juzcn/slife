"""Slife built-in plugins — non‑standard MCP stdio services.

Each plugin runs as an independent child process and communicates
with Slife via JSON-RPC over stdin/stdout (the MCP stdio transport).
They use FastMCP as the server framework, but they are **not**
generic MCP services — they are Slife‑specific plugins that borrow
the MCP stdio protocol as an IPC mechanism.

Tools are auto-discovered via ``list_tools`` and registered as
``MCPProxyTool`` instances in the agent's ``ToolRegistry``.

Harness-only tools (prefixed or listed in a denylist) are called
programmatically by AgentService — they are never exposed to the LLM.

Built-in plugins:
  - slife.plugins.mcp:    MCP proxy — manage external MCP server connections
  - slife.plugins.memory: Diary database — permanent conversation storage
  - slife.plugins.wechat: WeChat messaging — bidirectional iLink ClawBot bridge
"""
