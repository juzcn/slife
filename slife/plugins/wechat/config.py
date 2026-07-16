"""Per-user WeChat configuration I/O.

Each user gets their own ``wechat_<user>.json5`` file in the working
directory (alongside ``slife.json5``).  The file is created automatically
on first successful ``wechat_login`` and cleared on ``wechat_logout``.

Config format::

    {
      bot_token: "<token>",
      base_url: "https://ilinkai.weixin.qq.com",
      saved_at: 1718400000.0,   // epoch seconds
    }
"""

import json5
import logging
from pathlib import Path

logger = logging.getLogger("slife_wechat")

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"


def _config_path(user: str, work_dir: Path | None = None) -> Path:
    """Return the path to the per-user WeChat config file."""
    wd = work_dir or Path(".")
    return wd / f"wechat_{user}.json5"


def load_wechat_config(
    user: str, work_dir: Path | None = None,
) -> dict:
    """Load WeChat session config for *user*.

    Returns a dict with keys ``bot_token``, ``base_url``, ``saved_at``.
    Returns an empty dict if the file doesn't exist or is malformed.
    """
    path = _config_path(user, work_dir)
    if not path.exists():
        return {}

    try:
        raw = json5.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("wechat_config_parse_failed path=%s", path)
        return {}

    if not isinstance(raw, dict):
        return {}

    return {
        "bot_token": raw.get("bot_token", ""),
        "base_url": raw.get("base_url", DEFAULT_BASE_URL),
        "saved_at": raw.get("saved_at", 0),
        "ilink_user_id": raw.get("ilink_user_id", ""),
    }


def save_wechat_config(
    user: str, session: dict, work_dir: Path | None = None,
) -> Path:
    """Save (or update) WeChat session config for *user*.

    *session* should contain ``bot_token``, ``base_url``, ``saved_at``,
    and optionally ``ilink_user_id`` (the user's WeChat ID for messaging).
    Returns the path written to.
    """
    path = _config_path(user, work_dir)
    data = {
        "bot_token": session.get("bot_token", ""),
        "base_url": session.get("base_url", DEFAULT_BASE_URL),
        "saved_at": session.get("saved_at", 0),
        "ilink_user_id": session.get("ilink_user_id", ""),
    }
    # Write as JSON5-compatible JSON
    parts = [
        "{",
        f'  bot_token: "{data["bot_token"]}",',
        f'  base_url: "{data["base_url"]}",',
        f"  saved_at: {data['saved_at']},",
    ]
    if data["ilink_user_id"]:
        parts.append(f'  ilink_user_id: "{data["ilink_user_id"]}",')
    parts.append("}\n")
    path.write_text("\n".join(parts), encoding="utf-8")
    logger.info("wechat_config_saved user=%s path=%s", user, path)
    return path


def clear_wechat_config(
    user: str, work_dir: Path | None = None,
) -> bool:
    """Delete the WeChat session config file for *user*.

    Returns True if the file was deleted, False if it didn't exist.
    """
    path = _config_path(user, work_dir)
    if path.exists():
        path.unlink()
        logger.info("wechat_config_cleared user=%s path=%s", user, path)
        return True
    return False
