"""local-embed — standalone local embedding server.

Loads a local GGUF (llama-cpp) or HF transformer embedding model ONCE and
exposes it as an OpenAI-compatible ``/v1/embeddings`` HTTP endpoint plus
FastMCP tools.  slife's memdb/memfiles plugins both call it over HTTP, so
the model is never loaded twice in one process tree.

Modules::

    server.py     FastMCP plugin — MCP tools + /v1/embeddings custom route
    engine.py     Model engine (gguf / transformer), lazy load + encode
    cli.py        Console entry point (``local-embed``)
    threads.py    run_daemon — daemon-thread offload for blocking model calls
    logging.py    Structured logging setup

Usage::

    local-embed --backend gguf --gguf-path /path/to/model.gguf
    local-embed --backend transformer --model BAAI/bge-m3
"""

try:
    from importlib.metadata import version as _version

    __version__ = _version("local-embed")
except Exception:
    __version__ = "0.0.0"

__all__ = ["__version__"]
