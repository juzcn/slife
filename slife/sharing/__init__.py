"""Sharing — ngrok tunnel + signed URL builder.

Provides the infrastructure to expose local files via publicly-accessible
HTTPS URLs through an ngrok tunnel.  Files are served directly from disk
via signed tokens — no database, no BLOBs.

``share_url_for(token, filename)`` builds a public share URL.
``sign_path(file_path)`` creates a signed token for a file path.
"""
