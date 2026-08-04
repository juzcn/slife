"""Media server plugin — serves image BLOBs via plain HTTP.

A lightweight aiohttp server handling ``GET /media/<image_id>``,
returning raw image bytes with the correct ``Content-Type`` header.
Designed to be exposed to the internet via ngrok so LLM APIs can
fetch images by URL instead of base64 data URIs.

Auto-discovered by ``discover_plugins()`` and started like any other
plugin — the only difference is that after port discovery the harness
opens an ngrok tunnel instead of an MCP Streamable HTTP connection.
"""
