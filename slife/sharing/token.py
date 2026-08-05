"""In-memory file registry for sharing URLs.

Each exposed file gets a random 22-char ID stored in a session-scoped
dict.  The sharing server resolves IDs against this dict — no database,
no HMAC, no persistent state.  When the process exits, all registrations
are gone.  128-bit random IDs prevent enumeration.
"""

from __future__ import annotations

import secrets

# file_id → resolved absolute path
_files: dict[str, str] = {}


def register_file(file_path: str) -> str:
    """Register a file path and return a random 22-char file ID.

    Call ``share_url_for(file_id)`` to build the public URL.
    The file is *not* copied — the server reads it directly from disk.
    """
    file_id = secrets.token_urlsafe(16)  # 128 bits → 22 chars
    _files[file_id] = file_path
    return file_id


def lookup_file(file_id: str) -> str | None:
    """Resolve a file ID back to its absolute path, or ``None``."""
    return _files.get(file_id)
