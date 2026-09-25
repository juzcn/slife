"""slife-side MCP adapters.

The MCP machinery (client, oauth, wrapper process, connection pool) moved
to the standalone ``mcp-gateway`` package; what remains in slife is the
boundary adapter that wraps MCP tools as slife ``Tool`` objects
(:mod:`slife.mcp.tool_adapter`).
"""