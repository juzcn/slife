"""Monkey-patch keyring-wincred for WSL compatibility — two fixes:

1. **Target format** — keyring_wincred defaults to ``{service}:{username}``,
   but the native Windows WinVaultKeyring stores as ``{username}@{service}``.
   Patch to match so Windows credentials are readable from WSL.

2. **PowerShell encoding** — ``subprocess.run(text=True)`` decodes with
   Linux locale (UTF-8), but ``powershell.exe`` outputs Windows OEM bytes.
   Use ``encoding='utf-8', errors='replace'`` to avoid UnicodeDecodeError.
"""

from __future__ import annotations

import base64
import logging
import subprocess

logger = logging.getLogger("credstore.wsl_patch")

try:
    import keyring_wincred as _kw
    import keyring_wincred.powershell as _ps

    # ── patch 1: WinVault-compatible target format ────────────────

    def _make_target(service: str, username: str) -> str:
        return f"{username}@{service}"

    _kw._make_target = _make_target

    # ── patch 2: fix PowerShell encoding ──────────────────────────

    _CS = _ps._CRED_MANAGER_CS

    def _run_powershell(script: str) -> tuple[int, str, str]:
        full_script = _CS + "\n" + script
        script_bytes = full_script.encode("utf-16-le")
        encoded_script = base64.b64encode(script_bytes).decode("ascii")
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded_script,
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        # Trace first few calls to diagnose credential lookup issues
        _run_powershell._call_count = getattr(_run_powershell, "_call_count", 0) + 1
        if _run_powershell._call_count <= 5:
            logger.info(
                "powershell call #%d: rc=%d stdout=%d stderr=%d",
                _run_powershell._call_count,
                result.returncode,
                len(result.stdout or ""),
                len(result.stderr or ""),
            )
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    _ps._run_powershell = _run_powershell

    logger.info("keyring-wincred patched: target=%s, encoding=utf-8/replace",
                 _make_target("credstore", "TEST"))
except ImportError:
    logger.info("keyring-wincred NOT AVAILABLE — WSL credential bridge disabled")
