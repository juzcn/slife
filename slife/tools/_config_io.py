"""Shared config file read/write helpers.

Used by config_env.py and cli.py to avoid duplicating the same
json5 read/write logic across tool modules.
"""

import json5
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def with_fetched_at(source: dict | None) -> dict | None:
    """Return a copy of source dict with fetched_at timestamp added.

    Returns None if source is None or an empty dict.
    """
    if not source:
        return None
    result = dict(source)
    result.setdefault("fetched_at", now_iso())
    return result


def read_config(path: Path) -> dict:
    """Read and parse a JSON5 config file. Returns an empty dict on failure."""
    try:
        return json5.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Config file not found: %s", path)
        return {}
    except (ValueError, OSError) as e:
        logger.error("Cannot parse config %s: %s", path, e)
        return {}


def write_config(path: Path, raw: dict) -> None:
    """Write a dict to a JSON5 config file with indent=2 formatting."""
    path.write_text(json5.dumps(raw, indent=2, trailing_commas=False, ensure_ascii=False), encoding="utf-8")
