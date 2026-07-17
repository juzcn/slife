"""credstore CLI — terminal commands for credential management.

All secret input uses masked_input() — each keystroke echoes ``*``,
paste works, but the actual value is never visible or logged.

Commands::

    credstore set-password        Set/change cryptfile master password
    credstore status              Show backend status
    credstore set <key>           Store a credential (reads from stdin)
    credstore get <key>           Retrieve (masked output)
    credstore delete <key>        Delete a credential
    credstore list                List all stored credential keys
"""

from __future__ import annotations

import argparse
import sys


def masked_input(prompt: str = "") -> str:
    """Read a line from stdin, echoing ``*`` for each character.

    Supports paste and backspace.  The actual characters are never
    displayed — only ``*`` placeholders.  Works on Windows (msvcrt)
    and Unix (termios).

    Ctrl+C raises KeyboardInterrupt as usual.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()

    if sys.platform == "win32":
        return _masked_input_windows()
    else:
        return _masked_input_unix()


def _masked_input_windows() -> str:
    import msvcrt
    chars: list[str] = []
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            break
        if ch == "\x03":  # Ctrl+C
            sys.stdout.write("\n")
            sys.stdout.flush()
            raise KeyboardInterrupt()
        if ch in ("\x08", "\x7f"):  # Backspace / DEL
            if chars:
                chars.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
        elif ch == "\x1b":  # Escape sequence (arrow keys, etc.)
            # Read the rest of the escape sequence and ignore it
            while msvcrt.kbhit():
                msvcrt.getwch()
        elif ord(ch) >= 32:  # Printable characters only
            chars.append(ch)
            sys.stdout.write("*")
            sys.stdout.flush()
    return "".join(chars)


def _masked_input_unix() -> str:
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    chars: list[str] = []
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                break
            if ch == "\x03":
                sys.stdout.write("\n")
                sys.stdout.flush()
                raise KeyboardInterrupt()
            if ch in ("\x08", "\x7f"):
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
            elif ord(ch) >= 32:
                chars.append(ch)
                sys.stdout.write("*")
                sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return "".join(chars)


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
    set_p = sub.add_parser("set", help="Store a credential (reads secret from stdin)")
    set_p.add_argument("key", help="Credential key, e.g. 'slife/provider/deepseek'")

    # get <key>
    get_p = sub.add_parser("get", help="Retrieve a credential (masked output)")
    get_p.add_argument("key", help="Credential key to retrieve")

    # delete <key>
    del_p = sub.add_parser("delete", help="Delete a credential")
    del_p.add_argument("key", help="Credential key to delete")

    # list
    sub.add_parser("list", help="List all stored credential keys")

    # reset
    sub.add_parser(
        "reset",
        help="Restore all credentials from cryptfile backup to system keyring",
    )

    args = parser.parse_args(argv)

    # ── Gate: only 'set' requires master password ──
    # get/delete/list work with system keyring only (no master key).
    # reset asks for the master password interactively.
    if args.command == "set":
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
            return _cmd_get(args.key)
        elif args.command == "delete":
            return _cmd_delete(args.key)
        elif args.command == "list":
            return _cmd_list()
        elif args.command == "reset":
            return _cmd_reset()
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
        get_system_keyring, get_cryptfile, is_cryptfile_ready,
        reinit_cryptfile,
    )
    from credstore._store import DEFAULT_SERVICE

    print("Set master password for encrypted credential backup.")
    print()

    # ── Detect: first time or change? ──
    is_change = False
    old_data: dict[str, str] = {}  # key → secret

    cf = get_cryptfile()
    if cf is not None and hasattr(cf, "file_path"):
        crypt_path = getattr(cf, "file_path", None)
        if crypt_path and __import__("os").path.exists(crypt_path):
            is_change = True

    if is_change:
        # ── Password CHANGE: read all old data first ──
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
    sk = get_system_keyring()
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
        # First time: sync from system keyring
        synced = _sync_from_system_keyring()
        print(f"Master password set. {synced} credential(s) synced from system keyring.")

    return 0


def _read_cryptfile_keys(cf) -> list[str]:
    """Read all credential keys from a cryptfile backend."""
    import configparser
    from credstore._store import DEFAULT_SERVICE

    cfg = configparser.ConfigParser()
    cfg.read(cf.file_path)
    keys = []
    for section in cfg.sections():
        if section.startswith("keyring") or section.startswith("DEFAULT"):
            continue
        if section == DEFAULT_SERVICE:
            keys.extend(cfg.options(section))
    return keys


def _sync_from_system_keyring() -> int:
    """Sync credentials from system keyring to cryptfile.

    Since system keyrings don't support enumeration, this re-reads
    keys from the cryptfile and fetches their values from system keyring.
    For first-time setup, the cryptfile is empty so count is 0.
    Returns the count of synced credentials.
    """
    from credstore._backend import get_system_keyring, get_cryptfile, is_cryptfile_ready
    from credstore._store import DEFAULT_SERVICE

    if not is_cryptfile_ready():
        return 0

    sk = get_system_keyring()
    cf = get_cryptfile()
    if sk is None or cf is None:
        return 0

    # All known keys come from the store's perspective
    from credstore._store import _get_store
    keys = _get_store().list_keys()

    count = 0
    for key in keys:
        try:
            value = sk.get_password(DEFAULT_SERVICE, key)
            if value is not None:
                cf.set_password(DEFAULT_SERVICE, key, value)
                count += 1
        except Exception:
            pass

    return count


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
        print("Run 'credstore set-password' to enable cryptfile backup sync.")
    else:
        print("Cryptfile: not installed")
        print()
        print("Secrets are stored in system keyring only.")
        print("Install keyrings.cryptfile for encrypted backup: pip install keyrings.cryptfile")
    return 0


def _cmd_set(key: str) -> int:
    """Store a credential, reading the secret from stdin."""
    from credstore._store import set_credential

    print(f"Enter secret for '{key}' (paste then press Enter):")
    try:
        secret = masked_input("")
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130

    if not secret.strip():
        print("Error: secret cannot be empty.")
        return 1

    set_credential(key, secret)
    print(f"Stored: {key}")
    return 0


def _cmd_get(key: str) -> int:
    """Retrieve a credential, with masked output."""
    import credstore

    value = credstore.get_credential(key)
    if value is None:
        print(f"Not found: {key}")
        return 1

    from credstore._store import CredentialStore
    masked = CredentialStore.mask(value)
    print(f"{key}: {masked}")
    return 0


def _cmd_delete(key: str) -> int:
    """Delete a credential."""
    import credstore

    existed = credstore.delete_credential(key)
    if existed:
        print(f"Deleted: {key}")
    else:
        print(f"Not found: {key}")
    return 0 if existed else 1


def _cmd_reset() -> int:
    """Restore all credentials from cryptfile to system keyring.

    Requires the master password to decrypt the cryptfile.
    Reads every credential and re-writes to the system keyring.
    """
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


def _cmd_list() -> int:
    """List all stored credential keys."""
    import credstore

    keys = credstore.list_credentials()
    if not keys:
        print("No credentials stored.")
    else:
        for k in sorted(keys):
            print(k)
    return 0


if __name__ == "__main__":
    sys.exit(main())
