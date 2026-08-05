"""HMAC-signed file tokens for sharing URLs.

Each exposed file gets a signed token that carries the file path.
The sharing server (subprocess) verifies the HMAC to extract the path —
no shared state, no database, no IPC needed.  The token is the authority.

The secret is generated per-session and passed to the subprocess via the
``SLIFE_SHARING_SECRET`` environment variable.

Token format (binary, before base64url encoding)::

    file_path_utf8 + "." + hmac_sha256(file_path_utf8, secret)

The 32-byte HMAC prevents path forgery; the dot separator delimits the
variable-length path from the fixed-length signature.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os


def _get_secret() -> bytes:
    """Return the sharing secret from the environment.

    Raises ``RuntimeError`` if the secret has not been set (sharing server
    started before the main process generated one).
    """
    raw = os.environ.get("SLIFE_SHARING_SECRET")
    if not raw:
        raise RuntimeError(
            "SLIFE_SHARING_SECRET is not set — the sharing server must be "
            "started by the main process which generates the secret."
        )
    return raw.encode("utf-8")


def register_file(file_path: str) -> str:
    """Sign *file_path* and return a URL-safe token.

    The token is ``base64url(path_bytes + "." + hmac-sha256)``, carrying
    its own authority — no shared state needed.  Pass the returned token
    to ``share_url_for()`` to build the public URL.

    The file is **not** copied — the sharing server reads it from disk.
    """
    secret = _get_secret()
    path_bytes = file_path.encode("utf-8")
    sig = hmac.new(secret, path_bytes, hashlib.sha256).digest()
    signed = path_bytes + b"." + sig
    # urlsafe_b64encode produces padding with "="; strip it so the token
    # looks cleaner in URLs.  lookup_file() pads back before decoding.
    return base64.urlsafe_b64encode(signed).rstrip(b"=").decode("ascii")


def lookup_file(token: str) -> str | None:
    """Verify *token* and return the file path, or ``None`` if invalid.

    The sharing server calls this on every ``GET /share/{file_id}`` request.
    Because the token carries the HMAC, no process-shared state is required.
    """
    secret = _get_secret()

    # Restore padding stripped by register_file()
    padded = token + "=" * (-len(token) % 4)
    try:
        data = base64.urlsafe_b64decode(padded)
    except (ValueError, binascii.Error):
        return None

    # Binary format:  path_bytes + "." + sig(32 bytes sha256)
    # Minimum length: 1-byte path + 1 dot + 32 sig = 34
    if len(data) < 34:
        return None
    if data[-33:-32] != b".":
        return None

    path_bytes = data[:-33]
    sig = data[-32:]

    expected_sig = hmac.new(secret, path_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected_sig):
        return None

    return path_bytes.decode("utf-8")
