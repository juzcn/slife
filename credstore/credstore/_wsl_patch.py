"""Monkey-patch keyring-wincred to use WinVault-compatible target naming.

keyring-wincred uses ``{service}:{username}`` target format, but the
native Windows WinVaultKeyring uses ``{username}@{service}``.  This
patch swaps the format so credentials set on native Windows are
directly readable on WSL (and vice versa).

Loaded via keyring entry point — patching happens at import time,
before any backend instances are created.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("credstore.wsl_patch")

try:
    import keyring_wincred as _kw

    _orig = _kw._make_target

    def _make_target(service: str, username: str) -> str:
        """WinVault-compatible target: ``{username}@{service}``."""
        return f"{username}@{service}"

    _kw._make_target = _make_target
    logger.debug("keyring-wincred patched: target format -> {username}@{service}")
except ImportError:
    pass  # Not on WSL / keyring-wincred not installed
