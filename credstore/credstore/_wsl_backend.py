"""WSL keyring backend — bridges to Windows Credential Manager.

On WSL, no Linux desktop keyring (SecretService / GNOME Keyring /
kernel keyutils) is available.  This backend calls ``powershell.exe``
with inline C# P/Invoke to access the native Windows credential store
via ``advapi32.dll``.

A proper ``KeyringBackend`` subclass, registered via the standard
``keyring.backends`` entry point.  Priority 9.5 beats
``keyring_wincred.WinCredKeyring`` (9.0) when both are installed,
ensuring our target-format and encoding fixes are active.
"""

from __future__ import annotations

import base64
import logging
import platform
import subprocess
from typing import Optional

from jaraco.classes import properties
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError, PasswordSetError

from credstore._platform import is_wsl

logger = logging.getLogger("credstore.wsl_backend")

# ── Embedded PowerShell + C# bridge ─────────────────────────────────
# Inline C# calls advapi32.dll CredReadW / CredWriteW / CredDeleteW.
# Uses -EncodedCommand (base64 UTF-16LE) to avoid quoting issues.


_CRED_MANAGER_CS = r'''
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public class CredManager {
    public const int CRED_TYPE_GENERIC = 1;
    public const int CRED_PERSIST_LOCAL_MACHINE = 2;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct CREDENTIAL {
        public int Flags;
        public int Type;
        public string TargetName;
        public string Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public int CredentialBlobSize;
        public IntPtr CredentialBlob;
        public int Persist;
        public int AttributeCount;
        public IntPtr Attributes;
        public string TargetAlias;
        public string UserName;
    }

    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern bool CredRead(string target, int type, int reservedFlag, out IntPtr credentialPtr);

    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern bool CredWrite([In] ref CREDENTIAL userCredential, [In] uint flags);

    [DllImport("advapi32.dll", SetLastError = true)]
    public static extern bool CredDelete(string target, int type, int flags);

    [DllImport("advapi32.dll", SetLastError = true)]
    public static extern void CredFree([In] IntPtr cred);

    public static string GetCredential(string target) {
        IntPtr credPtr;
        if (!CredRead(target, CRED_TYPE_GENERIC, 0, out credPtr)) {
            return null;
        }
        try {
            CREDENTIAL cred = (CREDENTIAL)Marshal.PtrToStructure(credPtr, typeof(CREDENTIAL));
            if (cred.CredentialBlobSize > 0) {
                byte[] passwordBytes = new byte[cred.CredentialBlobSize];
                Marshal.Copy(cred.CredentialBlob, passwordBytes, 0, cred.CredentialBlobSize);
                return Convert.ToBase64String(passwordBytes);
            }
            return "";
        } finally {
            CredFree(credPtr);
        }
    }

    public static bool SetCredential(string target, string username, string base64Password) {
        byte[] passwordBytes = Convert.FromBase64String(base64Password);

        CREDENTIAL cred = new CREDENTIAL();
        cred.Type = CRED_TYPE_GENERIC;
        cred.TargetName = target;
        cred.UserName = username;
        cred.CredentialBlobSize = passwordBytes.Length;
        cred.CredentialBlob = Marshal.AllocHGlobal(passwordBytes.Length);
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE;

        try {
            Marshal.Copy(passwordBytes, 0, cred.CredentialBlob, passwordBytes.Length);
            return CredWrite(ref cred, 0);
        } finally {
            Marshal.FreeHGlobal(cred.CredentialBlob);
        }
    }

    public static bool DeleteCredential(string target) {
        return CredDelete(target, CRED_TYPE_GENERIC, 0);
    }
}
"@
'''


# ── PowerShell bridge helpers ───────────────────────────────────────


def _run_powershell(script: str) -> tuple[int, str, str]:
    """Execute a PowerShell snippet, returning (rc, stdout, stderr).

    Uses ``encoding='utf-8', errors='replace'`` because PowerShell
    outputs in the Windows OEM code page but the WSL Linux locale
    is UTF-8 — ``text=True`` (which uses the locale encoding) would
    crash on non-ASCII bytes.
    """
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


def _get_credential(target: str) -> Optional[str]:
    """Read a credential blob from Windows Credential Manager.

    Returns the password string, or ``None`` if not found.
    """
    escaped_target = target.replace("'", "''")
    script = f'''
$result = [CredManager]::GetCredential('{escaped_target}')
if ($result -eq $null) {{
    exit 1
}}
Write-Output $result
'''
    returncode, stdout, _ = _run_powershell(script)

    if returncode != 0 or not stdout:
        return None

    try:
        password_bytes = base64.b64decode(stdout)
        return password_bytes.decode("utf-16-le")
    except Exception:
        logger.debug("Failed to decode credential blob for %s", target)
        return None


def _set_credential(target: str, username: str, password: str) -> bool:
    """Store a credential into Windows Credential Manager.

    Returns ``True`` on success.
    """
    password_bytes = password.encode("utf-16-le")
    b64_password = base64.b64encode(password_bytes).decode("ascii")

    escaped_target = target.replace("'", "''")
    escaped_username = username.replace("'", "''")

    script = f'''
$result = [CredManager]::SetCredential('{escaped_target}', '{escaped_username}', '{b64_password}')
if (-not $result) {{
    exit 1
}}
'''
    returncode, _, _ = _run_powershell(script)
    return returncode == 0


def _delete_credential(target: str) -> bool:
    """Delete a credential from Windows Credential Manager.

    Returns ``True`` on success.
    """
    escaped_target = target.replace("'", "''")
    script = f'''
$result = [CredManager]::DeleteCredential('{escaped_target}')
if (-not $result) {{
    exit 1
}}
'''
    returncode, _, _ = _run_powershell(script)
    return returncode == 0


# ── Keyring backend ─────────────────────────────────────────────────


class WslBackend(KeyringBackend):
    """WSL system-keyring backend backed by Windows Credential Manager.

    Called ``WslBackend`` (not ``WslCredBackend``) so that keyring's
    ``by_priority`` sorting works correctly — it accesses ``priority``
    as a class attribute, which ``@classproperty`` provides.
    """

    @properties.classproperty
    def priority(cls) -> float:
        """Return 9.5 on WSL; raise RuntimeError otherwise.

        9.5 beats ``keyring_wincred.WinCredKeyring`` (9.0) so our
        fixes (target format + encoding) are always active when both
        packages are installed.
        """
        if platform.system() != "Linux":
            raise RuntimeError("WslBackend requires Linux")
        if not is_wsl():
            raise RuntimeError("WslBackend requires WSL")
        return 9.5

    @staticmethod
    def _target(service: str, username: str) -> str:
        """WinVault-compatible target: ``{username}@{service}``."""
        return f"{username}@{service}"

    # ── KeyringBackend interface ──────────────────────────────────

    def get_password(self, service: str, username: str) -> Optional[str]:
        # Match WinVaultKeyring._resolve_credential() fallback order:
        # 1. Try bare service name first — credentials stored by the
        #    native Windows backend (WinVaultKeyring) use the service
        #    name ("credstore") as the CredMan target.
        # 2. Fall back to compound name: {username}@{service} —
        #    credentials stored by WslBackend itself.
        result = _get_credential(service)
        if result is not None:
            return result
        return _get_credential(self._target(service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        target = self._target(service, username)
        if not _set_credential(target, username, password):
            raise PasswordSetError(
                f"Failed to store credential for {service}/{username}"
            )

    def delete_password(self, service: str, username: str) -> None:
        target = self._target(service, username)
        if not _delete_credential(target):
            raise PasswordDeleteError(
                f"Failed to delete credential for {service}/{username}"
            )
