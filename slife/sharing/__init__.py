"""Sharing — ngrok tunnel + HMAC-signed file tokens.

Provides the infrastructure to expose local files via publicly-accessible
HTTPS URLs through an ngrok tunnel.  File paths are signed with HMAC-SHA256
and encoded in the URL token — the sharing server (subprocess) verifies
the signature to extract the path.  No shared state, no database, no BLOBs.

``share_url_for(file_id)`` builds a public share URL.
``register_file(file_path)`` signs a path and returns a URL-safe token.
``lookup_file(token)`` verifies the HMAC and returns the file path (or ``None``).
"""
