"""Memfiles — ngrok tunnel + file-backed token registry.

Provides the infrastructure to expose local files via publicly-accessible
HTTPS URLs through an ngrok tunnel.  File tokens are short random strings
stored in a JSON registry file — the memfiles server subprocess reads
the registry to resolve tokens to file paths.  No HMAC, no shared state.

``register_file(file_path)`` returns a short URL-safe token.
``lookup_file(token)`` returns the file path (or ``None``).
``share_url_for(file_id)`` builds a public share URL via the tunnel.
"""
