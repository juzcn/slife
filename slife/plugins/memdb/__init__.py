"""slife.plugins.memdb — Diary memory built-in plugin.

A FastMCP server that:
  - Records every turn like a diary (one row = one turn)
  - Supports keyword (FTS5) and semantic (sqlite-vec) search
  - Isolates agents via separate database files (``--db``)
  - Speaks MCP over Streamable HTTP with the slife agent
"""
