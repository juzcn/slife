"""Sharing — ngrok tunnel + in-memory file registry.

Provides the infrastructure to expose local files via publicly-accessible
HTTPS URLs through an ngrok tunnel.  Files are registered with random IDs
in a session-scoped dict and served directly from disk — no database,
no crypto, no BLOBs.

``share_url_for(file_id)`` builds a public share URL.
``register_file(file_path)`` stores a path and returns a random file ID.
``lookup_file(file_id)`` resolves an ID back to a path.
"""
