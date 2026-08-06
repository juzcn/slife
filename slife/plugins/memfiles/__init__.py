"""Memfiles plugin — serves local files via short random hex tokens.

A lightweight aiohttp server handling ``GET /share/{file_id}``.
Files are served directly from disk — no database, no BLOBs, no crypto.
File IDs are random hex tokens (``secrets.token_hex(15)``, 30 chars)
with a file-backed JSON registry so the server subprocess can resolve
paths without IPC.  Designed to be exposed to the internet via ngrok so
LLMs and users can access shared files via HTTPS.

Auto-discovered by ``discover_plugins()`` and started like any other
plugin — the only difference is that after port discovery the harness
opens an ngrok tunnel instead of an MCP Streamable HTTP connection.
"""
