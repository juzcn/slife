"""Platform-specific credential enumeration.

Reads credential keys from the OS credential store.  On Windows this
uses ``win32cred.CredEnumerate``; on other platforms enumeration is
not supported and an empty list is returned.

Memory safety: pass ``with_values=False`` (the default) to enumerate
keys only — secret values are never decoded or stored.  Only set
``with_values=True`` when you genuinely need all values (e.g. syncing
to cryptfile backup).
"""

from __future__ import annotations

import os
import sys

__all__ = ["enumerate_system_keyring"]


def enumerate_system_keyring(
    service: str, with_values: bool = False
) -> list[tuple[str, str]]:
    """Enumerate credentials for *service* from the system keyring.

    Uses platform-specific APIs.  On Windows, reads from Credential
    Manager via ``win32cred.CredEnumerate``.  Returns a list of
    (key, value) tuples when *with_values* is True, otherwise
    (key, "") tuples.

    IMPORTANT: Pass ``with_values=False`` unless you genuinely need
    the secret values.  Batch-loading all secrets into memory is a
    leak risk — prefer ``with_values=False`` for enumeration and
    retrieve individual values only on demand, ``del``-ing each
    after use.
    """
    if os.name == "nt":
        return _enumerate_windows(service, with_values=with_values)

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
