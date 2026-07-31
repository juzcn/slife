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

    # Non-Windows: try the current keyring backend (SecretService / macOS
    # Keychain).  If no system keyring is available (headless Linux, WSL,
    # no D-Bus session), return empty silently — credstore falls back to
    # the cryptfile backup for enumeration.
    try:
        import keyring
        kr = keyring.get_keyring()
    except Exception:
        return []

    # FailKeyring means no viable backend — headless system.
    from keyring.backends.fail import Keyring as FailKeyring
    if isinstance(kr, FailKeyring):
        return []

    return _enumerate_keyring(kr, service, with_values=with_values)


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


def _enumerate_keyring(
    kr, service: str, with_values: bool = False
) -> list[tuple[str, str]]:
    """Enumerate credentials from a ``keyring`` backend on non-Windows.

    Most keyring backends only expose point-lookup APIs
    (``get_password``), not batch enumeration.  This function tries
    backend-specific enumeration where available and returns an empty
    list otherwise — credstore falls back to the cryptfile.
    """
    # ── SecretService (Linux desktop) ──────────────────────────────
    # Use the backend's D-Bus collection to enumerate items.
    try:
        from keyring.backends.SecretService import Keyring as SecretServiceKR
        if isinstance(kr, SecretServiceKR):
            collection = kr.get_preferred_collection()
            items = collection.get_all_items() if collection else []
            entries: list[tuple[str, str]] = []
            for item in items:
                label = item.get_label()
                if not label or not label.startswith(service):
                    continue
                # Extract key from label: "credstore\0<key>" or "service\0<key>"
                parts = label.split("\0", 1)
                key = parts[1] if len(parts) == 2 else label
                if with_values:
                    secret = item.get_secret()
                    value = secret.decode("utf-8") if isinstance(secret, bytes) else str(secret)
                else:
                    value = ""
                entries.append((key, value))
            return entries
    except Exception:
        pass

    # ── macOS Keychain ─────────────────────────────────────────────
    if sys.platform == "darwin":
        try:
            from keyring.backends.macOS import Keyring as MacOSKR
            if isinstance(kr, MacOSKR):
                return _enumerate_macos_keychain(service, with_values=with_values)
        except Exception:
            pass

    # ── Fallback: backend doesn't support enumeration ──────────────
    # credstore's cryptfile backup provides the full list.
    return []
