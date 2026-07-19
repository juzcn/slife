"""credstore CLI — terminal commands for credential management.

All secret input uses masked_input() — each keystroke echoes ``*``,
paste works, but the actual value is never visible or logged.

Commands::

    credstore set-password        Set/change cryptfile master password
    credstore status              Show backend status
    credstore set <key>           Store a credential (keyring + cryptfile)
    credstore get <key>           Retrieve (keyring; cryptfile fallback on miss)
    credstore delete <key>        Delete a credential
    credstore list                List all stored credential keys
    credstore reset-keyring       Restore keyring from cryptfile backup
    credstore reset-backup        Sync system keyring → cryptfile backup
"""

from __future__ import annotations

import argparse
import sys

from credstore._tty import masked_input


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code."""
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="credstore",
        description="Secure credential storage via OS keyring.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # set-password
    sub.add_parser(
        "set-password",
        help="Set or change the cryptfile master password (interactive)",
    )

    # status
    sub.add_parser("status", help="Show backend status")

    # set <key>
    set_p = sub.add_parser("set", help="Store a credential (keyring + encrypted backup)")
    set_p.add_argument("key", help="Credential key, e.g. 'slife/provider/deepseek'")

    # get <key>
    get_p = sub.add_parser("get", help="Retrieve a credential (keyring; cryptfile fallback on miss)")
    get_p.add_argument("key", help="Credential key to retrieve")
    get_p.add_argument(
        "--password", "-p",
        action="store_true",
        help="Dual-query keyring + cryptfile, output plaintext; fail on mismatch",
    )

    # delete <key>
    del_p = sub.add_parser("delete", help="Delete a credential")
    del_p.add_argument("key", help="Credential key to delete")

    # list
    sub.add_parser("list", help="List all stored credential keys")

    # reset-keyring
    sub.add_parser(
        "reset-keyring",
        help="Restore all credentials from cryptfile backup to system keyring",
    )

    # reset-backup
    sub.add_parser(
        "reset-backup",
        help="Sync credentials from system keyring to cryptfile backup",
    )

    args = parser.parse_args(argv)

    # ── Gate: 'set' / 'reset-backup' require cryptfile to exist ──
    # get/delete work with system keyring (no master key).
    # get has an optional cryptfile fallback (asks for master key).
    # reset-keyring / reset-backup ask for the master key interactively.
    if args.command in ("set", "reset-backup"):
        from credstore._backend import has_master_key
        from credstore._store import init_store
        init_store()
        if not has_master_key():
            print("Error: master key not set.", file=sys.stderr)
            print("Run 'credstore set-password' first.", file=sys.stderr)
            return 1

    # Dispatch
    try:
        if args.command == "set-password":
            return _cmd_set_password()
        elif args.command == "status":
            return _cmd_status()
        elif args.command == "set":
            return _cmd_set(args.key)
        elif args.command == "get":
            return _cmd_get(args.key, password_mode=args.password)
        elif args.command == "delete":
            return _cmd_delete(args.key)
        elif args.command == "list":
            return _cmd_list()
        elif args.command == "reset-keyring":
            return _cmd_reset_keyring()
        elif args.command == "reset-backup":
            return _cmd_reset_backup()
        else:
            parser.print_help()
            return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


# ── Command implementations ──────────────────────────────────────


def _cmd_set_password() -> int:
    """Interactively set or change the cryptfile master password.

    Two cases:
      - First time (no existing cryptfile): create, sync from system keyring.
      - Change (existing cryptfile): read all data with old password,
        re-encrypt everything with new password.
    """
    from credstore._backend import (
        get_cryptfile, reinit_cryptfile,
    )
    from credstore._config import get_cryptfile_path
    from credstore._store import DEFAULT_SERVICE, _read_cryptfile_keys  # noqa: PLC2701

    if not sys.stdin.isatty():
        print("Error: 'credstore set-password' requires an interactive terminal.", file=sys.stderr)
        return 1

    print("Set master password for encrypted credential backup.")
    print()

    # ── Detect: first time or change? ──
    is_change = False
    old_data: dict[str, str] = {}  # key → secret
    crypt_path = get_cryptfile_path()

    if __import__("os").path.exists(crypt_path):
        is_change = True

    if is_change:
        # ── Password CHANGE: read all old data first ──
        from credstore._backend import init_backend
        init_backend()
        cf = get_cryptfile()
        if cf is None:
            print("Error: cryptfile backend not available.", file=sys.stderr)
            return 1
        print("Changing existing master password.")
        old_pw = masked_input("Current master password: ")

        # Temporarily unlock with old password to read data
        try:
            cf.keyring_key = old_pw
            keys = _read_cryptfile_keys(cf)
            for key in keys:
                try:
                    value = cf.get_password(DEFAULT_SERVICE, key)
                    if value is not None:
                        old_data[key] = value
                except Exception:
                    pass
        except Exception:
            print("Error: incorrect password or corrupted file.", file=sys.stderr)
            return 1
        print(f"  Read {len(old_data)} credential(s) from existing backup.")
        print()

    # ── Set new password ──
    pw1 = masked_input("New master password: ")
    if len(pw1) < 8:
        print("Error: password must be at least 8 characters.")
        return 1

    pw2 = masked_input("Confirm password: ")
    if pw1 != pw2:
        print("Error: passwords do not match.")
        return 1

    # Re-init with new password (creates fresh encrypted file)
    reinit_cryptfile(pw1)

    from credstore._backend import has_master_key
    if not has_master_key():
        print("Error: could not initialize cryptfile backend.", file=sys.stderr)
        return 1

    # ── Write data back ──
    cf_new = get_cryptfile()
    synced = 0

    if is_change:
        # Re-encrypt old data with new password
        for key, value in old_data.items():
            try:
                cf_new.set_password(DEFAULT_SERVICE, key, value)
                synced += 1
            except Exception:
                pass
        print(f"Master password changed. {synced} credential(s) re-encrypted.")
    else:
        # First time: cryptfile created
        print("Master password set.")

    return 0


def _cmd_status() -> int:
    """Show backend diagnostic info."""
    from credstore._store import check_backend

    info = check_backend()
    print(f"Backend: {info.get('backend', 'unknown')}")
    print(f"Available: {info.get('available', False)}")

    if info.get("cryptfile_ready"):
        print("Cryptfile: ready (dual-write active)")
        if "cryptfile_path" in info:
            print(f"  File: {info['cryptfile_path']}")
        print()
        print("Dual-write: system keyring + cryptfile encrypted backup.")
        print("Safe from OS password changes — data auto-restores from cryptfile.")
    elif info.get("cryptfile_locked") is not None:
        print("Cryptfile: LOCKED (master password not set)")
        if "cryptfile_path" in info:
            print(f"  File: {info['cryptfile_path']}")
        print()
        print("Secrets are stored in system keyring only.")
        print("WARNING: Changing your OS login password may erase all secrets.")
        print("Run 'credstore set-password' to enable cryptfile backup.")
    else:
        print("Cryptfile: not installed")
        print()
        print("Secrets are stored in system keyring only.")
        print("Install keyrings.cryptfile for encrypted backup: pip install keyrings.cryptfile")
    return 0


def _cmd_set(key: str) -> int:
    """Store a credential: cryptfile + system keyring (atomic dual-write).

    Writes to cryptfile FIRST, then system keyring.  If keyring fails,
    rolls back the cryptfile entry so both stores stay consistent.
    """
    import credstore
    from credstore._backend import get_cryptfile
    from credstore._store import DEFAULT_SERVICE

    if not sys.stdin.isatty():
        print("Error: 'credstore set' requires an interactive terminal.", file=sys.stderr)
        return 1

    cf = get_cryptfile()
    if cf is None:
        print("Error: cryptfile backend not available.", file=sys.stderr)
        print("Install: pip install keyrings.cryptfile", file=sys.stderr)
        return 1

    print(f"Enter secret for '{key}' (paste then press Enter):")
    try:
        secret = masked_input("")
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130

    if not secret.strip():
        print("Error: secret cannot be empty.")
        return 1

    master_pw = masked_input("Master password (for encrypted backup): ")

    # 1. Write cryptfile first (backup)
    try:
        cf.keyring_key = master_pw
        cf.set_password(DEFAULT_SERVICE, key, secret)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        del cf.keyring_key

    # 2. Write system keyring (primary)
    try:
        credstore.set_credential(key, secret)
    except Exception as exc:
        # Rollback: remove from cryptfile
        try:
            cf.keyring_key = master_pw
            cf.delete_password(DEFAULT_SERVICE, key)
        except Exception:
            pass
        finally:
            del cf.keyring_key
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Stored: {key}")
    return 0


def _cmd_get(key: str, password_mode: bool = False) -> int:
    """Retrieve a credential.

    --password mode: dual-query keyring + cryptfile, output plaintext, fail on mismatch.
    Default mode:     keyring only, output masked.
    """
    import credstore

    if not sys.stdin.isatty():
        print("Error: 'credstore get' requires an interactive terminal.", file=sys.stderr)
        return 1

    if not password_mode:
        # ── default: keyring only, masked output ──
        value = credstore.get_credential(key)
        if value is None:
            print(f"Not found in system keyring: {key}", file=sys.stderr)
            return 1
        from credstore._store import CredentialStore
        print(f"{key}: {CredentialStore.mask(value)}")
        return 0

    # ── --password mode: dual-query, plaintext, consistency check ──
    from credstore._store import CredentialStore, DEFAULT_SERVICE

    master_pw = masked_input("Master password: ")
    if not master_pw.strip():
        print("Error: master password is required in --password mode.", file=sys.stderr)
        return 1

    # 1. Query keyring
    value_kr = credstore.get_credential(key)

    # 2. Query cryptfile
    value_cf = None
    cf_error: str | None = None
    try:
        value_cf = _read_cryptfile(key, master_pw)
    except Exception as exc:
        cf_error = str(exc)

    # 3. Evaluate results
    if value_kr is None and value_cf is None:
        print(f"Not found in either store: {key}", file=sys.stderr)
        return 1

    if value_kr is None and value_cf is not None:
        print(f"Error: {key} — found in cryptfile but missing from system keyring.", file=sys.stderr)
        print("Run 'credstore reset-keyring' to restore all credentials from backup.", file=sys.stderr)
        return 1

    if value_kr is not None and value_cf is None:
        if cf_error:
            print(f"Error: {key} — cryptfile read failed: {cf_error}", file=sys.stderr)
        else:
            print(f"Error: {key} — found in system keyring but missing from cryptfile backup.", file=sys.stderr)
        print("Run 'credstore reset-backup' to sync keyring → cryptfile.", file=sys.stderr)
        return 1

    # Both found — consistency check
    if value_kr != value_cf:
        print(f"Error: {key} — value mismatch between system keyring and cryptfile.", file=sys.stderr)
        print("The two stores have diverged. Determine the correct value, then:", file=sys.stderr)
        print("  credstore reset-backup   if keyring is authoritative", file=sys.stderr)
        print("  credstore reset-keyring  if cryptfile is authoritative", file=sys.stderr)
        return 1

    # Match — output plaintext
    print(value_kr)
    return 0


# ── cryptfile helpers (CLI layer — master key is only typed here) ───


def _delete_from_cryptfile(key: str) -> bool:
    """Remove a single credential from the cryptfile.

    Prompts for the master password.  Returns True if the credential
    was found and deleted from the cryptfile.
    """
    from credstore._backend import get_cryptfile
    from credstore._store import DEFAULT_SERVICE

    cf = get_cryptfile()
    if cf is None:
        return False

    if not sys.stdin.isatty():
        print(
            "(non-interactive — cryptfile cleanup skipped,"
            " re-run 'credstore delete' in a terminal to clean up)",
            file=sys.stderr,
        )
        return False

    master_pw = masked_input("Master password (to remove from encrypted backup): ")

    try:
        cf.keyring_key = master_pw
        cf.delete_password(DEFAULT_SERVICE, key)
        return True
    except ValueError as exc:
        print(f"Warning: {exc}", file=sys.stderr)
        print("Cryptfile cleanup skipped (incorrect master password).", file=sys.stderr)
        return False
    except Exception:
        # Key not found in cryptfile
        return False
    finally:
        del cf.keyring_key


def _read_cryptfile(key: str, master_password: str) -> str | None:
    """Read a single credential from the cryptfile.

    Returns the secret value, or None if not found.
    Raises ValueError if the master password is wrong.
    """
    from credstore._backend import get_cryptfile
    from credstore._store import DEFAULT_SERVICE

    cf = get_cryptfile()
    if cf is None:
        return None

    try:
        cf.keyring_key = master_password
        return cf.get_password(DEFAULT_SERVICE, key)
    finally:
        del cf.keyring_key


def _cmd_delete(key: str) -> int:
    """Delete a credential from system keyring + cryptfile."""
    import credstore

    if not sys.stdin.isatty():
        print("Error: 'credstore delete' requires an interactive terminal.", file=sys.stderr)
        return 1

    deleted_sk = credstore.delete_credential(key)
    deleted_cf = _delete_from_cryptfile(key)

    if deleted_sk or deleted_cf:
        print(f"Deleted: {key}")
        return 0
    else:
        print(f"Not found: {key}")
        return 1


def _cmd_list() -> int:
    """List credentials from both system keyring and cryptfile backup.

    Dual-read, dual-display: shows which credentials exist in each store
    so the user can tell at a glance where their secrets live.
    """
    from credstore._backend import get_cryptfile, init_backend, has_master_key
    from credstore._config import get_cryptfile_path
    from credstore._store import DEFAULT_SERVICE, CredentialStore, _read_cryptfile_keys  # noqa: PLC2701

    if not sys.stdin.isatty():
        print("Error: 'credstore list' requires an interactive terminal.", file=sys.stderr)
        return 1

    # ── 1. System keyring (no password needed) ──────────────────
    keyring_entries = _enumerate_system_keyring(DEFAULT_SERVICE)
    keyring_keys: dict[str, str] = {}  # key → masked value
    for k, v in keyring_entries:
        keyring_keys[k] = CredentialStore.mask(v) if v else "(empty)"

    # ── 2. Cryptfile (requires master password if it exists) ────
    cryptfile_keys: set[str] = set()
    cryptfile_path = get_cryptfile_path()
    cryptfile_exists = __import__("os").path.exists(cryptfile_path)

    if cryptfile_exists and has_master_key():
        master_pw = masked_input("Master password: ")
        if not master_pw.strip():
            print("Error: master password is required.", file=sys.stderr)
            return 1

        try:
            init_backend(password=master_pw)
            cf = get_cryptfile()
            if cf is not None:
                cf.keyring_key = master_pw
                try:
                    cryptfile_keys = set(_read_cryptfile_keys(cf))
                finally:
                    del cf.keyring_key
        except Exception as exc:
            print(f"Error: cannot read cryptfile — {exc}", file=sys.stderr)
            return 1

    # ── 3. Merge & display ─────────────────────────────────────
    all_keys = sorted(set(keyring_keys.keys()) | cryptfile_keys)

    if not all_keys:
        print("No credentials stored.")
        print()
        if not keyring_keys and not cryptfile_exists:
            print("Run 'credstore set <KEY>' to store a credential.")
        elif not keyring_keys and not cryptfile_keys:
            print("Cryptfile exists but is empty.  Credentials in the")
            print("system keyring cannot be enumerated on this platform.")
            print("Run 'credstore set <KEY>' to populate both stores.")
        return 0

    # Column widths
    key_width = max(len(k) for k in all_keys) + 2

    # Header
    print()
    print(f"  {'KEY':<{key_width}} SYSTEM KEYRING   CRYPTFILE")
    print(f"  {'─' * (key_width - 2):─<{key_width}} ──────────────   ─────────")

    ring_only = 0
    crypt_only = 0
    both = 0

    for key in all_keys:
        in_ring = key in keyring_keys
        in_crypt = key in cryptfile_keys

        if in_ring and in_crypt:
            both += 1
            ring_mark = "✔"
            crypt_mark = "✔"
        elif in_ring:
            ring_only += 1
            ring_mark = "✔"
            crypt_mark = "—"
        else:
            crypt_only += 1
            ring_mark = "—"
            crypt_mark = "✔"

        print(f"  {key:<{key_width}} {ring_mark:<13}   {crypt_mark}")

    print(f"  {'─' * (key_width - 2):─<{key_width}} ──────────────   ─────────")
    if ring_only == 0 and crypt_only == 0:
        print(f"  {len(all_keys)} credential(s) — all synced")
    else:
        parts = []
        if ring_only:
            parts.append(f"system only: {ring_only}")
        if crypt_only:
            parts.append(f"cryptfile only: {crypt_only}")
        if both:
            parts.append(f"both: {both}")
        print(f"  {len(all_keys)} credential(s) — {', '.join(parts)}")

    # Hint if mismatch
    if ring_only > 0:
        if cryptfile_exists:
            print()
            print(f"  Tip: run 'credstore reset-backup' to sync {ring_only} missing")
            print(f"  credential(s) from system keyring into the cryptfile.")
        else:
            print()
            print(f"  Tip: run 'credstore set-password' to enable encrypted backup,")
            print(f"  then 'credstore reset-backup' to sync {ring_only} credential(s).")
    elif crypt_only > 0:
        print()
        print(f"  Tip: run 'credstore reset-keyring' to restore {crypt_only}")
        print(f"  credential(s) from cryptfile back to the system keyring.")

    print()
    return 0


def _cmd_reset_keyring() -> int:
    """Restore all credentials from cryptfile to system keyring.

    Requires the master password to decrypt the cryptfile.
    Reads every credential and re-writes to the system keyring.
    """
    if not sys.stdin.isatty():
        print("Error: 'credstore reset-keyring' requires an interactive terminal.", file=sys.stderr)
        return 1

    print("Restore credentials from encrypted backup to system keyring.")
    print()

    master_pw = masked_input("Master password: ")

    from credstore._store import reset_credentials

    try:
        count = reset_credentials(master_pw)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Restored {count} credential(s) to system keyring.")
    return 0


def _cmd_reset_backup() -> int:
    """Reset cryptfile backup: sync all credentials from system keyring.

    Enumerates credentials from the system keyring (platform-specific)
    and writes each one to the cryptfile.  Useful for one-time migration
    of existing credentials that were stored before cryptfile dual-write
    was enabled.
    """
    from credstore._backend import get_cryptfile
    from credstore._store import DEFAULT_SERVICE

    if not sys.stdin.isatty():
        print("Error: 'credstore reset-backup' requires an interactive terminal.", file=sys.stderr)
        return 1

    print("Reset cryptfile backup from system keyring.")
    print()

    # 1. Enumerate
    entries = _enumerate_system_keyring(DEFAULT_SERVICE)
    if not entries:
        print("No credentials found in system keyring.")
        return 0

    print(f"Found {len(entries)} credential(s) in system keyring:")

    # 2. Master password
    print()
    master_pw = masked_input("Master password: ")

    # 3. Write each to cryptfile
    cf = get_cryptfile()
    if cf is None:
        print("Error: cryptfile backend not available.", file=sys.stderr)
        return 1

    synced = 0
    try:
        cf.keyring_key = master_pw
        for key, value in entries:
            try:
                cf.set_password(DEFAULT_SERVICE, key, value)
                print(f"  {key}")
                synced += 1
            except Exception as exc:
                print(f"  {key} — skipped: {exc}", file=sys.stderr)
    except ValueError:
        print("Error: incorrect master password.", file=sys.stderr)
        return 1
    finally:
        del cf.keyring_key

    print()
    print(f"Reset {synced} credential(s) in cryptfile backup.")
    return 0


def _enumerate_system_keyring(service: str) -> list[tuple[str, str]]:
    """Enumerate all credentials for *service* from the system keyring.

    Uses platform-specific APIs.  On Windows, reads from Credential
    Manager via ``win32cred.CredEnumerate``.  Returns a list of
    (key, value) tuples.
    """
    import os as _os

    if _os.name == "nt":
        return _enumerate_windows(service)

    # Other platforms: keyring backends don't support enumeration.
    print(
        "Credential enumeration is not supported on this platform.\n"
        "Re-run 'credstore set <KEY>' for each credential to populate\n"
        "the cryptfile backup.",
        file=_os.sys.stderr,
    )
    return []


def _enumerate_windows(service: str) -> list[tuple[str, str]]:
    """Enumerate credstore credentials from Windows Credential Manager."""
    try:
        from win32ctypes.pywin32 import win32cred
    except ImportError:
        try:
            import win32cred
        except ImportError:
            print(
                "win32cred not available — install pywin32 or pywin32-ctypes.",
                file=__import__("sys").stderr,
            )
            return []

    try:
        all_creds = win32cred.CredEnumerate(None, 0)
    except Exception as exc:
        print(f"Cannot enumerate credentials: {exc}", file=__import__("sys").stderr)
        return []

    if all_creds is None:
        return []

    entries: list[tuple[str, str]] = []
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

    return entries


if __name__ == "__main__":
    sys.exit(main())
