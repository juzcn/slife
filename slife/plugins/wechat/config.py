"""Per-user WeChat configuration I/O.

Each user gets their own ``wechat_<user>.json5`` file in the working
directory (alongside ``slife.json5``).  The bot token is stored directly
in the file — it is short-lived (~24h) and does not warrant credstore.

Config format::

    {
      bot_token: "u7mK...",
      base_url: "https://ilinkai.weixin.qq.com",
      saved_at: 1718400000.0,
      ilink_user_id: "",
    }
"""

import json
import os
import tempfile

import json5
import logging
from pathlib import Path

logger = logging.getLogger("slife_wechat")

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"


# ── Sync state (dedupe/replay ack), a small sidecar next to the session ──
# The iLink getupdates endpoint returns an opaque ``get_updates_buf`` ack;
# passing it back on the next poll tells the server we've seen up to there.
# Persisting it across a restart lets a restored session resume ack'ing
# instead of re-receiving the unacked window — which the poll loop would
# re-ingest as genuine duplicates once its 30s in-memory dedup window has
# passed (D6).


def _sync_path(user: str, work_dir: Path | None = None) -> Path:
    wd = work_dir or Path(".")
    return wd / f"wechat_{user}_sync.json"


def load_wechat_sync(user: str, work_dir: Path | None = None) -> dict:
    """Load the persisted sync state (``get_updates_buf``) for *user*."""
    path = _sync_path(user, work_dir)
    if not path.exists():
        return {"get_updates_buf": ""}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("wechat_sync_parse_failed path=%s", path)
        return {"get_updates_buf": ""}
    if not isinstance(raw, dict):
        return {"get_updates_buf": ""}
    return {"get_updates_buf": raw.get("get_updates_buf", "") or ""}


def save_wechat_sync(
    user: str, get_updates_buf: str, work_dir: Path | None = None,
) -> Path:
    """Persist the current ``get_updates_buf`` ack for *user* (atomic write)."""
    path = _sync_path(user, work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".wechat_sync_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"get_updates_buf": get_updates_buf}, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    logger.debug("wechat_sync_saved user=%s path=%s", user, path)
    return path


def clear_wechat_sync(user: str, work_dir: Path | None = None) -> None:
    """Delete the persisted sync state (on logout / fresh login)."""
    path = _sync_path(user, work_dir)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
        logger.info("wechat_sync_cleared user=%s", user)


def _config_path(user: str, work_dir: Path | None = None) -> Path:
    """Return the path to the per-user WeChat config file."""
    wd = work_dir or Path(".")
    return wd / f"wechat_{user}.json5"


def load_wechat_config(
    user: str, work_dir: Path | None = None,
) -> dict:
    """Load WeChat session config for *user*.

    Returns a dict with keys ``bot_token``, ``base_url``, ``saved_at``,
    ``ilink_user_id``.  Returns an empty dict if the config file does
    not exist or cannot be parsed.
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
    and optionally ``ilink_user_id``.
    """
    path = _config_path(user, work_dir)

    data = {
        "bot_token": session.get("bot_token", ""),
        "base_url": session.get("base_url", DEFAULT_BASE_URL),
        "saved_at": session.get("saved_at", 0),
    }
    ilink_user_id = session.get("ilink_user_id", "")
    if ilink_user_id:
        data["ilink_user_id"] = ilink_user_id

    path.write_text(json5.dumps(data, indent=2), encoding="utf-8")
    logger.info("wechat_config_saved user=%s path=%s", user, path)
    return path


def clear_wechat_config(
    user: str, work_dir: Path | None = None,
) -> bool:
    """Delete the WeChat session file for *user*."""
    path = _config_path(user, work_dir)
    if path.exists():
        path.unlink()
        logger.info("wechat_config_cleared user=%s path=%s", user, path)
        return True
    return False
