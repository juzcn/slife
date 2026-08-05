"""Sharing plugin — serves local files via random-URL dict lookup.

A lightweight aiohttp server handling ``GET /share/{file_id}``.
Files are served directly from disk — no database, no BLOBs, no crypto.
file_ids are 128-bit random strings stored in a session-scoped in-memory
dict.  Designed to be exposed to the internet via ngrok so LLMs and users
can access shared files via HTTPS.

Auto-discovered by ``discover_plugins()`` and started like any other
plugin — the only difference is that after port discovery the harness
opens an ngrok tunnel instead of an MCP Streamable HTTP connection.
"""
