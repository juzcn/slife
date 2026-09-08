"""Shared config file read/write helpers.

Used by config_env.py and cli.py to avoid duplicating the same
json5 read/write logic across tool modules.
"""

import functools
import json5
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import filelock

from slife.paths import get_config_path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slife.config import Config
    from slife.tools.context import ToolContext

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


class ConfigParseError(ValueError):
    """Raised when slife.json5 exists but cannot be parsed.

    Distinct from ``FileNotFoundError`` (which :func:`read_config` treats as a
    normal first-run state).  A mutating caller that proceeded past a parse
    error would write back an empty dict via ``os.replace`` and destroy the
    whole config — so the parse failure must be surfaced, not swallowed.
    """


_comment_warned: set[str] = set()


def _warn_comment_loss(path: Path) -> None:
    """Warn (once per path) if *path* contains JSON5 comments that a rewrite
    would silently strip.  Cheap scan — read only when the file exists."""
    try:
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    # Comment markers that are not part of a quoted string would be
    # over-detected by a naive pass (a URL like "https://" trips "//"); a
    # conservative line-scan for comment-start patterns is enough to reach
    # the "it has comments" verdict the warning needs.  False positives only
    # cost a log line.
    key = str(path)
    if key in _comment_warned:
        return
    # Any line carrying '//' outside a URL, or any block comment.
    has_comment = (
        any("//" in line and "://" not in line for line in text.splitlines())
        or "/*" in text
    )
    if has_comment:
        _comment_warned.add(key)
        logger.warning(
            "config_rewrite_strips_comments path=%s — tool writes "
            "re-serialize as plain JSON and drop // /* */ comments",
            path,
        )


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
    this process; atomic replace is the cross-process guarantee.  Creates
    the parent directory on first write (the mcp-plugin fork's behaviour —
    both config paths sit in a data dir that may not exist yet).

    Note: ``json5.dumps`` emits plain JSON, so a tool write strips any
    ``//``/``/* */`` comments from the file.  Warn once per path so a user
    isn't surprised their annotated config just got rewritten flat.
    """
    _warn_comment_loss(path)
    text = json5.dumps(raw, indent=2, trailing_commas=False, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
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


def config_write_locked(fn):
    """Decorator: run a config-mutating tool's ``execute`` inside the
    cross-process read→mutate→write lock.

    Tools execute in PARALLEL (the agent loop gathers concurrent tool calls),
    so two mutators editing the same slife.json5 — e.g. two ``model_set``
    calls in one turn, or ``model_set`` + ``config_env_set`` — must not both
    read, both mutate their own copy, then both ``os.replace``.  The
    decorated method must take the config path from ``self._config_path``
    (the :class:`_ConfigPathMixin` convention).  Early returns (validation
    errors) happen inside the lock and write nothing — harmless.
    """
    @functools.wraps(fn)
    async def _wrapped(self, **kwargs):
        path = getattr(self, "_config_path", None)
        if path is not None:
            with config_read_modify_write(path):
                return await fn(self, **kwargs)
        return await fn(self, **kwargs)
    return _wrapped


@contextmanager
def config_read_modify_write(path: Path):
    """Hold a CROSS-PROCESS lock covering one config read→mutate→write.

    ``write_config`` alone is atomic (temp + os.replace), but the
    read-modify-write window around it — read raw, apply a change, write back
    — is a race between processes (the gateway config tools live in the child
    AND tool modules can read/write the same file).  Two writers both read,
    both mutate, both ``os.replace``: the second clobbers the first's change.
    This wraps that window in a cross-process lock on ``<path>.lock`` so the
    write that follows the read is the only one in flight (F8).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = filelock.FileLock(path.with_suffix(path.suffix + ".lock"))
    with lock:
        yield


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
