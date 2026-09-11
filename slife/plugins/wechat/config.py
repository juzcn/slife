"""Per-user WeChat session I/O — one ``wechat_<user>.json5`` file per login.

The file is one short-lived (~24h) session unit: login credentials plus the
``get_updates_buf`` ack cursor (D6).  The token expires in ~24h and does not
warrant credstore.

The cursor comes back from the iLink ``getupdates`` endpoint; passing it back
on the next poll tells the server we've seen everything up to there.  Keeping
it in the session file lets a restored session resume ack'ing instead of
re-receiving the unacked window — which the poll loop would otherwise re-ingest
as genuine duplicates once its 30s in-memory dedup window has passed (D6).
It is written only when it changes, and always atomically.

Config format::

    {
      bot_token: "u7mK...",
      base_url: "https://ilinkai.weixin.qq.com",
      saved_at: 1718400000.0,
      ilink_user_id: "",
      get_updates_buf: "ChAIARC...",
    }
"""

import os
import tempfile

import json5
import logging
from pathlib import Path

logger = logging.getLogger("slife_wechat")

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"


def _atomic_write_json5(path: Path, data: dict) -> None:
    """Write *data* to *path* atomically (temp file + rename).

    A cursor write must never be able to leave the session file half-written
    — readers see either the old or the new content, never a torn mix.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".wechat_cfg_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json5.dumps(data, indent=2))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _config_path(user: str, work_dir: Path | None = None) -> Path:
    """Return the path to the per-user WeChat config file."""
    wd = work_dir or Path(".")
    return wd / f"wechat_{user}.json5"


def load_wechat_config(
    user: str, work_dir: Path | None = None,
) -> dict:
    """Load WeChat session config for *user*.

    Returns a dict with keys ``bot_token``, ``base_url``, ``saved_at``,
    ``ilink_user_id``, ``get_updates_buf``.  Returns an empty dict if the
    config file does not exist or cannot be parsed.
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
        "get_updates_buf": raw.get("get_updates_buf", "") or "",
    }


def save_wechat_config(
    user: str, session: dict, work_dir: Path | None = None,
) -> Path:
    """Save (or update) WeChat session config for *user* (atomic write).

    *session* should contain ``bot_token``, ``base_url``, ``saved_at`` and
    optionally ``ilink_user_id`` and ``get_updates_buf``.
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
    get_updates_buf = session.get("get_updates_buf", "")
    if get_updates_buf:
        data["get_updates_buf"] = get_updates_buf

    _atomic_write_json5(path, data)
    logger.info("wechat_config_saved user=%s path=%s", user, path)
    return path


def update_wechat_updates_buf(
    user: str, get_updates_buf: str, work_dir: Path | None = None,
) -> Path | None:
    """Merge *get_updates_buf* into the session file for *user* (atomic).

    A cursor is only meaningful alongside a live session, so the write is
    skipped — returns None — when the session file is missing or tokenless.
    Everything else in the file (notably ``saved_at``, which drives the 24h
    expiry) is preserved untouched.
    """
    if not get_updates_buf:
        return None
    path = _config_path(user, work_dir)
    if not path.exists():
        return None
    try:
        raw = json5.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("wechat_cursor_merge_parse_failed path=%s", path)
        return None
    if not isinstance(raw, dict) or not raw.get("bot_token"):
        return None

    raw = dict(raw)
    raw["get_updates_buf"] = get_updates_buf
    _atomic_write_json5(path, raw)
    logger.debug("wechat_cursor_saved user=%s path=%s", user, path)
    return path


def clear_wechat_config(
    user: str, work_dir: Path | None = None,
) -> bool:
    """Delete the WeChat session file for *user* — the ack cursor goes with it."""
    path = _config_path(user, work_dir)
    if path.exists():
        path.unlink()
        logger.info("wechat_config_cleared user=%s path=%s", user, path)
        return True
    return False