"""credstore CLI — terminal commands for credential management.

All secret input uses masked_input() — each keystroke echoes ``*``,
paste works, but the actual value is never visible or logged.

Commands::

    credstore set-password        Set/change cryptfile master password
    credstore status              Show backend status
    credstore set <key>           Store a credential (keyring + cryptfile)
    credstore get <key>           Retrieve (keyring; cryptfile fallback on miss)
    credstore delete <key>        Delete a credential
    credstore list                List credentials (keyring + cryptfile + env)
    credstore reset-keyring       Restore keyring from cryptfile backup
    credstore reset-backup        Sync system keyring → cryptfile backup
    credstore inject KEY...       Print shell export commands (for eval)
    credstore uninject KEY...     Print shell unset commands (cleanup)

Module layout::

    _store.py        CredentialStore + module-level API
    _shell.py        Shell formatting (format_export / format_unset)
    _backend.py      Dual-write backends + unlocked_cryptfile
    _enumerate.py    Platform-specific credential enumeration
    _config.py       Config file loading + cryptfile path resolution
    _resolver.py     keyring: URI resolution
    _tty.py          Masked terminal input (cross-platform)
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

    inject_p = sub.add_parser("inject", help="Persist credentials to system environment")
    inject_p.add_argument("keys", nargs="+", metavar="KEY",
                          help="Credential keys to export as env vars")
    inject_p.add_argument("--shell", choices=["auto", "bash", "powershell", "cmd"],
                          default="auto", help="Shell format (default: auto-detect)")

    uninject_p = sub.add_parser("uninject", help="Remove credentials from system environment")
    uninject_p.add_argument("keys", nargs="+", metavar="KEY",
                            help="Environment variables to remove")
    uninject_p.add_argument("--shell", choices=["auto", "bash", "powershell", "cmd"],
                          default="auto", help="Shell format (default: auto-detect)")

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
        elif args.command == "inject":
            return _cmd_inject(args.keys, args.shell)
        elif args.command == "uninject":
            return _cmd_uninject(args.keys, args.shell)
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
            del old_pw
            del old_data
            _err("incorrect password or corrupted file.")
            return 1

        del old_pw
        print(f"  Read {len(old_data)} credential(s) from existing backup.")
        print()

    # ── Set new password ──
    pw1 = masked_input("New master password: ")
    if len(pw1) < 8:
        del pw1
        if is_change:
            del old_data
        _err("password must be at least 8 characters.")
        return 1

    pw2 = masked_input("Confirm password: ")
    if pw1 != pw2:
        del pw2
        del pw1
        if is_change:
            del old_data
        _err("passwords do not match.")
        return 1
    del pw2

    backend_mod.reinit_cryptfile(pw1)
    del pw1  # cryptfile now holds keyring_key internally

    if not backend_mod.has_master_key():
        if is_change:
            del old_data
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
        del old_data
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
        del secret
        _err("secret cannot be empty.")
        return 1

    master_pw = masked_input("Master password (for encrypted backup): ")

    # 1. Write cryptfile first (backup)
    try:
        with backend_mod.unlocked_cryptfile(master_pw) as cf:
            cf.set_password(store_mod.DEFAULT_SERVICE, key, secret)
    except ValueError as exc:
        del secret
        del master_pw
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
        del secret
        del master_pw
        _err(str(exc))
        return 1

    del secret
    del master_pw
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
    del value
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
        del master_pw
        _err(f"Not found in either store: {key}")
        return 1
    elif value_kr is None:
        del master_pw
        _err(f"{key} — found in cryptfile but missing from system keyring.")
        print("Run 'credstore reset-keyring' to restore all credentials from backup.", file=sys.stderr)
        return 1
    elif value_cf is None:
        del value_kr
        del master_pw
        if cf_error:
            _err(f"{key} — cryptfile read failed: {cf_error}")
        else:
            _err(f"{key} — found in system keyring but missing from cryptfile backup.")
        print("Run 'credstore reset-backup' to sync keyring → cryptfile.", file=sys.stderr)
        return 1
    elif value_kr != value_cf:
        del value_cf
        del value_kr
        del master_pw
        _err(f"{key} — value mismatch between system keyring and cryptfile.")
        print("The two stores have diverged. Determine the correct value, then:", file=sys.stderr)
        print("  credstore reset-backup   if keyring is authoritative", file=sys.stderr)
        print("  credstore reset-keyring  if cryptfile is authoritative", file=sys.stderr)
        return 1
    else:
        # Match — output plaintext
        del value_cf
        del master_pw
        print(value_kr)
        del value_kr
        return 0


# ── inject / uninject ──────────────────────────────────────────


def _cmd_inject(keys: list[str], shell: str) -> int:
    """Persist credentials to system environment + print export for current shell.

    For each KEY:
      1. Read the secret from the system keyring.
      2. Persist: registry (Windows) or shell profile (Unix).
      3. Print the export command for immediate eval.
      4. ``del`` the secret immediately.
    """
    from credstore._shell import format_export

    for key in keys:
        value = store_mod.get_credential(key)
        if value is None:
            _err(
                f"'{key}' is not stored in the keyring.\n"
                f"Store it first: credstore set {key}"
            )
            return 1

        _persist_key(key, value, shell)

        if sys.stdout.isatty():
            _print_inject_activation(shell, key)
        else:
            print(format_export(key, value, shell))
        del value

    return 0


def _cmd_uninject(keys: list[str], shell: str) -> int:
    """Remove from system environment + print unset for current shell."""
    from credstore._shell import format_unset

    for key in keys:
        _unpersist_key(key, shell)
        print(format_unset(key, shell))

    return 0


# ── persistence helpers ───────────────────────────────────────


def _persist_key(key: str, value: str, shell: str) -> None:
    """Persist: registry (Windows) or shell profile (Unix)."""
    if os.name == "nt":
        _setx(key, value)
        print(f"# {key} → registry (new shells auto-load)", file=sys.stderr)
    else:
        from credstore._shell import add_to_profile
        added = add_to_profile(key, shell)
        if added:
            print(f"# {_shell_get_profile_path(shell)}", file=sys.stderr)


def _unpersist_key(key: str, shell: str) -> None:
    """Remove: registry (Windows) or shell profile (Unix)."""
    if os.name == "nt":
        _setx_delete(key)
        print(f"# {key} removed from registry", file=sys.stderr)
    else:
        from credstore._shell import remove_from_profile
        removed = remove_from_profile(key, shell)
        if removed:
            print(f"# {_shell_get_profile_path(shell)}", file=sys.stderr)


def _setx(key: str, value: str) -> None:
    """Write to HKCU\\Environment directly — no command-line leak."""
    import ctypes
    import winreg

    key_handle = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
    )
    winreg.SetValueEx(key_handle, key, 0, winreg.REG_EXPAND_SZ, value)
    winreg.CloseKey(key_handle)
    _broadcast_environment_change()


def _setx_delete(key: str) -> None:
    """Delete a value from HKCU\\Environment directly."""
    import ctypes
    import winreg

    try:
        key_handle = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0,
            winreg.KEY_SET_VALUE,
        )
        winreg.DeleteValue(key_handle, key)
        winreg.CloseKey(key_handle)
        _broadcast_environment_change()
    except FileNotFoundError:
        pass


def _broadcast_environment_change() -> None:
    """Notify running processes that HKCU\\Environment changed.

    Uses ``SendMessageTimeoutW`` with ``SMTO_ABORTIFHUNG`` so a hung
    top-level window cannot stall the broadcast (and thus the ``inject``
    or ``uninject`` command).  Falls back to a fire-and-forget
    ``SendNotifyMessageW`` if the timeout API is unavailable.
    """
    import ctypes

    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    ENV = "Environment"

    user32 = ctypes.windll.user32

    # Prefer SendMessageTimeoutW — aborts on hung windows after 2 s
    try:
        result = ctypes.c_ulong()
        user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            ENV,
            SMTO_ABORTIFHUNG,
            2000,  # 2-second timeout per window
            ctypes.byref(result),
        )
    except Exception:
        # Fallback: async fire-and-forget (no hang risk, but some
        # processes may not see the change until restart)
        user32.SendNotifyMessageW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, ENV)


def _shell_get_profile_path(shell: str) -> str:
    """Return a display-friendly profile path string."""
    from credstore._shell import get_profile_path
    p = get_profile_path(shell)
    return str(p) if p else "(unknown)"


def _print_inject_activation(shell: str, key: str) -> None:
    """Tell the user how to activate the key in their current shell."""
    from credstore._shell import resolve_shell
    fmt = resolve_shell(shell)
    if os.name == "nt":
        if fmt == "powershell":
            print(
                f"# Restart PowerShell, or:"
                f" Invoke-Expression (credstore inject {key})",
                file=sys.stderr,
            )
        else:
            print(
                f"# Restart cmd, or:"
                f" FOR /F \"delims=\" %i IN ('credstore inject {key}') DO %i",
                file=sys.stderr,
            )
    elif fmt == "powershell":
        print(
            f"# Run: Invoke-Expression (credstore inject {key})",
            file=sys.stderr,
        )
    else:
        print(f"# Run: eval \"$(credstore inject {key})\"", file=sys.stderr)


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
        del master_pw
        return True
    except ValueError as exc:
        del master_pw
        print(f"Warning: {exc}", file=sys.stderr)
        print("Cryptfile cleanup skipped (incorrect master password).", file=sys.stderr)
        return False
    except Exception:
        del master_pw
        return False


def _read_cryptfile(key: str, master_password: str) -> str | None:
    """Read a single credential from the cryptfile.

    Returns the secret value, or None if not found.
    Raises ValueError if the master password is wrong.

    Caller is responsible for ``del``-ing the returned secret.
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
    """List credentials from system keyring, cryptfile backup, and env vars.

    Triple-read: checks system keyring, cryptfile, and ``os.environ`` for
    each known credential key.  The ENV column shows whether the key is
    currently set as an environment variable.

    Memory-safe: collects only KEY names, never batch-loads secret
    values.  For sync comparison, retrieves values one at a time
    and immediately ``del``-s each after comparison.
    """
    # ── 1. System keyring keys only (no values) ─────────────────
    keyring_keys: set[str] = set()
    entries = _enumerate_system_keyring(store_mod.DEFAULT_SERVICE, with_values=False)
    for k, _ in entries:
        keyring_keys.add(k)
    del entries

    # ── 2. Cryptfile keys only (no values) ─────────────────────
    cryptfile_keys: set[str] = set()
    cryptfile_path = config_mod.get_cryptfile_path()
    cryptfile_exists = os.path.exists(cryptfile_path)

    # Ensure backends are initialised before checking has_master_key()
    backend_mod.init_backend()

    master_pw: str | None = None
    if cryptfile_exists and backend_mod.has_master_key():
        master_pw = masked_input("Master password: ")
        if not master_pw.strip():
            del master_pw
            _err("master password is required.")
            return 1

        try:
            backend_mod.init_backend(password=master_pw)
            cf = backend_mod.get_cryptfile()
            if cf is not None:
                with backend_mod.unlocked_cryptfile(master_pw) as cf_ctx:
                    cryptfile_keys = set(store_mod._read_cryptfile_keys(cf_ctx))
        except Exception as exc:
            del master_pw
            _err(f"cannot read cryptfile — {exc}")
            return 1

    # ── 3. Env vars (keys only — never decode secret values) ────
    env_keys: set[str] = {k for k in keyring_keys | cryptfile_keys if os.environ.get(k)}

    # ── 4. Merge & display (values retrieved one-at-a-time) ────
    all_keys = sorted(keyring_keys | cryptfile_keys)

    if not all_keys:
        _print_empty_list(keyring_keys, cryptfile_exists, cryptfile_keys)
        # Clean up master password reference
        if master_pw is not None:
            del master_pw
        return 0

    # Unlock cryptfile once for comparison pass, or pass None
    cf = backend_mod.get_cryptfile()
    try:
        if cf is not None and master_pw is not None:
            with backend_mod.unlocked_cryptfile(master_pw) as cf_ctx:
                _print_credential_table_safe(
                    all_keys, keyring_keys, cryptfile_keys, env_keys,
                    cryptfile_exists, cf_ctx,
                )
        else:
            _print_credential_table_safe(
                all_keys, keyring_keys, cryptfile_keys, env_keys,
                cryptfile_exists, None,
            )
    finally:
        if master_pw is not None:
            del master_pw

    return 0


def _print_empty_list(
    keyring_keys: set[str],
    cryptfile_exists: bool,
    cryptfile_keys: set[str],
) -> None:
    """Print the empty-credential message with appropriate guidance."""
    print("No credentials stored.")
    print()
    if not keyring_keys and not cryptfile_exists:
        print("Run 'credstore set <KEY>' to store a credential.")
    elif not keyring_keys and not cryptfile_keys:
        print("Cryptfile exists but is empty.  Credentials in the")
        print("system keyring cannot be enumerated on this platform.")
        print("Run 'credstore set <KEY>' to populate both stores.")


def _print_credential_table_safe(
    all_keys: list[str],
    keyring_keys: set[str],
    cryptfile_keys: set[str],
    env_keys: set[str],
    cryptfile_exists: bool,
    cf_ctx,  # unlocked cryptfile or None
) -> None:
    """Print a formatted table of credentials with sync status.

    Shows four columns: KEY, SYSTEM KEYRING, CRYPTFILE, ENV, STATUS.

    Memory-safe: retrieves values ONE at a time for comparison
    and immediately ``del``-s each after use.  Never batch-loads
    all secrets into memory.
    """
    key_width = max(len(k) for k in all_keys) + 2

    print()
    print(f"  {'KEY':<{key_width}} SYSTEM KEYRING   CRYPTFILE        ENV    STATUS")
    print(f"  {'─' * (key_width - 2):─<{key_width}} ──────────────   ──────────────   ────   ──────")

    ring_only = 0
    crypt_only = 0
    synced = 0
    mismatched = 0
    env_set = 0

    for key in all_keys:
        in_ring = key in keyring_keys
        in_crypt = key in cryptfile_keys
        in_env = key in env_keys
        env_mark = "✔" if in_env else "—"
        if in_env:
            env_set += 1

        if in_ring and in_crypt:
            # Fetch ONE value from each store, compare, immediately del
            ring_val = store_mod.get_credential(key)
            crypt_val = (
                cf_ctx.get_password(store_mod.DEFAULT_SERVICE, key)
                if cf_ctx is not None
                else None
            )

            if ring_val is not None and crypt_val is not None:
                if ring_val == crypt_val:
                    synced += 1
                    ring_mark, crypt_mark, status = "✔", "✔", "synced"
                else:
                    mismatched += 1
                    ring_mark, crypt_mark, status = "✔", "✔", "MISMATCH ⚠"
                del crypt_val
            elif ring_val is not None:
                ring_only += 1
                ring_mark, crypt_mark, status = "✔", "—", "keyring only"
            else:
                crypt_only += 1
                ring_mark, crypt_mark, status = "—", "✔", "cryptfile only"

            del ring_val
        elif in_ring:
            ring_only += 1
            ring_mark, crypt_mark, status = "✔", "—", "keyring only"
        else:
            crypt_only += 1
            ring_mark, crypt_mark, status = "—", "✔", "cryptfile only"

        print(f"  {key:<{key_width}} {ring_mark:<13}   {crypt_mark:<14}   {env_mark:<4}   {status}")

    print(f"  {'─' * (key_width - 2):─<{key_width}} ──────────────   ──────────────   ────   ──────")
    parts = []
    if synced:
        parts.append(f"synced: {synced}")
    if ring_only:
        parts.append(f"system only: {ring_only}")
    if crypt_only:
        parts.append(f"cryptfile only: {crypt_only}")
    if mismatched:
        parts.append(f"mismatched: {mismatched}")
    if env_set:
        parts.append(f"env: {env_set}")
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
        del master_pw
        _err(str(exc))
        return 1

    del master_pw
    print(f"Restored {count} credential(s) to system keyring.")
    return 0


@requires_tty
def _cmd_reset_backup() -> int:
    """Reset cryptfile backup: sync all credentials from system keyring.

    Note: this MUST load all values into memory (needs them to write
    to cryptfile).  Values are ``del``-ed as soon as the sync completes.
    """
    print("Reset cryptfile backup from system keyring.")
    print()

    # with_values=True is required here — we genuinely need the secrets
    # to write them into the cryptfile backup
    entries = _enumerate_system_keyring(
        store_mod.DEFAULT_SERVICE, with_values=True,
    )
    if not entries:
        print("No credentials found in system keyring.")
        return 0

    print(f"Found {len(entries)} credential(s) in system keyring:")
    print()
    master_pw = masked_input("Master password: ")

    cf = backend_mod.get_cryptfile()
    if cf is None:
        del entries
        del master_pw
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
        del entries
        del master_pw
        _err("incorrect master password.")
        return 1

    # Explicit cleanup — batch of secrets no longer needed
    del entries
    del master_pw

    print()
    print(f"Reset {synced} credential(s) in cryptfile backup.")
    return 0


# ── enumeration (delegates to _enumerate.py) ──────────────────

from credstore._enumerate import enumerate_system_keyring as _enumerate_system_keyring  # noqa: E402 — kept as private alias for internal CLI use


if __name__ == "__main__":
    sys.exit(main())
