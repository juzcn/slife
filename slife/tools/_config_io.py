"""Shared config file read/write helpers.

Used by config_env.py and cli.py to avoid duplicating the same
json5 read/write logic across tool modules.
"""

import json5
import logging
import os
import tempfile
import threading
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


class ConfigParseError(ValueError):
    """Raised when slife.json5 exists but cannot be parsed.

    Distinct from ``FileNotFoundError`` (which :func:`read_config` treats as a
    normal first-run state).  A mutating caller that proceeded past a parse
    error would write back an empty dict via ``os.replace`` and destroy the
    whole config — so the parse failure must be surfaced, not swallowed.
    """


def read_config(path: Path) -> dict:
    """Read and parse a JSON5 config file.

    Returns ``{}`` only when the file does not exist (first run).  A file that
    exists but cannot be parsed raises :class:`ConfigParseError` so mutating
    callers abort instead of rewriting the config as an empty dict.
    """
    try:
        return json5.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("config_not_found path=%s", path)
        return {}
    except (ValueError, OSError) as e:
        logger.error("config_parse_error path=%s err=%s", path, e)
        raise ConfigParseError(f"Cannot parse config {path}: {e}") from e


_write_lock = threading.Lock()


def write_config(path: Path, raw: dict) -> None:
    """Atomically write a dict to a JSON5 config file.

    Writes to a temp file in the same directory then ``os.replace()`` — a
    reader never sees a truncated/interleaved file and a crash mid-write
    can't corrupt the config. The lock serializes writers in
    this process; atomic replace is the cross-process guarantee.
    """
    text = json5.dumps(raw, indent=2, trailing_commas=False, ensure_ascii=False)
    with _write_lock:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            # Preserve the existing file's mode — mkstemp creates 0600, which
            # would silently tighten a previously readable config.
            if path.exists():
                try:
                    os.chmod(tmp, path.stat().st_mode & 0o7777)
                except OSError:
                    pass
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


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
