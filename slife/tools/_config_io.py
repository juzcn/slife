"""Shared config file read/write helpers.

Used by config_env.py and cli.py to avoid duplicating the same
YAML read/write logic across tool modules.
"""

import asyncio
import functools
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import filelock
from ruamel.yaml.error import YAMLError

from slife.paths import get_config_path
from slife.tools._yaml_doc import new_yaml, render_document
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe
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
    """Raised when slife.yaml exists but cannot be parsed.

    Distinct from ``FileNotFoundError`` (which :func:`read_config` treats as a
    normal first-run state).  A mutating caller that proceeded past a parse
    error would write back an empty dict via ``os.replace`` and destroy the
    whole config — so the parse failure must be surfaced, not swallowed.
    """


class ConfigLockTimeout(TimeoutError):
    """Raised when the cross-process config lock is not acquired in time.

    The lock wait is bounded by the registry's storage.filelock (was blocking
    forever on a stale ``<path>.lock``).  A timeout fails the read-modify-write
    loudly instead of hanging the caller.
    """


def read_config(path: Path) -> dict:
    """Read and parse a YAML config file.

    Returns ``{}`` only when the file does not exist (first run).  A file that
    exists but cannot be parsed raises :class:`ConfigParseError` so mutating
    callers abort instead of rewriting the config as an empty dict.
    """
    try:
        raw = new_yaml().load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("config_not_found path=%s", path)
        return {}
    except (YAMLError, ValueError, OSError) as e:
        logger.error("config_parse_error path=%s err=%s", path, e)
        raise ConfigParseError(f"Cannot parse config {path}: {e}") from e
    # An empty file loads as None and a top-level list as a sequence; neither is
    # usable by any caller.  Both are surfaced like a parse error rather than
    # returned, so a mutating caller aborts instead of rewriting the file.
    if not isinstance(raw, dict):
        logger.error("config_not_mapping path=%s type=%s", path, type(raw).__name__)
        raise ConfigParseError(f"Cannot parse config {path}: not a mapping")
    return raw


_write_lock = threading.Lock()


def write_config(path: Path, raw: dict) -> None:
    """Atomically write a dict to a YAML config file.

    Writes to a temp file in the same directory then ``os.replace()`` — a
    reader never sees a truncated/interleaved file and a crash mid-write
    can't corrupt the config. The lock serializes writers in
    this process; atomic replace is the cross-process guarantee.  Creates
    the parent directory on first write (the mcp-gateway fork's behaviour —
    both config paths sit in a data dir that may not exist yet).

    Atomic, and the file's **comments survive**: the write edits the existing
    document rather than re-serializing the dict, so ``#`` annotations,
    indentation and key order stay put (see ``slife/tools/_yaml_doc.py``).
    A file that does not exist yet — or one that cannot be read as a document
    — is rendered fresh.
    """
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    text = render_document(current, raw)
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
    so two mutators editing the same slife.yaml — e.g. two ``model_set``
    calls in one turn, or ``model_set`` + ``config_env_set`` — must not both
    read, both mutate their own copy, then both ``os.replace``.  The
    decorated method must take the config path from ``self._config_path``
    (the :class:`_ConfigPathMixin` convention).  Early returns (validation
    errors) happen inside the lock and write nothing — harmless.

    The lock is HELD across the tool body (that is the point — the read and
    the write it protects are on either side of the await), but it is TAKEN by
    non-blocking polling rather than a blocking acquire, which would freeze
    the loop for the whole timeout.  That freeze is a real deadlock, not just
    a stall: tool calls run concurrently on one loop, so while call A holds
    the lock waiting on its inner await, call B's blocking acquire would stop
    the loop and A could never resume to release it — nor could any heartbeat
    or timer.  See :func:`_acquire_config_lock`.
    """
    @functools.wraps(fn)
    async def _wrapped(self, **kwargs):
        path = getattr(self, "_config_path", None)
        if path is None:
            return await fn(self, **kwargs)
        lock = _file_lock_for(path)
        await _acquire_config_lock(lock, path)
        try:
            return await fn(self, **kwargs)
        finally:
            try:
                lock.release()
            except Exception:  # noqa: BLE001 — a release failure must not fail the edit
                logger.debug("config_lock_release_failed path=%s", path, exc_info=True)
    return _wrapped


async def _acquire_config_lock(lock: filelock.FileLock, path: Path) -> None:
    """Take *lock* without blocking the event loop, or raise ConfigLockTimeout.

    Polls with *non-blocking* attempts, from the loop thread — deliberately
    NOT one blocking ``acquire()`` on a worker thread.  filelock keeps its
    reentrancy counter PER THREAD, so a lock acquired on one thread must be
    released on that thread: acquiring via ``run_daemon`` and then releasing
    from the caller's ``finally`` leaves the OS lock held, and the next waiter
    burns the full timeout and fails even though the first call finished
    immediately (measured: two concurrent calls that should serialize in
    0.6s took 10.0s with one ConfigLockTimeout).  A non-blocking attempt
    cannot stall the loop, and acquire/release stay on the same thread.
    """
    try:
        async with asyncio.timeout(_timeouts.timeouts.storage.filelock):
            while True:
                try:
                    lock.acquire(blocking=False)
                    return
                except filelock.Timeout:
                    # Held elsewhere (another process, or a concurrent tool
                    # call in this one) — wait a poll interval and try again.
                    # filelock's own interval, so there is no second pacing
                    # constant to keep in sync with the registry.
                    await asyncio.sleep(lock.poll_interval)
    except TimeoutError as e:
        raise ConfigLockTimeout(
            f"Could not acquire config lock for {path} within "
            f"{_timeouts.timeouts.storage.filelock:g}s"
        ) from e


def _file_lock_for(path: Path) -> filelock.FileLock:
    """The cross-process lock guarding one config file's read→mutate→write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return filelock.FileLock(
        path.with_suffix(path.suffix + ".lock"),
        timeout=_timeouts.timeouts.storage.filelock,  # bounded (was blocking forever)
    )


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

    SYNCHRONOUS, for the callers that are synchronous functions
    (``switch_model``, the gateway's config helpers, ``write_embedding_config``).
    An ``async def`` caller with an await inside the block must use
    :func:`config_write_locked` instead — that path acquires off the event
    loop, which this one cannot do.
    """
    lock = _file_lock_for(path)
    try:
        with lock:
            yield
    except filelock.Timeout as e:
        raise ConfigLockTimeout(
            f"Could not acquire config lock for {path} within "
            f"{_timeouts.timeouts.storage.filelock:g}s"
        ) from e


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


# ── Mixin for tools that read/write slife.yaml ──────────────────────


class _ConfigPathMixin:
    """Shared __init__ + from_config for tools that need the config path.

    Used by cli.py (4 tools) and config_env.py (3 tools) — same pattern
    as ``_SkillDirMixin`` in ``skill.py``.
    """

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or get_config_path()

    def _require_config(self) -> str | None:
        """Require a config path before a read-only config tool runs.

        Returns ``None`` when ``_config_path`` is available, else the shared
        "config path not available" error.  Every config-reading tool spelled
        this guard out inline before it moved here.
        """
        if not self._config_path:
            return "Error: config path not available."
        return None

    @classmethod
    def from_config(cls, cfg: dict, config: "Config | None", ctx: "ToolContext | None" = None):  # pyright: ignore[reportIncompatibleMethodOverride]
        path = config._path if config else None
        tool = cls(config_path=path)
        if ctx is not None:
            object.__setattr__(tool, "_ctx", ctx)
        return tool
