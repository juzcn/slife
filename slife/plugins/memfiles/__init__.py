"""Memfiles plugin — file cabinet + public file sharing, as a standard plugin.

A self-contained, replaceable Streamable HTTP plugin (same contract as
memdb / mqtt): the harness spawns ``server.py``, connects via MCP, and
registers the ``memfiles__*`` tools.  The plugin owns the in-process
token registry, the ngrok tunnel, and serves file bytes on the same port
via a custom HTTP route (``GET /share/{file_id}``).

LLM-visible tools: ``expose_file``, ``save_content_or_files``.
Harness-only tools: ``__tunnel_status``, ``__register_file``.
"""
