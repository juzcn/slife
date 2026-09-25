"""Central registry for every time value in slife.

All time values live HERE as typed dataclass defaults — this module is the
single source of truth.  There is no external YAML/config file and no second
seat for the same value: a developer tunes a value by editing the dataclass
default and its comment, and a structurally invalid edit fails loudly at
import (:func:`validate`).

Two kinds of value share the file, because one file answering "what is this
number and who owns it" beats two:

- **budgets** (``work`` / ``ready`` / ``grace`` / ``transport`` / ``stream`` /
  ``storage`` / ``deliver``) bound a single await — the owner-of-await rule.
- **cadences** (``pacing``) are how OFTEN something runs — a poll period, a
  heartbeat, a backoff step, a session lifetime.  A cadence never bounds an
  await and a budget never sets a cadence; the role keeps that visible.

Both are registry-owned.  A cadence left as a module constant is a second seat
for a value the next reader has to go find, so the gate
(``tests/test_no_magic_timeouts.py``) scans for time-valued literals and names
alike, in ``slife/`` and in ``tests/``.

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
    task_lifetime: float = 3600.0  # async worker task: a wedge backstop, not a
                                  # caller's budget.  Nobody awaits an async
                                  # task, so no await owns it — but a task
                                  # that never ends wedges a serial worker and
                                  # everything queued behind it, and nothing
                                  # else would ever notice.  Deliberately far
                                  # above task_budget: honest work is never
                                  # meant to reach it.
    stall: float = 120.0          # LLM stream inactivity watchdog (resets per chunk)
    shell: float = 120.0          # execute_shell default timeout — generous backstop: the agent's injected timeout overrides it, so this base must not preempt longer work
    pip_install: float = 120.0    # pip_install tool deadline
    save_memory: float = 10.0     # memdb save_turn / drop_context_turns bound
    recall_discriminator: float = 20.0  # pre-turn recall discriminator call


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
    tool_sync_wait: float = 150.0  # THE startup-sync budget — one value, three
                                  # consumers that are the same fact: how long
                                  # ONE mirror may wait on the gateway, how long
                                  # the tool-set line waits before reporting what
                                  # the set has, and how long a wedged reconcile
                                  # pass may hold its guard before the next pass
                                  # abandons it.  It must outlast the gateway's
                                  # OWN two clocks — establishment
                                  # (connect_startup) plus one listing
                                  # (list_tools) — or the line would announce
                                  # "synced" while a server is still legitimately
                                  # connecting; validate() enforces that.  The
                                  # gateway's re-list backoff (5→10→20→40, capped
                                  # at 60 — mcp_gateway/connection.py) is the
                                  # second clock it covers: a first listing that
                                  # times out can still succeed on that retry.
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
    oauth_refresh_skew: float = 60.0   # a token expiring within this counts as
                                       # expired — refreshing after the wire
                                       # deadline is a guaranteed 401
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
class Pacing:
    """Cadences — how often something runs, never how long an await may block.

    Every value here is a period between two events (a poll, a heartbeat, a
    backoff step) or a lifetime after which a cached thing is stale.  They are
    registry-owned like the budgets above: a cadence is a developer decision
    about how the system breathes, and it needs the same one-seat answer.
    """

    heartbeat: float = 1800.0        # autonomous idle heartbeat period
                                     # (``agent.heartbeat_interval`` overrides it)
    schedule_poll: float = 30.0      # cron due-task poll — cron's unit is a
                                     # minute, so a 30 s poll fires within half
                                     # a minute of the due time
    miss_grace: float = 120.0        # a fire whose due time is within this of
                                     # "now" is freshly due; older, it was
                                     # missed while slife was down
    tunnel_probe: float = 1.0        # cadence between sharefile ``__check``
                                     # probes while waiting for a terminal state
    wechat_drain: float = 5.0        # harness → wechat plugin inbox drain
    a2a_drain: float = 1.0           # harness → a2a plugin inbox drain
    mcp_relist_initial: float = 5.0  # first delay before re-listing a server
                                     # that reported no tools
    mcp_relist_max: float = 60.0     # cap on that exponential backoff
    mcp_relist_multiplier: float = 2.0
    mcp_stderr_poll: float = 0.05    # stdio stderr drain cadence
    oauth_poll: float = 5.0          # device-flow token-endpoint poll
    oauth_poll_min: float = 1.0      # floor for that poll — a server answering
                                     # ``interval=0`` must not spin the loop
    media_poll: float = 15.0         # media async-task poll (the providers' own
                                     # examples use 15 s)
    sharefile_health: float = 30.0   # tunnel liveness probe cadence
    typing_refresh: float = 8.0      # wechat typing-indicator refresh
    typing_max_lifetime: float = 300.0  # bound on that keepalive — it must stop
                                     # if the agent never replies
    qr_poll: float = 2.0             # wechat QR status check
    wechat_upstream_poll: float = 3.0   # wechat plugin → WeChat backend poll
    wechat_session_max_age: float = 82800.0  # 23 h — past this the saved
                                     # session is re-logged-in rather than used
    reap_poll: float = 0.05          # process-reap poll between SIGTERM and
                                     # SIGKILL (inside the caller's own deadline)
    warm_delay: float = 5.0          # plugin warm-up grace after the readiness
                                     # handshake, so the first ``tools/list``
                                     # response is flushed first
    timer_max_wait_minutes: float = 1440.0  # ``wait_minutes`` product bound —
                                     # MINUTES, not seconds (24 h)


@dataclass
class Timeouts:
    work: Work = field(default_factory=Work)
    ready: Ready = field(default_factory=Ready)
    grace: Grace = field(default_factory=Grace)
    transport: Transport = field(default_factory=Transport)
    stream: Stream = field(default_factory=Stream)
    storage: Storage = field(default_factory=Storage)
    deliver: Deliver = field(default_factory=Deliver)
    pacing: Pacing = field(default_factory=Pacing)


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
        ("pacing", ts.pacing),
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
    if ts.ready.tool_sync_wait < ts.ready.connect_startup + ts.ready.list_tools:
        # A mirror's wait IS the gateway's own two bounds — bringing the server
        # up and reading one listing, which is all a stateless server needs to
        # answer.  A smaller startup-sync budget would
        # have the line announce "synced" while a server is still legitimately
        # connecting (the old 75 < 120 did exactly that).
        errs.append(
            "invariant: ready.tool_sync_wait >= "
            "ready.connect_startup + ready.list_tools"
        )
    if ts.ready.tunnel_heal < ts.ready.tunnel_read_url:
        # A running child gets at least the patience a fresh start gets, or
        # the monitor would respawn one that was never given time to heal.
        errs.append("invariant: ready.tunnel_heal >= ready.tunnel_read_url")
    if ts.work.stall <= 0:
        errs.append("invariant: work.stall must be > 0")
    if ts.work.task_lifetime < ts.work.task_budget:
        # A task nobody waits on must get at least the patience a task someone
        # waits on gets; a shorter lifetime would abort async work that the
        # synchronous path would still be waiting for.
        errs.append(
            "invariant: work.task_lifetime >= work.task_budget "
            "(an async task must not expire before a waited one would)"
        )
    # Cadences: a period of zero is a busy loop, a backoff whose cap sits below
    # its first step never grows, and a keepalive bound shorter than the
    # refresh it bounds is not a bound.
    for f in fields(ts.pacing):
        if getattr(ts.pacing, f.name) <= 0:
            errs.append(
                f"invariant: pacing.{f.name} must be > 0 — a zero cadence spins"
            )
    if ts.pacing.mcp_relist_max < ts.pacing.mcp_relist_initial:
        errs.append("invariant: pacing.mcp_relist_max >= pacing.mcp_relist_initial")
    if ts.pacing.mcp_relist_multiplier < 1:
        errs.append("invariant: pacing.mcp_relist_multiplier >= 1 (a backoff must grow)")
    if ts.pacing.oauth_poll < ts.pacing.oauth_poll_min:
        errs.append("invariant: pacing.oauth_poll >= pacing.oauth_poll_min")
    if ts.pacing.typing_max_lifetime < ts.pacing.typing_refresh:
        errs.append("invariant: pacing.typing_max_lifetime >= pacing.typing_refresh")
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