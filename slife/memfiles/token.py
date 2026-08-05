"""Short random hex tokens with a file-backed registry.

Each exposed file gets a short random hex token (30 chars, ``secrets.token_hex(15)``).
Deliberately below 32 chars to avoid the generic ``[A-Za-z0-9]{32,}``
secret-sanitization pattern in ``logfmt.py``.

The token→path mapping is stored in a JSON file so the memfiles server
subprocess can resolve tokens without any shared-memory or IPC.

The registry file path is passed to the subprocess via the
``SLIFE_MEMFILES_REGISTRY`` environment variable — set once at spawn,
never changes.

When the registry is not set (e.g. tests, or memfiles not yet started),
``register_file`` falls back to an in-process dict.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Module state ──────────────────────────────────────────────────────

_registry_path: Path | None = None
_registry_cache: dict[str, str] | None = None

# In-process fallback when the registry file hasn't been created yet
# (e.g. unit tests that mock share_url_for).
_fallback: dict[str, str] = {}


# ── Registry file I/O ─────────────────────────────────────────────────


def _get_registry_path() -> Path | None:
    """Return the registry file path from the environment, or create one."""
    global _registry_path
    if _registry_path is not None:
        return _registry_path
    env = os.environ.get("SLIFE_MEMFILES_REGISTRY")
    if env:
        _registry_path = Path(env)
        return _registry_path
    return None


def _read_registry() -> dict[str, str]:
    """Read the registry file, returning the in-memory copy if fresh."""
    global _registry_cache
    path = _get_registry_path()
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        _registry_cache = data
        return data
    except (OSError, json.JSONDecodeError):
        return _registry_cache or {}


def _write_registry(data: dict[str, str]) -> None:
    """Atomically write the registry file."""
    path = _get_registry_path()
    if path is None:
        return
    payload = json.dumps(data, ensure_ascii=False)
    # Atomic: write to temp file then rename
    fd, tmp = tempfile.mkstemp(
        suffix=".json", prefix=".memfiles_registry.",
        dir=path.parent,
    )
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    Path(tmp).replace(path)
    _registry_cache = data


# ── Public API ────────────────────────────────────────────────────────


def init_registry() -> str:
    """Create a new registry file and return its path.

    Called by the main process before spawning the memfiles server
    subprocess.  The path is placed in ``SLIFE_MEMFILES_REGISTRY``
    so the subprocess inherits it.
    """
    fd, tmp = tempfile.mkstemp(
        suffix=".json", prefix="memfiles_registry_",
    )
    os.close(fd)
    path = Path(tmp)
    path.write_text("{}", encoding="utf-8")
    os.environ["SLIFE_MEMFILES_REGISTRY"] = str(path)
    global _registry_path, _registry_cache
    _registry_path = path
    _registry_cache = {}
    logger.debug("registry_init path=%s", path)
    return str(path)


def cleanup_registry() -> None:
    """Remove the registry file (called at shutdown)."""
    global _registry_path, _registry_cache
    path = _get_registry_path()
    if path is not None and path.exists():
        try:
            path.unlink()
        except OSError:
            pass
    _registry_path = None
    _registry_cache = None
    os.environ.pop("SLIFE_MEMFILES_REGISTRY", None)


def register_file(file_path: str) -> str:
    """Generate a short hex token for *file_path* and persist it.

    Returns a 30-character hex token.  Deliberately below 32 chars to
    avoid matching the generic ``[A-Za-z0-9]{32,}`` secret-sanitization
    pattern.  Uses hex (not base64url) so no underscores break the
    Textual/Rich markdown URL detection.

    Reuses an existing token if *file_path* was already registered.
    """
    path = _get_registry_path()
    if path is None:
        # Fallback: in-process dict (tests, pre-init calls)
        for tok, fp in _fallback.items():
            if fp == file_path:
                return tok
        tok = secrets.token_hex(15)  # 120 bits → 30 chars  (< 32 avoids sanitization)
        _fallback[tok] = file_path
        logger.debug("register_fallback token=%s path=%s", tok, file_path)
        return tok

    registry = _read_registry()

    # Build reverse index once for O(1) duplicate check
    path_to_token: dict[str, str] = {}
    for tok, fp in registry.items():
        path_to_token.setdefault(fp, tok)

    if file_path in path_to_token:
        return path_to_token[file_path]

    tok = secrets.token_hex(15)  # 120 bits → 30 chars  (< 32 avoids sanitization)
    registry[tok] = file_path
    _write_registry(registry)
    logger.debug("register_file token=%s path=%s", tok, file_path)
    return tok


def lookup_file(token: str) -> str | None:
    """Return the file path for *token*, or ``None`` if unknown.

    The memfiles server calls this on ``GET /share/{file_id}``.
    """
    path = _get_registry_path()
    if path is None:
        return _fallback.get(token)

    registry = _read_registry()
    return registry.get(token)
