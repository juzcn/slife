"""Per-user WeChat configuration I/O.

Each user gets their own ``wechat_<user>.json5`` file in the working
directory (alongside ``slife.json5``).  The file stores non-sensitive
metadata (base_url, saved_at, ilink_user_id); the ``bot_token`` is
stored in the OS keyring via credstore.

Config format::

    {
      keyring_ref: "keyring:slife/wechat/<user>",
      base_url: "https://ilinkai.weixin.qq.com",
      saved_at: 1718400000.0,
      ilink_user_id: "",
    }
"""

import json5
import logging
from pathlib import Path

logger = logging.getLogger("slife_wechat")

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
_KEYRING_KEY_PREFIX = "slife/wechat"


def _config_path(user: str, work_dir: Path | None = None) -> Path:
    """Return the path to the per-user WeChat config file."""
    wd = work_dir or Path(".")
    return wd / f"wechat_{user}.json5"


def load_wechat_config(
    user: str, work_dir: Path | None = None,
) -> dict:
    """Load WeChat session config for *user*.

    Retrieves bot_token from credstore (keyring).  Falls back to
    reading from the local json5 file for legacy configs — and
    auto-migrates those tokens to credstore on first read.

    Returns a dict with keys ``bot_token``, ``base_url``, ``saved_at``,
    ``ilink_user_id``.
    """
    path = _config_path(user, work_dir)
    keyring_key = f"{_KEYRING_KEY_PREFIX}/{user}"

    # ── Try credstore first ──
    from credstore import get_credential
    bot_token = get_credential(keyring_key) or ""

    if bot_token:
        # Read metadata from file
        meta = _read_meta_file(path)
        meta["bot_token"] = bot_token
        return meta

    # ── Legacy: plaintext token in file → auto-migrate ──
    if not path.exists():
        return {}

    try:
        raw = json5.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("wechat_config_parse_failed path=%s", path)
        return {}

    if not isinstance(raw, dict):
        return {}

    legacy_token = raw.get("bot_token", "")
    base_url = raw.get("base_url", DEFAULT_BASE_URL)
    saved_at = raw.get("saved_at", 0)
    ilink_user_id = raw.get("ilink_user_id", "")

    # Auto-migrate legacy plaintext token → credstore
    if legacy_token:
        try:
            _credstore_set(keyring_key, legacy_token)
            # Rewrite file without the plaintext token
            _write_meta_file(path, base_url, saved_at, ilink_user_id, keyring_key)
            logger.info("wechat_token_migrated user=%s key=%s", user, keyring_key)
        except Exception as exc:
            logger.warning("wechat_token_migrate_failed user=%s err=%s", user, exc)

    return {
        "bot_token": legacy_token,
        "base_url": base_url,
        "saved_at": saved_at,
        "ilink_user_id": ilink_user_id,
    }


def save_wechat_config(
    user: str, session: dict, work_dir: Path | None = None,
) -> Path:
    """Save (or update) WeChat session config for *user*.

    *session* should contain ``bot_token``, ``base_url``, ``saved_at``,
    and optionally ``ilink_user_id``.
    """
    path = _config_path(user, work_dir)
    keyring_key = f"{_KEYRING_KEY_PREFIX}/{user}"

    # Store bot_token via credstore CLI (masked stdin, never plaintext)
    bot_token = session.get("bot_token", "")
    if bot_token:
        _credstore_set(keyring_key, bot_token)

    base_url = session.get("base_url", DEFAULT_BASE_URL)
    saved_at = session.get("saved_at", 0)
    ilink_user_id = session.get("ilink_user_id", "")

    _write_meta_file(path, base_url, saved_at, ilink_user_id, keyring_key)
    logger.info("wechat_config_saved user=%s path=%s", user, path)
    return path


def clear_wechat_config(
    user: str, work_dir: Path | None = None,
) -> bool:
    """Delete the WeChat session for *user*.

    Removes bot_token from credstore and deletes the local metadata file.
    """
    path = _config_path(user, work_dir)
    keyring_key = f"{_KEYRING_KEY_PREFIX}/{user}"

    # Delete from credstore
    try:
        from credstore import delete_credential
        delete_credential(keyring_key)
    except Exception:
        pass

    # Delete local metadata file
    if path.exists():
        path.unlink()
        logger.info("wechat_config_cleared user=%s path=%s", user, path)
        return True
    return False


# ── internal helpers ─────────────────────────────────────────────


def _credstore_set(key: str, secret: str) -> None:
    """Store a credential via the credstore CLI.

    Uses subprocess with stdin pipe — the secret never appears in
    command-line arguments (which would leak to process listings).
    """
    import subprocess
    import shutil

    credstore_exe = shutil.which("credstore")
    if credstore_exe is None:
        # Fall back to running as a Python module
        import sys
        credstore_exe = sys.executable
        cmd = [credstore_exe, "-m", "credstore", "set", key]
    else:
        cmd = [credstore_exe, "set", key]

    proc = subprocess.run(
        cmd,
        input=secret + "\n",
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"credstore set failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )


def _read_meta_file(path: Path) -> dict:
    """Read non-sensitive metadata from the wechat config file."""
    if not path.exists():
        return {}
    try:
        raw = json5.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        "base_url": raw.get("base_url", DEFAULT_BASE_URL),
        "saved_at": raw.get("saved_at", 0),
        "ilink_user_id": raw.get("ilink_user_id", ""),
    }


def _write_meta_file(
    path: Path,
    base_url: str,
    saved_at: float,
    ilink_user_id: str,
    keyring_key: str,
) -> None:
    """Write non-sensitive metadata to the wechat config file."""
    import os
    # Use os.linesep instead of hardcoded newline
    parts = [
        "{",
        f'  keyring_ref: "{keyring_key}",',
        f'  base_url: "{base_url}",',
        f"  saved_at: {saved_at},",
    ]
    if ilink_user_id:
        parts.append(f'  ilink_user_id: "{ilink_user_id}",')
    parts.append("}\n")
    path.write_text("\n".join(parts), encoding="utf-8")
