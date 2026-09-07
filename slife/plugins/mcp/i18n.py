"""Minimal user-facing strings for mcp_plugin (slife-free).

The standalone plugin ships a single hardcoded EN string — the OAuth
notification title that slife's ``ui.i18n`` used to provide.  Kept as a
``t()`` call site so the shape matches the old harness call exactly and a
future i18n table can be dropped in without touching ``process.py``.
"""

_STRINGS = {
    "notify_oauth_title": "MCP Authorization Required",
}


def t(key: str) -> str:
    """Return the user-facing string for *key* (fallback: the key itself)."""
    return _STRINGS.get(key, key)