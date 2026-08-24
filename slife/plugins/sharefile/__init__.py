"""Sharefile plugin — public file sharing.

A self-contained, replaceable Streamable HTTP plugin (same contract as
memdb / media): the harness spawns ``server.py``, connects via MCP, and
registers the sharefile tools.  The plugin owns the in-process
token registry, the ngrok tunnel, and serves file bytes on the same port
via a custom HTTP route (``GET /share/{file_id}``).

Its sole LLM-visible tool is ``share_file`` — it publishes a local file
as a public HTTPS URL that multimodal LLM APIs can fetch directly
(instead of inline base64).  Publishing is always the LLM's explicit
choice; the file cabinet (memfiles) never auto-publishes.

LLM-visible tools: ``share_file``.
Internal tools: ``__tunnel_status``, ``__register_file``.
"""
