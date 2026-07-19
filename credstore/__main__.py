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
import functools
import os
import sys

from credstore import _backend as backend_mod
from credstore import _config as config_mod
from credstore import _store as store_mod
from credstore._tty import masked_input


# ── Helpers ────────────────────────────────────────────────────────────


def _err(msg: str) -> None:
    """Print an error message to stderr."""
    print(f"Error: {msg}", file=sys.stderr)


def requires_tty(func):
    """Decorator: require an interactive terminal for CLI commands."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not sys.stdin.isatty():
            cmd = func.__name__.replace("_cmd_", "")
            _err(f"'credstore {cmd}' requires an interactive terminal.")
            return 1
        return func(*args, **kwargs)
    return wrapper


# ── Command dispatch table ─────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for credstore CLI."""
    parser = argparse.ArgumentParser(
        prog="credstore",
        description="Secure credential storage via OS keyring.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("set-password", help="Set or change the cryptfile master password (interactive)")
    sub.add_parser("status", help="Show backend status")

    set_p = sub.add_parser("set", help="Store a credential (keyring + encrypted backup)")
    set_p.add_argument("key", help="Credential key, e.g. 'slife/provider/deepseek'")

    get_p = sub.add_parser("get", help="Retrieve a credential (keyring; cryptfile fallback on miss)")
    get_p.add_argument("key", help="Credential key to retrieve")
    get_p.add_argument("--password", "-p", action="store_true",
                       help="Dual-query keyring + cryptfile, output plaintext; fail on mismatch")

    del_p = sub.add_parser("delete", help="Delete a credential")
    del_p.add_argument("key", help="Credential key to delete")

    sub.add_parser("list", help="List all stored credential keys")
    sub.add_parser("reset-keyring", help="Restore all credentials from cryptfile backup to system keyring")
    sub.add_parser("reset-backup", help="Sync credentials from system keyring to cryptfile backup")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code."""
    if argv is None:
        argv = sys.argv[1:]

    parser = _build_parser()
    args = parser.parse_args(argv)

    # Gate: 'set' / 'reset-backup' require cryptfile to exist
    if args.command in ("set", "reset-backup"):
        store_mod.init_store()
        if not backend_mod.has_master_key():
            _err("master key not set.")
            print("Run 'credstore set-password' first.", file=sys.stderr)
            return 1

    # Dispatch via explicit routing (clearer than dict for argument-passing)
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
        _err(str(exc))
        return 1


# ── Command implementations ──────────────────────────────────────


@requires_tty
def _cmd_set_password() -> int:
    """Interactively set or change the cryptfile master password.

    Two cases:
      - First time (no existing cryptfile): create, sync from system keyring.
      - Change (existing cryptfile): read all data with old password,
        re-encrypt everything with new password.
    """
    print("Set master password for encrypted credential backup.")
    print()

    is_change = False
    old_data: dict[str, str] = {}  # key → secret
    crypt_path = config_mod.get_cryptfile_path()

    if os.path.exists(crypt_path):
        is_change = True

    if is_change:
        backend_mod.init_backend()
        cf = backend_mod.get_cryptfile()
        if cf is None:
            _err("cryptfile backend not available.")
            return 1
        print("Changing existing master password.")
        old_pw = masked_input("Current master password: ")

        try:
            with backend_mod.unlocked_cryptfile(old_pw) as cf:
                keys = store_mod._read_cryptfile_keys(cf)
                for key in keys:
                    try:
                        value = cf.get_password(store_mod.DEFAULT_SERVICE, key)
                    except Exception:
                        pass
                    else:
                        if value is not None:
                            old_data[key] = value
        except Exception:
            _err("incorrect password or corrupted file.")
            return 1
        print(f"  Read {len(old_data)} credential(s) from existing backup.")
        print()

    # ── Set new password ──
    pw1 = masked_input("New master password: ")
    if len(pw1) < 8:
        _err("password must be at least 8 characters.")
        return 1

    pw2 = masked_input("Confirm password: ")
    if pw1 != pw2:
        _err("passwords do not match.")
        return 1

    backend_mod.reinit_cryptfile(pw1)

    if not backend_mod.has_master_key():
        _err("could not initialize cryptfile backend.")
        return 1

    cf_new = backend_mod.get_cryptfile()
    synced = 0

    if is_change:
        for key, value in old_data.items():
            try:
                cf_new.set_password(store_mod.DEFAULT_SERVICE, key, value)
                synced += 1
            except Exception:
                pass
        print(f"Master password changed. {synced} credential(s) re-encrypted.")
    else:
        print("Master password set.")

    return 0


def _cmd_status() -> int:
    """Show backend diagnostic info."""
    info = store_mod.check_backend()
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


@requires_tty
def _cmd_set(key: str) -> int:
    """Store a credential: cryptfile + system keyring (atomic dual-write).

    Writes to cryptfile FIRST, then system keyring.  If keyring fails,
    rolls back the cryptfile entry so both stores stay consistent.
    """
    cf = backend_mod.get_cryptfile()
    if cf is None:
        _err("cryptfile backend not available.")
        print("Install: pip install keyrings.cryptfile", file=sys.stderr)
        return 1

    print(f"Enter secret for '{key}' (paste then press Enter):")
    try:
        secret = masked_input("")
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130

    if not secret.strip():
        _err("secret cannot be empty.")
        return 1

    master_pw = masked_input("Master password (for encrypted backup): ")

    # 1. Write cryptfile first (backup)
    try:
        with backend_mod.unlocked_cryptfile(master_pw) as cf:
            cf.set_password(store_mod.DEFAULT_SERVICE, key, secret)
    except ValueError as exc:
        _err(str(exc))
        return 1

    # 2. Write system keyring (primary)
    try:
        store_mod.set_credential(key, secret)
    except Exception as exc:
        # Rollback: remove from cryptfile
        try:
            with backend_mod.unlocked_cryptfile(master_pw) as cf:
                cf.delete_password(store_mod.DEFAULT_SERVICE, key)
        except Exception:
            pass
        _err(str(exc))
        return 1

    print(f"Stored: {key}")
    return 0


@requires_tty
def _cmd_get(key: str, password_mode: bool = False) -> int:
    """Retrieve a credential.

    --password mode: dual-query keyring + cryptfile, output plaintext, fail on mismatch.
    Default mode:     keyring only, output masked.
    """
    if not password_mode:
        return _cmd_get_default(key)
    return _cmd_get_password(key)


def _cmd_get_default(key: str) -> int:
    """Keyring only, masked output."""
    value = store_mod.get_credential(key)
    if value is None:
        _err(f"Not found in system keyring: {key}")
        return 1
    print(f"{key}: {store_mod.CredentialStore.mask(value)}")
    return 0


def _cmd_get_password(key: str) -> int:
    """Dual-query keyring + cryptfile, plaintext, consistency check."""
    master_pw = masked_input("Master password: ")
    if not master_pw.strip():
        _err("master password is required in --password mode.")
        return 1

    value_kr = store_mod.get_credential(key)

    value_cf = None
    cf_error: str | None = None
    try:
        value_cf = _read_cryptfile(key, master_pw)
    except Exception as exc:
        cf_error = str(exc)

    # Evaluate results with exhaustive if/elif/else
    if value_kr is None and value_cf is None:
        _err(f"Not found in either store: {key}")
        return 1
    elif value_kr is None:
        _err(f"{key} — found in cryptfile but missing from system keyring.")
        print("Run 'credstore reset-keyring' to restore all credentials from backup.", file=sys.stderr)
        return 1
    elif value_cf is None:
        if cf_error:
            _err(f"{key} — cryptfile read failed: {cf_error}")
        else:
            _err(f"{key} — found in system keyring but missing from cryptfile backup.")
        print("Run 'credstore reset-backup' to sync keyring → cryptfile.", file=sys.stderr)
        return 1
    elif value_kr != value_cf:
        _err(f"{key} — value mismatch between system keyring and cryptfile.")
        print("The two stores have diverged. Determine the correct value, then:", file=sys.stderr)
        print("  credstore reset-backup   if keyring is authoritative", file=sys.stderr)
        print("  credstore reset-keyring  if cryptfile is authoritative", file=sys.stderr)
        return 1
    else:
        # Match — output plaintext
        print(value_kr)
        return 0


# ── cryptfile helpers (CLI layer — master key is only typed here) ───


def _delete_from_cryptfile(key: str) -> bool:
    """Remove a single credential from the cryptfile.

    Prompts for the master password.  Returns True if the credential
    was found and deleted from the cryptfile.
    """
    cf = backend_mod.get_cryptfile()
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
        with backend_mod.unlocked_cryptfile(master_pw) as cf:
            cf.delete_password(store_mod.DEFAULT_SERVICE, key)
        return True
    except ValueError as exc:
        print(f"Warning: {exc}", file=sys.stderr)
        print("Cryptfile cleanup skipped (incorrect master password).", file=sys.stderr)
        return False
    except Exception:
        return False


def _read_cryptfile(key: str, master_password: str) -> str | None:
    """Read a single credential from the cryptfile.

    Returns the secret value, or None if not found.
    Raises ValueError if the master password is wrong.
    """
    cf = backend_mod.get_cryptfile()
    if cf is None:
        return None

    with backend_mod.unlocked_cryptfile(master_password) as cf:
        return cf.get_password(store_mod.DEFAULT_SERVICE, key)


@requires_tty
def _cmd_delete(key: str) -> int:
    """Delete a credential from system keyring + cryptfile."""
    deleted_sk = store_mod.delete_credential(key)
    deleted_cf = _delete_from_cryptfile(key)

    if deleted_sk or deleted_cf:
        print(f"Deleted: {key}")
        return 0
    else:
        print(f"Not found: {key}")
        return 1


@requires_tty
def _cmd_list() -> int:
    """List credentials from both system keyring and cryptfile backup."""
    # ── 1. System keyring (no password needed) ──────────────────
    keyring_entries = _enumerate_system_keyring(store_mod.DEFAULT_SERVICE)
    keyring_values: dict[str, str] = {}
    for k, v in keyring_entries:
        keyring_values[k] = v or ""

    # ── 2. Cryptfile (requires master password to decrypt) ────
    cryptfile_values: dict[str, str] = {}
    cryptfile_path = config_mod.get_cryptfile_path()
    cryptfile_exists = os.path.exists(cryptfile_path)

    # Ensure backends are initialised before checking has_master_key()
    backend_mod.init_backend()

    if cryptfile_exists and backend_mod.has_master_key():
        master_pw = masked_input("Master password: ")
        if not master_pw.strip():
            _err("master password is required.")
            return 1

        try:
            backend_mod.init_backend(password=master_pw)
            cf = backend_mod.get_cryptfile()
            if cf is not None:
                with backend_mod.unlocked_cryptfile(master_pw) as cf:
                    for key in store_mod._read_cryptfile_keys(cf):
                        try:
                            val = cf.get_password(store_mod.DEFAULT_SERVICE, key)
                            if val:
                                cryptfile_values[key] = val
                        except Exception:
                            pass
        except Exception as exc:
            _err(f"cannot read cryptfile — {exc}")
            return 1

    # ── 3. Merge & display ─────────────────────────────────────
    all_keys = sorted(set(keyring_values.keys()) | set(cryptfile_values.keys()))

    if not all_keys:
        _print_empty_list(keyring_values, cryptfile_exists, set(cryptfile_values.keys()))
        return 0

    _print_credential_table(all_keys, keyring_values, cryptfile_values, cryptfile_exists)
    return 0


def _print_empty_list(
    keyring_values: dict[str, str],
    cryptfile_exists: bool,
    cryptfile_keys: set[str],
) -> None:
    """Print the empty-credential message with appropriate guidance."""
    print("No credentials stored.")
    print()
    if not keyring_values and not cryptfile_exists:
        print("Run 'credstore set <KEY>' to store a credential.")
    elif not keyring_values and not cryptfile_keys:
        print("Cryptfile exists but is empty.  Credentials in the")
        print("system keyring cannot be enumerated on this platform.")
        print("Run 'credstore set <KEY>' to populate both stores.")


def _print_credential_table(
    all_keys: list[str],
    keyring_values: dict[str, str],
    cryptfile_values: dict[str, str],
    cryptfile_exists: bool,
) -> None:
    """Print a formatted table of credentials with sync status and tips."""
    key_width = max(len(k) for k in all_keys) + 2

    print()
    print(f"  {'KEY':<{key_width}} SYSTEM KEYRING   CRYPTFILE        STATUS")
    print(f"  {'─' * (key_width - 2):─<{key_width}} ──────────────   ──────────────   ──────")

    ring_only = 0
    crypt_only = 0
    synced = 0
    mismatched = 0

    for key in all_keys:
        in_ring = key in keyring_values
        in_crypt = key in cryptfile_values

        if in_ring and in_crypt:
            ring_val = keyring_values[key]
            crypt_val = cryptfile_values[key]
            if ring_val == crypt_val:
                synced += 1
                ring_mark, crypt_mark, status = "✔", "✔", "synced"
            else:
                mismatched += 1
                ring_mark, crypt_mark, status = "✔", "✔", "MISMATCH ⚠"
        elif in_ring:
            ring_only += 1
            ring_mark, crypt_mark, status = "✔", "—", "keyring only"
        else:
            crypt_only += 1
            ring_mark, crypt_mark, status = "—", "✔", "cryptfile only"

        print(f"  {key:<{key_width}} {ring_mark:<13}   {crypt_mark:<14}   {status}")

    print(f"  {'─' * (key_width - 2):─<{key_width}} ──────────────   ──────────────   ──────")
    parts = []
    if synced:
        parts.append(f"synced: {synced}")
    if ring_only:
        parts.append(f"system only: {ring_only}")
    if crypt_only:
        parts.append(f"cryptfile only: {crypt_only}")
    if mismatched:
        parts.append(f"mismatched: {mismatched}")
    print(f"  {len(all_keys)} credential(s) — {', '.join(parts)}")

    _print_list_tips(ring_only, crypt_only, cryptfile_exists)
    print()


def _print_list_tips(
    ring_only: int,
    crypt_only: int,
    cryptfile_exists: bool,
) -> None:
    """Print context-sensitive sync tips."""
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


@requires_tty
def _cmd_reset_keyring() -> int:
    """Restore all credentials from cryptfile to system keyring."""
    print("Restore credentials from encrypted backup to system keyring.")
    print()

    master_pw = masked_input("Master password: ")

    try:
        count = store_mod.reset_credentials(master_pw)
    except Exception as exc:
        _err(str(exc))
        return 1

    print(f"Restored {count} credential(s) to system keyring.")
    return 0


@requires_tty
def _cmd_reset_backup() -> int:
    """Reset cryptfile backup: sync all credentials from system keyring."""
    print("Reset cryptfile backup from system keyring.")
    print()

    entries = _enumerate_system_keyring(store_mod.DEFAULT_SERVICE)
    if not entries:
        print("No credentials found in system keyring.")
        return 0

    print(f"Found {len(entries)} credential(s) in system keyring:")
    print()
    master_pw = masked_input("Master password: ")

    cf = backend_mod.get_cryptfile()
    if cf is None:
        _err("cryptfile backend not available.")
        return 1

    synced = 0
    try:
        with backend_mod.unlocked_cryptfile(master_pw) as cf:
            for key, value in entries:
                try:
                    cf.set_password(store_mod.DEFAULT_SERVICE, key, value)
                    print(f"  {key}")
                    synced += 1
                except Exception as exc:
                    print(f"  {key} — skipped: {exc}", file=sys.stderr)
    except ValueError:
        _err("incorrect master password.")
        return 1

    print()
    print(f"Reset {synced} credential(s) in cryptfile backup.")
    return 0


def _enumerate_system_keyring(service: str) -> list[tuple[str, str]]:
    """Enumerate all credentials for *service* from the system keyring.

    Uses platform-specific APIs.  On Windows, reads from Credential
    Manager via ``win32cred.CredEnumerate``.  Returns a list of
    (key, value) tuples.
    """
    if os.name == "nt":
        return _enumerate_windows(service)

    # Other platforms: keyring backends don't support enumeration.
    print(
        "Credential enumeration is not supported on this platform.\n"
        "Re-run 'credstore set <KEY>' for each credential to populate\n"
        "the cryptfile backup.",
        file=sys.stderr,
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
