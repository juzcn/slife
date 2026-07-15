"""slife-memory — Permanent memory MCP service with hybrid search.

A FastMCP server that:
  - Records every conversation like a diary (one row = one complete chat)
  - Supports keyword (FTS5) and semantic (sqlite-vec) search
  - Provides crash recovery — detects interrupted sessions at startup
  - Isolates users via the author column (--user flag)
  - Communicates via MCP protocol with the slife agent

Usage:
    uv run python -m slife_memory.server              # auto-detect transport
    uv run python -m slife_memory.server --port 9877  # HTTP mode
"""
