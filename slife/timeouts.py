"""Central registry for every timeout value in slife.

All timeout values live HERE as typed dataclass defaults — this module is the
single source of truth.  There is no external YAML/config file and no second
seat for the same value: a developer tunes a value by editing the dataclass
default and its comment, and a structurally invalid edit fails loudly at
import (:func:`validate`).

CONVENTION (enforced by tests/test_no_magic_timeouts.py):

- Consumers do call-time lookups — ``_T.timeouts.<role>.<key>`` — and NEVER
  import-capture values into module constants or def-time default args, so
  tests can monkeypatch ``slife.timeouts.timeouts.<role>.<key>`` at runtime.

``timeouts.py`` intentionally imports nothing from ``slife.*`` (``config.py``
imports us; a cycle would break both).  stdlib only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields


class TimeoutConfigError(ValueError):
    """A registry value violates a type / non-negativity / invariant rule."""


# ── Role dataclasses — the values themselves (single source of truth) ──


@dataclass
class Work:
    tool_budget: float = 120.0    # loop wait_for wrap for tools w/o native `timeout`/`_timeout`
    task_budget: float = 120.0    # subagent task bound + subagent stream cap
    stall: float = 120.0          # LLM stream inactivity watchdog (resets per chunk)
    shell: float = 120.0          # execute_shell default timeout — generous backstop: the agent's injected timeout overrides it, so this base must not preempt longer work
    pip_install: float = 120.0    # pip_install tool deadline
    save_memory: float = 10.0     # memdb save_turn / advance_context_start bound


@dataclass
class Ready:
    plugin_start: float = 60.0
    spawn: float = 60.0
    connect_attempt: float = 10.0
    connect_startup: float = 120.0
    signal: float = 60.0
    stderr_line: float = 1.0
    relisten: float = 1.0        # backoff before re-opening a dropped change stream
    relisten_max: float = 30.0   # cap on that backoff — a peer that keeps rejecting
                                 # re-listen (e.g. its subscription quota is full)
                                 # must not be asked every second forever
    probe_broker: float = 1.0
    probe_endpoint: float = 5.0
    tunnel_start: float = 45.0
    tunnel_read_url: float = 30.0
    tunnel_settle: float = 20.0
    tunnel_heal: float = 180.0    # how long a published tunnel may stay unreachable
                                  # from the edge before the monitor respawns it —
                                  # a CLI child that loses its edge connection
                                  # re-registers on its own and KEEPS its hostname,
                                  # while a respawn mints a new one and strands
                                  # every link already handed out
    connect_retry_delay: float = 0.5
    sharefile_retry_delay: float = 2.0
    list_tools: float = 20.0
    tool_sync_wait: float = 75.0  # how long the startup tool-set line waits on a
                                  # server that is UP but has not managed a
                                  # tools/list yet before reporting what it has
                                  # (its first listing can time out and succeed on
                                  # the retry).  A server whose transport never
                                  # came up is not waited on at all — a failed
                                  # spawn is a settled verdict, marked
                                  # unavailable.  Follows the gateway's re-list
                                  # backoff (5→10→20→40, capped at 60 —
                                  # mcp_gateway/connection.py), so the wait ends
                                  # where that retry stops growing.
    watchdog_backoff_initial: float = 1.0
    watchdog_backoff_max: float = 30.0
    watchdog_backoff_multiplier: float = 2.0


@dataclass
class Grace:
    gentle: float = 3.0
    force: float = 5.0
    cleanup: float = 2.0
    shutdown: float = 3.0
    tunnel_kill: float = 5.0


@dataclass
class Transport:
    connect: float = 10.0
    pool: float = 10.0
    read: float | None = None      # DELEGATED — owned by the loop's tool budget
    write: float | None = None     # DELEGATED — owned by the loop's tool budget
    oauth: float = 30.0
    poll_oauth: float = 300.0
    embed: float = 60.0
    embed_api: float = 10.0
    media_download: float = 300.0
    media_request: float = 180.0
    media_connect: float = 30.0
    media_deadline: float = 1200.0  # dashscope/mcp media generate_video poll deadline (per call)
    wechat_poll: float = 120.0
    url_download: float = 30.0
    qr_deadline: float = 600.0


@dataclass
class Stream:
    retries: int = 2
    retry_base_delay: float = 0.5


@dataclass
class Storage:
    sqlite_busy: float = 5.0
    filelock: float = 10.0


@dataclass
class Deliver:
    mqtt_connect: float = 15.0
    reply_first: float = 15.0
    keepalive: float = 25.0
    retry_delay: float = 1.0


@dataclass
class Timeouts:
    work: Work = field(default_factory=Work)
    ready: Ready = field(default_factory=Ready)
    grace: Grace = field(default_factory=Grace)
    transport: Transport = field(default_factory=Transport)
    stream: Stream = field(default_factory=Stream)
    storage: Storage = field(default_factory=Storage)
    deliver: Deliver = field(default_factory=Deliver)


# ── Validation ──────────────────────────────────────────────────────────


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(ts: Timeouts) -> list[str]:
    """Return a list of human-readable violations (empty when valid)."""
    errs: list[str] = []
    for role, obj in (
        ("work", ts.work), ("ready", ts.ready), ("grace", ts.grace),
        ("transport", ts.transport), ("stream", ts.stream),
        ("storage", ts.storage), ("deliver", ts.deliver),
    ):
        for f in fields(obj):
            v = getattr(obj, f.name)
            if f.name in ("read", "write"):
                if v is not None and not (_is_number(v) and v >= 0):
                    errs.append(f"timeouts.{role}.{f.name} must be null or >= 0, got {v!r}")
                continue
            if f.name == "retries":
                if not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
                    errs.append(f"timeouts.{role}.{f.name} must be a non-negative int, got {v!r}")
                continue
            if not _is_number(v):
                errs.append(f"timeouts.{role}.{f.name} must be a number, got {type(v).__name__}")
                continue
            if v < 0 or not math.isfinite(v):
                errs.append(f"timeouts.{role}.{f.name} must be finite and >= 0, got {v!r}")
    if not (0 <= ts.grace.gentle <= ts.grace.force):
        errs.append(f"invariant: grace.gentle({ts.grace.gentle}) <= grace.force({ts.grace.force})")
    if not (ts.ready.relisten <= ts.ready.connect_attempt <= ts.ready.spawn):
        errs.append("invariant: ready.relisten <= ready.connect_attempt <= ready.spawn")
    if ts.ready.relisten_max < ts.ready.relisten:
        errs.append("invariant: ready.relisten_max >= ready.relisten")
    if ts.ready.connect_startup < ts.ready.spawn:
        errs.append("invariant: ready.connect_startup >= ready.spawn")
    if ts.ready.tunnel_heal < ts.ready.tunnel_read_url:
        # A running child gets at least the patience a fresh start gets, or
        # the monitor would respawn one that was never given time to heal.
        errs.append("invariant: ready.tunnel_heal >= ready.tunnel_read_url")
    if ts.work.stall <= 0:
        errs.append("invariant: work.stall must be > 0")
    return errs


def checked(ts: Timeouts) -> Timeouts:
    """Validate *ts* and raise :class:`TimeoutConfigError` on any violation.

    The raising counterpart of :func:`validate` — used at import for the
    singleton and by tests to assert a bad edit fails loudly.
    """
    errs = validate(ts)
    if errs:
        raise TimeoutConfigError("; ".join(errs))
    return ts


timeouts = checked(Timeouts())  # a structurally invalid edit fails at import