"""Platform-specific credential enumeration.

Reads credential keys from the OS credential store.  On Windows this
uses ``win32cred.CredEnumerate``; on WSL it uses ``powershell.exe``
with inline C# P/Invoke to ``advapi32.dll CredEnumerateW``.  Other
platforms return an empty list.

Memory safety: pass ``with_values=False`` (the default) to enumerate
keys only — secret values are never decoded or stored.  Only set
``with_values=True`` when you genuinely need all values (e.g. syncing
to cryptfile backup).
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys

__all__ = ["enumerate_system_keyring"]


# ── WSL detection ───────────────────────────────────────────────────

def _is_wsl() -> bool:
    if os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop"):
        return True
    try:
        with open("/proc/version", "r", encoding="ascii", errors="replace") as f:
            content = f.read().lower()
            return "microsoft" in content or "wsl" in content
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return False


# ── PowerShell-based enumeration for WSL ────────────────────────────

_CRED_ENUM_SCRIPT = r'''
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public class CredEnum {
    public const int CRED_TYPE_GENERIC = 1;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct CREDENTIAL {
        public int Flags;
        public int Type;
        public IntPtr TargetName;
        public IntPtr Comment;
        public long LastWritten;
        public int CredentialBlobSize;
        public IntPtr CredentialBlob;
        public int Persist;
        public int AttributeCount;
        public IntPtr Attributes;
        public IntPtr TargetAlias;
        public IntPtr UserName;
    }

    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern bool CredEnumerateW(string filter, int flags, out int count, out IntPtr credentials);

    [DllImport("advapi32.dll", SetLastError = true)]
    public static extern void CredFree(IntPtr buffer);

    public static string EnumerateCredentials(string serviceSuffix) {
        int count;
        IntPtr buf;
        if (!CredEnumerateW(null, 0, out count, out buf)) {
            return "[]";
        }
        var results = new System.Collections.Generic.List<string>();
        // CredEnumerateW returns an array of POINTERS to CREDENTIAL structs,
        // not a contiguous array of CREDENTIAL structs.
        for (int i = 0; i < count; i++) {
            IntPtr credPtr = Marshal.ReadIntPtr(buf, i * IntPtr.Size);
            CREDENTIAL cred = (CREDENTIAL)Marshal.PtrToStructure(credPtr, typeof(CREDENTIAL));
            if (cred.Type != CRED_TYPE_GENERIC) continue;
            string user = Marshal.PtrToStringUni(cred.UserName) ?? "";
            string target = Marshal.PtrToStringUni(cred.TargetName) ?? "";
            if (user.Length == 0) continue;
            if (target != serviceSuffix && !target.EndsWith("@" + serviceSuffix)) continue;
            string blobStr = "";
            if (cred.CredentialBlobSize > 0) {
                byte[] bytes = new byte[cred.CredentialBlobSize];
                Marshal.Copy(cred.CredentialBlob, bytes, 0, cred.CredentialBlobSize);
                blobStr = Convert.ToBase64String(bytes);
            }
            results.Add(user + "|" + blobStr);
        }
        CredFree(buf);
        return "[" + string.Join(",", results.ConvertAll(s => "\"" + s.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"")) + "]";
    }
}
"@

$svc = [Console]::In.ReadToEnd().Trim()
$json = [CredEnum]::EnumerateCredentials($svc)
Write-Output $json
'''


def _enumerate_wsl(
    service: str, with_values: bool = False
) -> list[tuple[str, str]]:
    """Enumerate credstore credentials via PowerShell CredEnumerate on WSL."""
    import json

    try:
        script_bytes = _CRED_ENUM_SCRIPT.encode("utf-16-le")
        encoded_script = base64.b64encode(script_bytes).decode("ascii")

        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded_script,
            ],
            input=service,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"Cannot enumerate credentials: {exc}", file=sys.stderr)
        return []

    if result.returncode != 0:
        print(
            f"WSL enum: powershell rc={result.returncode} "
            f"stderr={result.stderr[:200]!r}",
            file=sys.stderr,
        )
        return []

    raw_stdout = result.stdout.strip()
    print(
        f"WSL enum: rc=0 stdout_len={len(raw_stdout)} "
        f"stdout={raw_stdout[:300]!r}",
        file=sys.stderr,
    )

    try:
        raw_entries = json.loads(raw_stdout or "[]")
    except json.JSONDecodeError as exc:
        print(f"WSL enum: JSON parse error: {exc}", file=sys.stderr)
        return []

    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in raw_entries:
        if "|" in entry:
            username, b64_blob = entry.split("|", 1)
        else:
            username, b64_blob = entry, ""

        if username in seen:
            continue
        seen.add(username)

        if with_values and b64_blob:
            try:
                blob = base64.b64decode(b64_blob)
                value = blob.decode("utf-16-le")
            except Exception:
                continue
            entries.append((username, value))
        else:
            entries.append((username, ""))

    return entries


# ── public API ──────────────────────────────────────────────────────


def enumerate_system_keyring(
    service: str, with_values: bool = False
) -> list[tuple[str, str]]:
    """Enumerate credentials for *service* from the system keyring.

    Uses platform-specific APIs.  On Windows, reads from Credential
    Manager via ``win32cred.CredEnumerate``.  On WSL, uses
    ``powershell.exe`` with ``advapi32.dll CredEnumerateW``.
    Returns a list of (key, value) tuples when *with_values* is True,
    otherwise (key, "") tuples.

    IMPORTANT: Pass ``with_values=False`` unless you genuinely need
    the secret values.  Batch-loading all secrets into memory is a
    leak risk — prefer ``with_values=False`` for enumeration and
    retrieve individual values only on demand, ``del``-ing each
    after use.
    """
    if os.name == "nt":
        return _enumerate_windows(service, with_values=with_values)

    if _is_wsl():
        return _enumerate_wsl(service, with_values=with_values)

    # Other platforms: keyring backends don't support enumeration.
    print(
        "Credential enumeration is not supported on this platform.\n"
        "Re-run 'credstore set <KEY>' for each credential to populate\n"
        "the cryptfile backup.",
        file=sys.stderr,
    )
    return []


def _enumerate_windows(
    service: str, with_values: bool = False
) -> list[tuple[str, str]]:
    """Enumerate credstore credentials from Windows Credential Manager.

    When *with_values* is False (default), returns (key, "") tuples
    and discards decoded secrets immediately — safe for enumeration.
    Only set *with_values=True* when you genuinely need all values
    (e.g. reset-backup).
    """
    try:
        from win32ctypes.pywin32 import win32cred
    except ImportError:
        try:
            import win32cred
        except ImportError:
            print(
                "win32cred not available — install pywin32 or pywin32-ctypes.",
                file=sys.stderr,
            )
            return []

    try:
        all_creds = win32cred.CredEnumerate(None, 0)
    except Exception as exc:
        print(f"Cannot enumerate credentials: {exc}", file=sys.stderr)
        return []

    if all_creds is None:
        return []

    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for cred in all_creds:
        target = cred.get("TargetName", "")
        cred_type = cred.get("Type", 0)

        # CRED_TYPE_GENERIC = 1
        if cred_type != 1:
            continue

        # Our credentials have TargetName = service or username@service
        if target != service and not target.endswith("@" + service):
            continue

        username = cred.get("UserName", "")
        if not username:
            continue

        # Dedup: Windows Credential Manager may hold duplicate entries
        # from different keyring backends (WinVault + fallback chain).
        if username in seen:
            continue
        seen.add(username)

        if with_values:
            # Decode the credential blob (UTF-16 as written by keyring)
            blob = cred.get("CredentialBlob", b"")
            try:
                value = blob.decode("utf-16")
            except (UnicodeDecodeError, UnicodeError):
                try:
                    value = blob.decode("utf-8")
                except (UnicodeDecodeError, UnicodeError):
                    continue
            entries.append((username, value))
        else:
            # Keys only — never decode secret values for enumeration
            entries.append((username, ""))

    # Discard raw credential structures from memory
    del all_creds

    return entries
