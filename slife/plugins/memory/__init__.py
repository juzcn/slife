"""slife.plugins.memory — Diary memory built-in plugin.

A FastMCP server that:
  - Records every conversation like a diary (one row = one turn)
  - Supports keyword (FTS5) and semantic (sqlite-vec) search
  - Isolates users via the author column (--user flag)
  - Communicates via MCP stdio protocol with the slife agent
"""
