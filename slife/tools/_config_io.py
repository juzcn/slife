"""Shared config file read/write helpers.

Used by config_env.py and cli.py to avoid duplicating the same
json5 read/write logic across tool modules.
"""

import json5
import logging
from datetime import datetime, timezone
from pathlib import Path

from slife.paths import get_config_path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slife.config import Config
    from slife.tools.context import ToolContext

logger = logging.getLogger(__name__)

# ── Module-level Config reference ──────────────────────────────────────
# Set by AgentService at startup so tool modules can access the parsed
# Config instead of re-reading slife.json5 ad-hoc.
_current_config: "Config | None" = None


def get_config() -> "Config | None":
    """Return the live :class:`Config` instance, or None before startup."""
    return _current_config


def set_config(config: "Config") -> None:
    """Set the live Config (called by AgentService at startup)."""
    global _current_config
    _current_config = config


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
        logger.warning("config_not_found path=%s", path)
        return {}
    except (ValueError, OSError) as e:
        logger.error("config_parse_error path=%s err=%s", path, e)
        return {}


def write_config(path: Path, raw: dict) -> None:
    """Write a dict to a JSON5 config file with indent=2 formatting."""
    path.write_text(json5.dumps(raw, indent=2, trailing_commas=False, ensure_ascii=False), encoding="utf-8")


def format_source_info(source: object) -> str:
    """Format a source provenance dict into a human-readable string.

    Accepts ``{type, url, version}`` and returns a string like
    ``"github — https://example.com — v1.0.0"``.
    Returns ``""`` if source is not a non-empty dict.
    """
    if not isinstance(source, dict) or not source:
        return ""
    parts = []
    if source.get("type"):
        parts.append(source["type"])
    if source.get("url"):
        parts.append(source["url"])
    if source.get("version"):
        parts.append(f"v{source['version']}")
    return " — ".join(parts) if parts else ""


# ── Mixin for tools that read/write slife.json5 ──────────────────────


class _ConfigPathMixin:
    """Shared __init__ + from_config for tools that need the config path.

    Used by cli.py (4 tools) and config_env.py (3 tools) — same pattern
    as ``_SkillDirMixin`` in ``skill.py``.
    """

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or get_config_path()

    @classmethod
    def from_config(cls, cfg: dict, config: "Config | None", ctx: "ToolContext | None" = None):  # pyright: ignore[reportIncompatibleMethodOverride]
        path = config._path if config else None
        tool = cls(config_path=path)
        if ctx is not None:
            object.__setattr__(tool, "_ctx", ctx)
        return tool
