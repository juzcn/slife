"""Memfiles plugin — notes / diary / files cabinet + public file sharing.

A self-contained, replaceable Streamable HTTP plugin (same contract as
memdb / mqtt): the harness spawns ``server.py``, connects via MCP, and
registers the ``memfiles__*`` tools.  The plugin owns the in-process
token registry, the ngrok tunnel, and serves file bytes on the same port
via a custom HTTP route (``GET /share/{file_id}``).

Notes/diary are dual-written to markdown files and a SQLite index
(FTS5 + vec0 hybrid search, reusing memdb's ``SemanticManager``);
saved files are recorded by metadata with an optional LLM summary.

LLM-visible tools: ``note_save``, ``diary_write``, ``file_save``,
``url_save``, ``note_list``, ``diary_list``, ``note_read``, ``diary_read``,
``list_files``, ``search``, ``read``, ``share_file``, ``embedding_check``.
Internal tools: ``__tunnel_status``, ``__register_file``.
"""
