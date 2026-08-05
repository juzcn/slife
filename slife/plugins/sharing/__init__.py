"""Sharing plugin — serves local files via signed URLs.

A lightweight aiohttp server handling ``GET /share/<token>/<filename>``.
Files are served directly from disk — no database, no BLOBs.  The token
carries an HMAC-signed file path.  Designed to be exposed to the internet
via ngrok so LLMs and users can access shared files via HTTPS.

Auto-discovered by ``discover_plugins()`` and started like any other
plugin — the only difference is that after port discovery the harness
opens an ngrok tunnel instead of an MCP Streamable HTTP connection.
"""
