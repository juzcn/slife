"""Media serving — ngrok tunnel + SQLite BLOB URL builder.

Provides the infrastructure to replace base64-encoded data URIs with
publicly-accessible HTTPS URLs for LLM vision API calls.

``media_url_for(image_id)`` is the single entry point for building
public image URLs.  When no tunnel is active it returns ``None`` —
callers skip image injection silently.  No base64 fallback.
"""
