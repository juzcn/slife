"""HMAC-signed file tokens for memfiles URLs.

Each exposed file gets a signed token that carries the file path.
The memfiles server (subprocess) verifies the HMAC to extract the path —
no shared state, no database, no IPC needed.  The token is the authority.

The secret is generated per-session and passed to the subprocess via the
``SLIFE_MEMFILES_SECRET`` environment variable.

Token format (binary, before base64url encoding)::

    file_path_utf8 + "." + hmac_sha256(file_path_utf8, secret)

The 32-byte HMAC prevents path forgery; the dot separator delimits the
variable-length path from the fixed-length signature.

When the secret is not set (e.g. tests, or memfiles not yet started),
``register_file`` falls back to a random 22-char token — not verifiable
by any server, but harmless.  ``lookup_file`` returns ``None`` in that
case since it cannot verify anything.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import secrets

logger = logging.getLogger(__name__)

# ── In-process fallback for when the secret is not set ────────────────
# Used only as a graceful degradation path (e.g. unit tests that mock
# share_url_for).  The memfiles server subprocess always has the secret
# set and never touches this dict.
_fallback_files: dict[str, str] = {}


def _get_secret() -> bytes | None:
    """Return the memfiles secret, or ``None`` when not set."""
    raw = os.environ.get("SLIFE_MEMFILES_SECRET")
    if not raw:
        return None
    return raw.encode("utf-8")


def register_file(file_path: str) -> str:
    """Sign *file_path* and return a URL-safe token.

    When ``SLIFE_MEMFILES_SECRET`` is set (normal operation), the token is
    ``base64url(path_bytes + "." + hmac-sha256)``, carrying its own
    authority — verifiable by the memfiles server subprocess.

    When the secret is **not** set (tests, memfiles not started), falls
    back to a random 22-char token stored in an in-process dict.  Such
    tokens cannot be verified by the memfiles server, but the fallback
    prevents crashes in code paths that call ``register_file`` before
    the memfiles infrastructure is ready.

    The file is **not** copied — the memfiles server reads it from disk.
    """
    secret = _get_secret()
    if secret is None:
        token = secrets.token_urlsafe(16)  # 128 bits → 22 chars
        _fallback_files[token] = file_path
        logger.debug("register_fallback token=%s path=%s", token, file_path)
        return token

    path_bytes = file_path.encode("utf-8")
    sig = hmac.new(secret, path_bytes, hashlib.sha256).digest()
    signed = path_bytes + b"." + sig
    # urlsafe_b64encode produces padding with "="; strip it so the token
    # looks cleaner in URLs.  lookup_file() pads back before decoding.
    return base64.urlsafe_b64encode(signed).rstrip(b"=").decode("ascii")


def lookup_file(token: str) -> str | None:
    """Verify *token* and return the file path, or ``None`` if invalid.

    The memfiles server calls this on every ``GET /share/{file_id}``
    request.  Because the token carries the HMAC, no process-shared
    state is required.
    """
    secret = _get_secret()

    # Fallback path: no secret set → check in-process dict (tests only)
    if secret is None:
        return _fallback_files.get(token)

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
