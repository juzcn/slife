"""Signed URL tokens for local file sharing.

Each share URL embeds the file path and an HMAC signature that proves
the URL was issued by this server.  No database or state — the token
*is* the authorization.

URLs live forever and stop working only when the file is deleted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from pathlib import Path

_KEY_PATH = Path.home() / ".slife" / "share_signing_key"

_key: bytes | None = None


def _load_key() -> bytes:
    """Load or create the persistent signing key."""
    global _key
    if _key is not None:
        return _key
    if _KEY_PATH.exists():
        _key = _KEY_PATH.read_bytes()
    else:
        _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _key = secrets.token_bytes(32)
        _KEY_PATH.write_bytes(_key)
    return _key


def sign_path(file_path: str) -> str:
    """Sign a file path, returning an opaque token string.

    Format: ``{path_b64}.{hmac_hex}``

    The path is base64-encoded (not encrypted) — the HMAC prevents
    forgery but the path itself is visible to anyone who decodes the
    base64 portion.  This is intentional: path obscurity is not the
    security model; tamper-proofing is.
    """
    key = _load_key()
    path_b64 = base64.urlsafe_b64encode(file_path.encode()).rstrip(b"=").decode()
    sig = hmac.new(key, path_b64.encode(), hashlib.sha256).hexdigest()
    return f"{path_b64}.{sig}"


def verify_token(token: str) -> str | None:
    """Validate a signed token and return the file path.

    Returns ``None`` if the token has been tampered with or is malformed.
    """
    try:
        path_b64, sig = token.rsplit(".", 1)
    except ValueError:
        return None

    key = _load_key()
    expected = hmac.new(key, path_b64.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None

    # Restore base64 padding
    padding = 4 - len(path_b64) % 4
    if padding != 4:
        path_b64 += "=" * padding

    try:
        return base64.urlsafe_b64decode(path_b64).decode()
    except Exception:
        return None
