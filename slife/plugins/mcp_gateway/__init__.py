"""mcp_gateway — standalone MCP gateway.

Persistent connections to external MCP servers (stdio, SSE, streamable HTTP),
OAuth 2.0 device-code flow, and a management CLI.  Ships with Slife as its MCP
plugin but has no dependency on it.
"""

__version__ = "0.1.6"

__all__ = ["__version__"]

#: Inter-process stderr contract with the host: the child gateway marks
#: user-facing OAuth lines (``[OAUTH]``) and action requests the host must
#: relay back (``[OAUTH-ACTION]``).  Defined once here, shared by
#: ``process.py`` (parsing stderr) and ``oauth.py`` (emitting).
_OAUTH_MARKER = "[OAUTH]"
_OAUTH_ACTION_MARKER = "[OAUTH-ACTION]"