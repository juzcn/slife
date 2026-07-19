"""slife.plugins.memory — Diary memory built-in plugin.

A FastMCP server that:
  - Records every conversation like a diary (one row = one turn)
  - Supports keyword (FTS5) and semantic (sqlite-vec) search
  - Isolates agents via the author column (--agent flag)
  - Communicates via MCP stdio protocol with the slife agent
"""
