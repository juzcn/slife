"""Monkey-patch keyring-wincred for WSL compatibility.

keyring_wincred.powershell._run_powershell calls ``subprocess.run(text=True)``
which decodes stdout using the Linux locale encoding (UTF-8).  But
``powershell.exe`` outputs bytes in the Windows OEM code page.  On WSL this
causes ``UnicodeDecodeError`` whenever PowerShell produces non-ASCII output
(e.g. Add-Type compilation messages).

This patch replaces ``text=True`` with ``encoding='utf-8', errors='replace'``
so undecodable bytes are replaced instead of crashing.

Loaded via keyring entry point — patching happens at import time,
before any backend instances are created.
"""

from __future__ import annotations

import base64
import logging
import subprocess

logger = logging.getLogger("credstore.wsl_patch")

try:
    import keyring_wincred.powershell as _ps

    _CRED_MANAGER_CS = _ps._CRED_MANAGER_CS  # reuse the embedded C# script

    def _run_powershell(script: str) -> tuple[int, str, str]:
        """Run a PowerShell script, decoding output as UTF-8 with replacement."""
        full_script = _CRED_MANAGER_CS + "\n" + script
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
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    _ps._run_powershell = _run_powershell

    logger.debug("keyring-wincred patched: powershell encoding fix")
except ImportError:
    pass  # Not on WSL / keyring-wincred not installed
