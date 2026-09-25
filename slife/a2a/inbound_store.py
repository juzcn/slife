"""Persisted record of inbound A2A tasks — in flight, and orphaned by a restart.

An inbound task's completion bridge is **process-local**.  The SDK responder
runs a task's whole lifecycle (ack → working → artifact → terminal) inside the
process that received it, and ``MeshResponder`` resolves it from a dict that
lives no longer than that process.  The reply path is process-local too: the
requester's ``ResponseTopic`` and ``CorrelationData`` are read off the inbound
request's MQTT properties and never leave it.

So a restart orphans every inbound task still in flight — the bridge is gone,
and with it any way to publish the terminal the task is owed.  Nothing on the
wire can fix that: the peer's reply topic is not reconstructible, and the SDK
offers no post-restart completion path.

This store is what lets a restarted process tell those tasks apart from ids it
has simply never seen.  Every inbound task is written here when it arrives and
removed when it leaves the responder, so whatever a fresh process finds on disk
is exactly the set its predecessor died holding.  Those are reported *stale* —
never completable — so the model answers their peers with a plain message
instead of walking into a refused ``task_response``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ruamel.yaml import YAMLError

from slife.tools._yaml_doc import new_yaml, render

logger = logging.getLogger(__name__)

#: Bound each section — a peer that never answers must not grow the file
#: without limit.  A2A task arrivals are far below this in any sane session.
_MAX_ENTRIES = 200


def _iso_now() -> str:
    """Current UTC time as an ISO-8601 string (official A2A timestamps)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_path() -> Path:
    """``<slife data dir>/a2a_inbound.yaml``.

    ``get_data_dir()`` honours ``$SLIFE_DATA_DIR``, which the host exports so
    plugin children resolve the same directory as the main process.  YAML, like
    every other file in that directory (``slife.yaml``, ``tools.yaml``,
    ``wechat_<agent>.yaml``): this is state a human may well open — an entry
    here is a peer still owed a reply — so it reads as a document rather than
    as one long line.
    """
    from slife.paths import get_data_dir

    return get_data_dir() / "a2a_inbound.yaml"


def resolve_path() -> Path:
    """The inbound-state path for this process.

    ``$A2A_INBOUND_FILE`` (test/dev override) > slife data dir default.
    """
    env = os.environ.get("A2A_INBOUND_FILE")
    if env:
        return Path(env).expanduser()
    return default_path()


@dataclass(frozen=True)
class InboundTask:
    """One inbound A2A task, as this store tracks it."""

    task_id: str
    peer: str
    """Sending peer's agent name."""

    since: str
    """ISO-8601 UTC arrival time."""

    def as_dict(self) -> dict:
        return {"task_id": self.task_id, "peer": self.peer, "since": self.since}


class InboundStore:
    """In-flight inbound tasks, persisted across restarts.

    ``pending`` — tasks this process is holding a completion bridge for.
    ``stale`` — tasks a *previous* process was holding.  They can never be
    completed, and stay listed until their peer is answered or the task is
    re-registered by a retry.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else resolve_path()
        self._pending: dict[str, InboundTask] = {}
        self._stale: dict[str, InboundTask] = {}
        self._load()

    # ── Load / save ───────────────────────────────────────────────────

    def _load(self) -> None:
        """Adopt the previous run's ``pending`` as ``stale``.

        This process starts holding no bridges, so everything the last one was
        holding is orphaned by definition.  Anything already stale stays stale
        — a task orphaned by a restart two restarts ago is no more completable
        than one orphaned by the last, and dropping it here would silently
        forgive a reply the peer is still owed — but the section is still held
        to :data:`_MAX_ENTRIES` like the live one.
        """
        try:
            raw = new_yaml().load(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, YAMLError) as e:
            # A corrupt state file must never block the mesh — losing it costs
            # the stale list, which is a reminder, not protocol state.
            logger.warning("a2a_inbound_state_unreadable err=%s", e)
            return
        if not isinstance(raw, dict):
            return
        self._stale = self._parse(raw.get("stale"))
        for task_id, task in self._parse(raw.get("pending")).items():
            self._stale.setdefault(task_id, task)
        # ``_trim`` runs on ``pending`` at every add, but ``stale`` is only ever
        # filled HERE — so without this the section was exempt from the cap the
        # class documents, growing across restarts without limit, and the whole
        # list is rendered into every turn's prompt.
        self._trim(self._stale)
        if self._stale:
            logger.info("a2a_inbound_orphaned count=%d", len(self._stale))

    def _parse(self, section) -> dict[str, InboundTask]:
        out: dict[str, InboundTask] = {}
        if not isinstance(section, dict):
            return out
        for task_id, entry in section.items():
            if not isinstance(entry, dict):
                continue
            out[str(task_id)] = InboundTask(
                task_id=str(task_id),
                peer=str(entry.get("peer", "unknown")),
                since=str(entry.get("since", "")),
            )
        return out

    def _save(self) -> None:
        """Write atomically — a half-written file read by the next process
        would look like corruption (and the crash that caused it is exactly
        when the stale list matters most).

        The light writer, deliberately, not ``_config_io.write_config``: this
        file is rewritten on every inbound task's arrival and completion, and
        the config writer's job is preserving a *human's* document — it parses,
        diffs, dumps and re-verifies (about 20 ms a write and an fsync, against
        ~1 ms here).  There is nothing here to preserve: the file is machine-
        written, and losing it costs a reminder rather than protocol state (see
        the module docstring), so the atomic replace alone is the guarantee it
        needs — a reader sees all of the old file or all of the new one.
        """
        payload = {
            "pending": {k: v.as_dict() for k, v in self._pending.items()},
            "stale": {k: v.as_dict() for k, v in self._stale.items()},
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(render(payload), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning("a2a_inbound_state_write_failed err=%s", e)

    @staticmethod
    def _trim(section: dict[str, InboundTask]) -> None:
        """Drop the oldest entries past the cap (in place)."""
        if len(section) <= _MAX_ENTRIES:
            return
        ordered = sorted(section.values(), key=lambda t: t.since or "")
        for task in ordered[: len(section) - _MAX_ENTRIES]:
            section.pop(task.task_id, None)

    # ── Live inbound tasks ────────────────────────────────────────────

    def add(self, task_id: str, peer: str) -> None:
        """A task just arrived — this process now holds its bridge.

        Clears any stale entry for the same id: a peer that retries re-sends
        the same ``Task.id``, and the retry genuinely re-registers the bridge,
        so the task is completable again.
        """
        if not task_id:
            return
        was_stale = self._stale.pop(task_id, None)
        self._pending[task_id] = InboundTask(task_id, peer, _iso_now())
        self._trim(self._pending)
        if was_stale is not None:
            logger.info("a2a_inbound_revived task=%s peer=%s", task_id, peer)
        self._save()

    def drop(self, task_id: str) -> None:
        """The task left the responder — completed, cancelled, or the peer
        vanished.  It is no longer awaiting anything from this process."""
        if self._pending.pop(task_id, None) is None:
            return
        self._save()

    # ── Stale (restart-orphaned) tasks ────────────────────────────────

    def stale(self) -> list[InboundTask]:
        """Tasks a previous process died holding, oldest first."""
        return sorted(self._stale.values(), key=lambda t: t.since or "")

    def stale_entry(self, task_id: str) -> InboundTask | None:
        return self._stale.get(task_id)

    def clear_stale_peer(self, peer: str) -> None:
        """Forget this peer's orphaned tasks — the model has answered them
        with a plain message, which is the only reply they can still get."""
        drop = [k for k, v in self._stale.items() if v.peer == peer]
        if not drop:
            return
        for task_id in drop:
            self._stale.pop(task_id, None)
        self._save()
