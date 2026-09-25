"""Subagent (agent worker) process management — worker-scoped JSON-RPC.

Follows ``MCPWrapperProcess`` pattern: asyncio subprocess + pipe bridging.
The stdin/stdout protocol is a local worker control channel (``worker/*``
methods) — deliberately **not** A2A.  A subagent is a local worker, not an
A2A peer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from cachetools import FIFOCache

from slife.platform import terminate_process
from slife.fifoset import FifoSet
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

# A subagent_name is rendered into the child's system prompt identity line and
# into its log filename — restrict it to a safe identifier so an injected
# parent agent can neither forge a multi-line identity nor traverse out of the
# log dir ("..\\..\\evil").
_SAFE_SUBAGENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# Caps on per-worker bookkeeping — a long-lived worker with many tasks must
# not grow these without bound.
_MAX_TASK_RECORDS = 500
_MAX_ASYNC_RESULTS = 200
#: Bound on the two FIFO id sets (cancelled ids, ids whose late reply is still
#: expected).  They are different facts with the same magnitude — "how many
#: recently-finished tasks might still speak" — so they share one cap.
_MAX_RECENT_IDS = 500

if TYPE_CHECKING:
    from slife.config import Config

logger = logging.getLogger(__name__)


def _log_notify_failure(task: asyncio.Task) -> None:
    """Report a failed completion push — the notice would otherwise vanish.

    An async task's result reaches its caller through exactly one channel (the
    manager's push), so a push that dies silently is a task that never reports.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("subagent_notify_failed err=%s", exc, exc_info=exc)


class TaskTimeout(TimeoutError):
    """A worker task did not answer inside its bound.

    It carries the task id because the task is not discarded: the worker is
    preempted (it is serial — a stuck task must not block later ones), and a
    reply that arrives afterwards is stored for ``get_task_result``.  Without
    the id on the exception the caller could not ask for what it was told it
    would not get.
    """

    def __init__(self, message: str, task_id: str):
        super().__init__(message)
        self.task_id = task_id


# ── Module-level current-manager reference ───────────────────────────
# Set by AgentService.start_subagent() / stop_subagent() so that builtin
# tools (Slife.tools.subagent) can look up the live SubagentManager.
_current_manager: "SubagentManager | None" = None


def get_manager() -> "SubagentManager | None":
    """Return the live SubagentManager, or None if subagents are not active."""
    return _current_manager


def set_manager(manager: "SubagentManager") -> None:
    """Set the current SubagentManager (called by AgentService.start_subagent)."""
    global _current_manager
    _current_manager = manager


def clear_manager() -> None:
    """Clear the current SubagentManager (called by AgentService.stop_subagent)."""
    global _current_manager
    _current_manager = None


class SubagentProcess:
    """Single subagent child process with JSON-RPC 2.0 IPC."""

    def __init__(
        self, name: str, config: "Config",
        context_source: str = "clean", context_messages: list[dict] | None = None,
    ):
        import json as _json

        self._name = name
        self._config = config
        self._config_json = _json.dumps(config.to_dict(), ensure_ascii=False)
        # Path of a 0600 temp file carrying _config_json to the child — the
        # config contains resolved plaintext api_keys, which must not ride the
        # process env (visible via /proc/<pid>/environ).
        self._config_file: str | None = None
        self._context_source = context_source
        self._context_messages = context_messages
        self._process: asyncio.subprocess.Process | None = None
        self._running = False
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stdin_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._async_results: FIFOCache[str, str] = FIFOCache(maxsize=_MAX_ASYNC_RESULTS)
        self._ready = asyncio.Event()
        # Local worker task records — rpc_id → {task_id, agent_name, preview,
        # status, result}.  Kept separate from the A2A task store: worker
        # tasks are not mesh tasks.
        self._task_records: dict[str, dict] = {}
        # Tasks whose id the parent is still tracking as unresolved — "sent to
        # the child, no reply accounted for yet".  This single set IS the
        # busy/queued count (see :attr:`is_busy`): it used to be a hand-kept
        # integer incremented in two places and decremented in six, and the two
        # disagreed as soon as one path forgot (an evicted record, a failed
        # stdin write) — a worker that stayed "busy" forever, so every later
        # send auto-queued as async against an idle child.
        self._awaiting: set[str] = set()
        # Expiry watchdogs for async tasks, rpc_id → task.  An async task has
        # no waiting caller, so ``send_task``'s own timeout does not apply and
        # nothing else would ever notice a task that never finishes.
        self._watchdogs: dict[str, asyncio.Task] = {}
        # task_ids the parent has cancelled — the child skips them if still
        # queued; any late response is ignored.  Over-cap eviction drops the
        # OLDEST id (FIFO), not an arbitrary set member (F10).
        self._cancelled: FifoSet = FifoSet()
        # Sync tasks that timed out but the child is still processing — their
        # late result is STORED for get_task_result, not discarded (the tool
        # promises the result remains retrievable).
        self._late_results: FifoSet = FifoSet()

    @property
    def name(self) -> str: return self._name
    @property
    def pid(self) -> int | None: return self._process.pid if self._process else None
    @property
    def is_running(self) -> bool: return self._running and self._process is not None and self._process.returncode is None
    @property
    def is_ready(self) -> bool: return self._ready.is_set()
    @property
    def is_busy(self) -> bool:
        """True while a task is in flight (the child processes tasks serially)."""
        return bool(self._awaiting)
    @property
    def queued(self) -> int:
        """Number of tasks sent but not yet resolved (in-flight + queued)."""
        return len(self._awaiting)
    @property
    def context_source(self) -> str:
        """How this worker's context was built: ``"clean"`` or ``"cloned"``."""
        return self._context_source
    @property
    def pending_async_count(self) -> int:
        """Number of async tasks sent but not yet completed."""
        return sum(
            1 for r in self._task_records.values()
            if r.get("mode") == "async" and r.get("status") == "pending"
        )

    async def start(self) -> None:
        if self._running: return
        cmd = [sys.executable, "-m", "slife.subagent.headless"]
        logger.info("spawn name=%s", self._name)
        env = dict(os.environ)
        env["SLIFE_SUBAGENT_NAME"] = self._name
        env["SLIFE_SUBAGENT_CREATED_AT"] = (
            datetime.now().astimezone().replace(microsecond=0).isoformat()
        )
        # The config carries resolved plaintext api_keys — hand it over via a
        # 0600 temp file (SLIFE_CONFIG_FILE), never the process env which is
        # visible via /proc/<pid>/environ.
        if self._config_json:
            fd, path = tempfile.mkstemp(
                prefix="slife_subagent_", suffix=".json",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(self._config_json)
                os.chmod(path, 0o600)
            except Exception:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                raise
            self._config_file = path
            env["SLIFE_CONFIG_FILE"] = path
        env["SLIFE_SUBAGENT_CONTEXT"] = self._context_source
        # The a2a plugin port (SLIFE_A2A_PORT) is inherited from os.environ
        # above — the subagent reuses the main agent's mesh channel.  The
        # a2a plugin owns the main agent's identity.
        # Subagents connect to the main agent's shared plugin servers
        # (MCP / memdb / wechat) via inherited ports — no isolation.
        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
            self._running = True
            # Start the stdout/stderr readers BEFORE writing the (potentially
            # large) cloned context to stdin: the child only reads stdin after
            # finishing its own boot (plugin connects etc.), meanwhile stderr is
            # at DEBUG.  With no reader yet, a build-up of >~64 KB of stderr
            # blocks the child, which then never reads stdin, which blocks our
            # drain() below — both stuck until the registry's ready.spawn timeout
            # kills it.  (Same class as the prior stderr relay pipe-wedge.)  Do NOT call
            # _read_one() concurrently: two readline() calls on the same
            # StreamReader cause "readuntil() called while another coroutine
            # is already waiting for incoming data".
            self._stdout_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._read_stderr())

            # Cloned context rides the stdin JSON-RPC channel (env is limited to
            # ~32 KB on Windows — too small for a conversation).
            proc = self._process
            if self._context_messages and proc is not None and proc.stdin is not None:
                ctx_msg = json.dumps(
                    {"jsonrpc": "2.0", "method": "context",
                     "params": {"messages": self._context_messages}, "id": None},
                    ensure_ascii=False,
                ) + "\n"
                proc.stdin.write(ctx_msg.encode())
                await proc.stdin.drain()
        except BaseException:
            # A spawn failure (deleted venv, AV block) or a drain failure
            # (child died mid-boot) must NOT leak the 0600 config file
            # carrying plaintext api_keys, nor orphan a spawned child.
            if self._process is not None and self._running:
                await self._stop_process()
            else:
                self._cleanup_config_file()
            raise
        ready_spawn = _timeouts.timeouts.ready.spawn  # call-time lookup
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=ready_spawn)
            logger.info("ready name=%s", self._name)
        except asyncio.TimeoutError:
            await self._stop_process()
            raise RuntimeError(
                f"Subagent '{self._name}' not ready within {ready_spawn:g}s"
            )
        except Exception:
            await self._stop_process()
            raise

    async def stop(self) -> None:
        await self._stop_process()

    async def _stop_process(self) -> None:
        if not self._process or not self._running: return
        logger.info("stop name=%s pid=%s", self._name, self._process.pid)
        for f in self._pending.values():
            if not f.done(): f.set_exception(RuntimeError(f"Subagent '{self._name}' stopped"))
        self._pending.clear()
        self._async_results.clear()
        self._abandon_pending("worker stopped")
        stdout_task = self._stdout_task
        stderr_task = self._stderr_task
        for t in (stdout_task, stderr_task):
            if t and not t.done(): t.cancel()
        # Send JSON-RPC shutdown before terminating
        if self._process.stdin and self._process.returncode is None:
            try:
                shutdown = json.dumps({"jsonrpc":"2.0","method":"shutdown","id":None}) + "\n"
                self._process.stdin.write(shutdown.encode()); await self._process.stdin.drain()
            except Exception:
                logger.debug("shutdown_send_failed name=%s", self._name, exc_info=True)
        await terminate_process(self._process, label=f"subagent:{self._name}")
        self._running = False; self._process = None
        # Await both reader tasks
        for t in (stdout_task, stderr_task):
            if t and not t.done():
                try: await t
                except (asyncio.CancelledError, Exception):
                    logger.debug("reader_cancel name=%s", self._name, exc_info=True)
        self._cleanup_config_file()

    def _abandon_pending(self, reason: str) -> None:
        """Close out every task this worker will never answer.

        Called when the child is gone — a deliberate stop, or a reader that hit
        EOF because the child died on its own.  Two things must not be left
        behind.  Each still-pending record would read "pending" forever, so it
        is marked failed.  And each *async* task promised an auto-push ("the
        result will be delivered automatically") that can now never arrive, so
        the notice is sent as a failure instead of the caller waiting on
        silence — exactly the silence the design forbids for a reported task.

        Idempotent: a record is only touched while it is still ``pending``, so
        the second caller (the reader's ``finally`` racing an explicit stop)
        does nothing.
        """
        abandoned = [
            rpc_id for rpc_id, rec in self._task_records.items()
            if rec.get("status") == "pending"
        ]
        for rpc_id in abandoned:
            text = f"Error: {reason}"
            self._record_update(rpc_id, "failed", text)
            rec = self._task_records.get(rpc_id) or {}
            if rec.get("mode") != "async-poll":
                self._notify_manager_task_done(rpc_id, text)
        self._awaiting.clear()
        for t in self._watchdogs.values():
            if not t.done(): t.cancel()
        self._watchdogs.clear()
        if abandoned:
            logger.info(
                "subagent_tasks_abandoned name=%s count=%d reason=%s",
                self._name, len(abandoned), reason,
            )

    def _cleanup_config_file(self) -> None:
        """Delete the 0600 temp config file handed to the child."""
        if self._config_file is not None:
            try:
                os.unlink(self._config_file)
            except OSError:
                pass
            self._config_file = None

    async def _send_child_cancel(self, task_id: str) -> None:
        """Best-effort: tell the child to skip/cancel a task still queued or
        running (worker/cancel is a notification — no response expected)."""
        try:
            if self._process is not None and self._process.stdin is not None:
                req = json.dumps(
                    {"jsonrpc": "2.0", "method": "worker/cancel",
                     "params": {"task_id": task_id}, "id": None},
                ) + "\n"
                async with self._stdin_lock:
                    self._process.stdin.write(req.encode())
                    await self._process.stdin.drain()
        except Exception:
            pass

    async def send_notification(
        self, method: str, params: dict | None = None,
    ) -> None:
        """Send a JSON-RPC notification to the worker (no response expected).

        Best-effort — a dead or not-yet-ready worker is skipped silently.
        Used to tell workers about parent-side events, e.g. an MCP wrapper
        restart so they reconnect their shared plugin client.
        """
        if not self.is_running or not self._process or not self._process.stdin:
            return
        req = json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": None},
            ensure_ascii=False,
        )
        try:
            async with self._stdin_lock:
                self._process.stdin.write((req + "\n").encode())
                await self._process.stdin.drain()
        except Exception:
            logger.debug(
                "subagent_notify_send_failed name=%s method=%s",
                self._name, method, exc_info=True,
            )

    async def _send_request(
        self, task: str, mode: str,
    ) -> tuple[str, asyncio.Future[str] | None]:
        """Record one task, write ``worker/send``, and return ``(rpc_id, future)``.

        The one place a task leaves for the child, so the bookkeeping a task's
        life depends on — the record, the await-slot, the future — is registered
        *before* the write and unwound if the write fails.  The registration is
        also why the write is last: the child can answer during ``drain()``, and
        a reply that arrives before the parent knows what it sent would be
        dropped as unknown.

        *mode* is the record's vocabulary — ``"sync"``, ``"async"`` or
        ``"async-poll"``.  A future comes back only for ``"sync"``.
        """
        if not self.is_running or not self._process or not self._process.stdin:
            raise RuntimeError(f"Subagent '{self._name}' not running")
        if not self.is_ready:
            raise RuntimeError(f"Subagent '{self._name}' not ready")
        rpc_id = uuid.uuid4().hex[:12]
        self._record_send(rpc_id, task, mode=mode)
        self._awaiting.add(rpc_id)
        future: asyncio.Future[str] | None = None
        if mode == "sync":
            future = asyncio.get_running_loop().create_future()
            self._pending[rpc_id] = future
        req = json.dumps(
            {"jsonrpc": "2.0", "method": "worker/send",
             "params": {"task": task}, "id": rpc_id},
            ensure_ascii=False,
        )
        try:
            async with self._stdin_lock:
                self._process.stdin.write((req + "\n").encode())
                await self._process.stdin.drain()
        except BaseException:
            # A dead/closed pipe must not leave the task behind: the record
            # would read "pending" forever and the await-slot would keep the
            # worker looking busy with nothing in flight.
            self._pending.pop(rpc_id, None)
            self._awaiting.discard(rpc_id)
            self._record_update(rpc_id, "failed", "Error: send failed")
            raise
        return rpc_id, future

    async def send_task(self, task: str, timeout: float | None = None) -> str:
        """Send a task and wait for its result, up to *timeout*.

        On timeout the task is **preempted** in the child (a serial worker
        cannot afford to be wedged by one task) and the late reply is kept for
        :meth:`get_task_result` — the raised :class:`TaskTimeout` carries the id
        that lookup needs.
        """
        if timeout is None:
            timeout = _timeouts.timeouts.work.task_budget  # call-time lookup
        rpc_id, future = await self._send_request(task, "sync")
        assert future is not None
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rpc_id, None)
            # Release the await-slot — otherwise a worker whose task never
            # resolves stays busy forever, every later send auto-queues async,
            # and records pile up.  Mark it late-arriving: the child keeps
            # processing it serially, and its eventual response is STORED for
            # get_task_result rather than discarded (the tool promises the
            # result stays retrievable) or mis-routed as a fresh completion.
            self._late_results.add(rpc_id)
            self._late_results.evict_to(_MAX_RECENT_IDS)
            self._awaiting.discard(rpc_id)
            self._record_update(rpc_id, "failed", "Error: timed out")
            await self._send_child_cancel(rpc_id)
            raise TaskTimeout(
                f"Task to '{self._name}' timed out after {timeout}s", rpc_id,
            ) from None
        except asyncio.CancelledError:
            self._pending.pop(rpc_id, None)
            self._cancelled.add(rpc_id)
            self._cancelled.evict_to(_MAX_RECENT_IDS)
            self._awaiting.discard(rpc_id)
            # Preempt it here too.  The caller is gone (the turn was cancelled
            # under it — an Esc), which is the same situation as a timeout: the
            # child would otherwise keep working on a task nobody wants, and
            # being serial it would hold every later task behind it.
            await self._send_child_cancel(rpc_id)
            raise

    async def send_task_async(self, task: str, mode: str = "auto") -> str:
        """Send a task without waiting for the result — returns *rpc_id*.

        *mode* ``"auto"`` (default) auto-pushes the result to the parent
        when the worker completes — it ALSO stays retrievable via
        :meth:`get_task_result`; ``"poll"`` suppresses the push — the
        caller retrieves the result via :meth:`get_task_result`.

        Nobody awaits an async task, so ``send_task``'s timeout cannot apply;
        the expiry watchdog started here is what keeps "no caller" from meaning
        "no bound at all".  It is a wedge backstop, not a caller's budget (see
        ``work.task_lifetime``): honest work is never meant to reach it.
        """
        rpc_id, _ = await self._send_request(
            task, "async-poll" if mode == "poll" else "async",
        )
        self._watchdogs[rpc_id] = asyncio.create_task(self._expire(rpc_id))
        logger.debug("subagent_async_send name=%s rpc_id=%s", self._name, rpc_id)
        return rpc_id

    async def _expire(self, rpc_id: str) -> None:
        """Watchdog for one async task: give up on it, and say so.

        Runs the same remedy the sync path runs on timeout — mark the task
        failed, preempt it in the child, and expect the reply as a late result
        (stored, not re-pushed) — plus the one thing only an async task needs:
        the caller is *told*, because nothing else ever would.  A pushed failure
        is the difference between "this task cannot finish" and a caller waiting
        forever on an auto-push that will not come.
        """
        lifetime = _timeouts.timeouts.work.task_lifetime  # call-time lookup
        try:
            await asyncio.sleep(lifetime)
        except asyncio.CancelledError:
            raise  # resolved (or stopped) — the task answered after all
        finally:
            # This watchdog is finished either way; when the task answered
            # first, the resolver had already forgotten it.
            self._watchdogs.pop(rpc_id, None)
        rec = self._task_records.get(rpc_id)
        if rec is None or rec.get("status") != "pending":
            return  # answered (or cancelled) while the watchdog slept
        text = f"Error: task timed out after {lifetime:g}s without a reply"
        self._late_results.add(rpc_id)
        self._late_results.evict_to(_MAX_RECENT_IDS)
        self._awaiting.discard(rpc_id)
        self._record_update(rpc_id, "failed", text)
        if rec.get("mode") != "async-poll":
            self._notify_manager_task_done(rpc_id, text)
        await self._send_child_cancel(rpc_id)
        logger.warning(
            "subagent_async_task_expired name=%s task=%s lifetime=%g",
            self._name, rpc_id, lifetime,
        )

    def _resolve_watchdog(self, rpc_id: str) -> None:
        """Stop watching a task that has resolved, one way or another."""
        task = self._watchdogs.pop(rpc_id, None)
        if task is not None and not task.done():
            task.cancel()

    def get_task_result(self, rpc_id: str) -> tuple[str, str | None]:
        """Return ``(state, result)`` for a worker task.

        *state* is the task record's status — ``pending``, ``completed``,
        ``failed`` or ``cancelled`` — or ``unknown`` when no record was ever
        made for *rpc_id* (a mistyped id, or one the 500-record cap evicted).
        Keeping the two apart is the point: the previous contract returned
        ``None`` for every one of those cases and the caller rendered them all
        as "pending", so a completed task reported "pending" the second time it
        was polled, a cancelled one reported it forever, and a typo looked like
        work in progress.

        Reading does not consume: the result stays retrievable for the task's
        whole record lifetime, so an auto-pushed result can still be polled
        afterwards (which the tool's own description promises).
        """
        rec = self._task_records.get(rpc_id)
        stored = self._async_results.get(rpc_id)
        if rec is None:
            # No record: either it was evicted (a result may outlive it) or the
            # id was never ours.
            return ("completed", stored) if stored is not None else ("unknown", None)
        status = rec.get("status", "unknown")
        if status == "pending":
            return "pending", None
        return status, stored if stored is not None else (rec.get("result") or "")

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a worker task — drops it if queued, preempts it if running.

        Cleans up the local waiter (sync future / async result), marks the
        task record ``cancelled``, and notifies the child (``worker/cancel``),
        which drops a still-queued task or stops the running agent loop at the
        next safe point — the same Esc mechanism as the main agent.  Any late
        result is discarded by :meth:`_read_stdout` (the id is in
        :attr:`_cancelled`).
        """
        rec = self._task_records.get(task_id)
        if rec is None:
            return False
        if rec.get("status") != "pending":
            return False

        # Cancel a synchronous waiter, if one is still waiting.
        fut = self._pending.pop(task_id, None)
        if fut is not None and not fut.done():
            fut.set_exception(RuntimeError(f"Task '{task_id}' cancelled"))
        # Drop any stored async result.
        self._async_results.pop(task_id, None)
        self._resolve_watchdog(task_id)

        rec["status"] = "cancelled"
        rec["result"] = "Cancelled by parent"
        self._cancelled.add(task_id)
        self._cancelled.evict_to(_MAX_RECENT_IDS)
        self._awaiting.discard(task_id)

        # Notify the child so it skips a still-queued task (best-effort).
        await self._send_child_cancel(task_id)
        return True

    def list_task_records(self) -> list[dict]:
        """Return this worker's task records, newest first."""
        recs = list(self._task_records.values())
        recs.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return recs

    def _record_send(self, rpc_id: str, task: str, mode: str = "sync") -> None:
        """Record a newly-sent worker task (status pending).

        *mode* is ``"sync"`` (a caller waits), ``"async"`` (fire-and-forget,
        result auto-pushed) or ``"async-poll"`` (fire-and-forget, no push).
        """
        self._task_records[rpc_id] = {
            "task_id": rpc_id,
            "agent_name": self._name,
            "preview": task[:200],
            "status": "pending",
            "mode": mode,
            "result": None,
            # Monotonic wall time, not the loop's clock: a record is written
            # from the send path only, but keeping the ordering clock free of
            # the event loop means a record can also be made (or inspected)
            # outside one.
            "created_at": time.monotonic(),
        }
        if len(self._task_records) > _MAX_TASK_RECORDS:
            # Drop the oldest record — a long-lived worker with many tasks
            # must not grow the store without bound.
            oldest = min(
                self._task_records,
                key=lambda k: self._task_records[k].get("created_at", 0),
            )
            self._task_records.pop(oldest, None)

    def _store_async_result(self, rpc_id: str, result: str) -> None:
        """Store a worker result for get_task_result, bounded."""
        # FIFOCache evicts the oldest-inserted result at maxsize.
        self._async_results[rpc_id] = result

    def _record_update(self, rpc_id: str, status: str, result: str | None) -> None:
        """Update a worker task record on completion / failure."""
        rec = self._task_records.get(rpc_id)
        if rec is None:
            return
        rec["status"] = status
        if result is not None:
            rec["result"] = result[:2000]

    async def _read_stdout(self) -> None:
        if not self._process or not self._process.stdout: return
        from slife.logfmt import PROTOCOL_LINE_LIMIT, discard_overlong_line
        reader = self._process.stdout
        # One stdout line can legitimately be a many-MB worker result — raise
        # the StreamReader cap accordingly.  A line beyond even that is
        # discarded (tail and all) rather than killing the reader: a dead
        # reader strands every _pending sync future and the worker's task
        # sits "pending" forever (the same class as the stderr pipe-wedge).
        try:
            # `_limit` is private on asyncio.StreamReader — read defensively
            # via getattr (avoids the read-side attribute warning) and raise
            # it on the write.
            reader._limit = max(  # type: ignore[attr-defined]
                int(getattr(reader, "_limit", 0)), PROTOCOL_LINE_LIMIT,
            )
        except (AttributeError, TypeError, ValueError):
            pass
        try:
            while self._running:
                try:
                    line = await reader.readline()
                except ValueError:
                    # LimitOverrunError — a single over-long line.  Discard
                    # its remainder and keep reading; never die here.
                    dropped = await discard_overlong_line(reader)
                    logger.warning(
                        "subagent_stdout_line_overlong_discarded "
                        "name=%s min_bytes=%d", self._name, dropped,
                    )
                    continue
                if not line: break
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                try:
                    self._dispatch_message(msg)
                except Exception:
                    # A malformed/unhandled message must not kill the reader —
                    # that would strand the _pending sync futures until
                    # send_task's own timeout. Log and move on.
                    logger.warning(
                        "subagent_msg_error name=%s line=%.200s",
                        self._name, line, exc_info=True,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "subagent_stdout_read_error name=%s", self._name, exc_info=True,
            )
        finally:
            # The reader is done (stop / EOF / error) — resolve any leftover
            # sync waiters so send_task fails fast instead of hanging until
            # its own timeout, and abandon everything else this worker will
            # never answer (a silent pending record is the same defect as a
            # silent pending future: a caller waiting on a promise nobody can
            # keep).
            for rpc_id, f in list(self._pending.items()):
                if not f.done():
                    f.set_exception(RuntimeError(
                        f"Subagent '{self._name}' closed before task "
                        f"'{rpc_id}' was resolved"
                    ))
            self._pending.clear()
            self._abandon_pending("worker exited before replying")
            # The reader ends when the child's stdout closes — i.e. the child is
            # gone: an orderly stop, or (the case nothing else covers) a crash
            # of its own.  Mark it and drop the temp config file HERE, not only
            # in _stop_process: a worker that dies on its own is never stopped
            # by anyone, so its 0600 file — carrying the parent's plaintext
            # api_keys — would outlive it on disk, and the registry entry would
            # read as a live worker to everything that only checks the dict.
            self._running = False
            self._cleanup_config_file()

    def _dispatch_message(self, msg: dict) -> None:
        """Handle one decoded JSON-RPC line from the worker's stdout.

        Resolves sync waiters, stores async results, honours the ready
        signal, and drops late responses for cancelled tasks.  Raises on a
        structurally-bad message — :meth:`_read_stdout` catches it and keeps
        the reader alive.
        """
        rpc_id = msg.get("id")
        # Late response for a timed-out sync task — store it for retrieval via
        # get_task_result, but do NOT auto-push: the caller was already told it
        # timed out, and a push would double-announce a task it believes failed.
        # The record IS un-failed: the task did finish, and a log that still
        # says "timed out" while the result sits retrievable makes the two
        # accounts of one task contradict each other.
        if rpc_id and rpc_id in self._late_results:
            self._late_results.discard(rpc_id)
            self._resolve_watchdog(rpc_id)
            if "error" in msg:
                err = msg["error"].get("message", "Unknown")
                self._store_async_result(rpc_id, f"Error: {err}")
                self._record_update(rpc_id, "failed", f"Error: {err}")
            else:
                result_text = str(msg.get("result", ""))
                self._store_async_result(rpc_id, result_text)
                self._record_update(rpc_id, "completed", result_text)
            logger.debug(
                "subagent_late_result_stored task=%s", rpc_id,
            )
            return
        # Late response for a cancelled task — discard it (the local waiter
        # was already cleaned up by cancel_task).
        if rpc_id and rpc_id in self._cancelled:
            self._cancelled.discard(rpc_id)
            self._resolve_watchdog(rpc_id)
            logger.debug(
                "subagent_cancelled_result_discarded task=%s", rpc_id,
            )
            return
        if rpc_id and rpc_id in self._pending:
            # Sync waiter — resolve the pending future
            f = self._pending.pop(rpc_id, None)
            self._awaiting.discard(rpc_id)
            if not f or f.done(): return
            if "error" in msg:
                err = msg["error"].get("message", "Unknown")
                f.set_exception(RuntimeError(err))
                self._record_update(rpc_id, "failed", f"Error: {err}")
            else:
                result_text = str(msg.get("result", ""))
                f.set_result(result_text)
                self._record_update(rpc_id, "completed", result_text)
        elif rpc_id:
            # No synchronous waiter — store for async retrieval IF the task
            # is one we sent and have not resolved yet.  A response for an
            # unknown id (a buggy/duplicate worker line, or a record the
            # 500-record cap evicted) must not mutate records or the auto-push
            # channel: it would resurrect a cancelled/completed record and
            # double-push into the inbox.  It IS booked as answered, though —
            # that is a fact about the id, not about the record.
            self._resolve_watchdog(rpc_id)
            self._awaiting.discard(rpc_id)
            rec = self._task_records.get(rpc_id)
            if rec is None or rec.get("status") != "pending":
                logger.warning(
                    "subagent_unknown_or_stale_response task=%s status=%s "
                    "name=%s — ignored",
                    rpc_id, (rec or {}).get("status", "unknown"), self._name,
                )
                return
            if "error" in msg:
                err = msg["error"].get("message", "Unknown")
                result_text = f"Error: {err}"
                self._store_async_result(rpc_id, result_text)
                self._record_update(rpc_id, "failed", result_text)
            else:
                result_text = str(msg.get("result", ""))
                self._store_async_result(rpc_id, result_text)
                self._record_update(rpc_id, "completed", result_text)
            # Notify the manager so it can auto-push the result to the user,
            # unless the task was sent in "poll" mode — the caller retrieves
            # it via get_task_result instead (no redundant push).  The store
            # keeps the result in BOTH modes, so an auto task is pollable too.
            if rec.get("mode") != "async-poll":
                self._notify_manager_task_done(rpc_id, result_text)
        elif rpc_id is None:
            # JSON-RPC notification or ready signal (no id)
            if isinstance(msg.get("result"), dict) and msg["result"].get("ready"):
                self._ready.set()
            elif "method" in msg:
                method = msg["method"]
                params = (
                    msg.get("params", {})
                    if isinstance(msg.get("params"), dict) else {}
                )
                task_id = params.get("task_id", "")
                if method == "worker/complete":
                    # The result was already handled by the JSON-RPC response
                    # path above (sync waiter resolved; async result stored +
                    # manager notified).  The notification only carries a
                    # task_id — no result — so nothing further to record.
                    logger.debug(
                        "subagent_complete name=%s task=%s",
                        self._name, task_id,
                    )
                elif method == "worker/progress":
                    logger.debug(
                        "subagent_progress name=%s task=%s pct=%s",
                        self._name, task_id,
                        params.get("pct", "?"),
                    )

    async def _read_stderr(self) -> None:
        from slife.logfmt import drain_stderr
        await drain_stderr(
            self._process, f"subagent:{self._name}", logger,
            running_check=lambda: self._running,
        )

    def _notify_manager_task_done(self, task_id: str, result_text: str) -> None:
        """Signal the manager that an async task has settled.

        *result_text* (the result, or the failure text) is passed in directly
        rather than re-read from :attr:`_async_results`: reading the store no
        longer consumes it, but the push must deliver what *this* transition
        decided — a task that just failed must not push a stale success.

        Fire-and-forget: the push is a notice, and the caller of this method is
        the stdout reader, which must never block on the manager's inbox.
        """
        mgr = get_manager()
        if mgr is None or mgr.on_task_complete is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "subagent_notify_no_loop name=%s task=%s", self._name, task_id,
            )
            return
        task = loop.create_task(
            mgr.on_task_complete(self._name, task_id, result_text)
        )
        # A notice nobody sent is worse than a noise line: the manager's push
        # is the only way an async task is ever heard from, so its failure is
        # logged rather than left to asyncio's "never retrieved" silence.
        task.add_done_callback(_log_notify_failure)


class SubagentManager:
    """Manages a collection of SubagentProcess instances.

    The registry is the object graph AND the lifetime: a worker that leaves it
    is stopped, and one that dies on its own is swept (see :meth:`_prune_dead`).
    ``spawn``/``stop``/``stop_all`` all mutate it under one lock — a spawn and a
    stop of the same name can otherwise interleave and leave two children for
    one name, one of them unreachable.
    """

    def __init__(self, config: "Config"):
        self._subagents: dict[str, SubagentProcess] = {}
        self._config = config
        sc = config.subagent_config or {}
        self._max = sc.get("max_subagents", 5)
        #: Serialises registry mutation.  Held across ``SubagentProcess.start``
        #: (and a stop's teardown), so a burst of parallel spawns boots one
        #: worker at a time — the price of the cap being an exact count and of
        #: "reuse a running name" meaning what it says.
        self._registry_lock = asyncio.Lock()
        # Callback invoked when a subagent task completes:
        #   async def cb(agent_name: str, task_id: str, result: str) -> None
        self.on_task_complete: "Callable | None" = None

    @property
    def count(self) -> int: return sum(1 for p in self._subagents.values() if p.is_running)

    def spawned_running(self, name: str) -> bool:
        """True if a worker with *name* is currently running (spawn() would
        reuse it rather than create a new one).  Lets tools report reuse."""
        proc = self._subagents.get(name)
        return proc is not None and proc.is_running

    def _prune_dead(self) -> list[str]:
        """Drop workers that exited on their own; return their names.

        ``stop()`` is the only other deleter, and nothing calls it for a worker
        that died by itself (a crash, an OOM kill, a provider client that took
        the process down) — so without this the entry, and the task store it
        carries, outlive the process for the life of the parent.  ``spawn`` is
        the growth vector and therefore where the sweep runs.
        """
        dead = [n for n, p in self._subagents.items() if not p.is_running]
        for n in dead:
            self._subagents.pop(n, None)
        if dead:
            logger.info("subagent_registry_pruned_dead names=%s", ",".join(dead))
        return dead

    async def spawn(
        self, name: str | None = None,
        context_source: str = "clean", context_messages: list[dict] | None = None,
    ) -> str:
        # The worker's name is its identity — never auto-generate an id.
        if not name or not name.strip():
            raise ValueError("subagent_name is required")
        name = name.strip()
        if not _SAFE_SUBAGENT_NAME.match(name):
            # The name lands in the child's system prompt ("You are {name}")
            # and its log filename — a bare `.strip()` let an injected parent
            # forge the identity line or traverse out of the log dir ("..\..").
            raise ValueError(
                "subagent_name must be a safe identifier "
                "(letters/digits/_/. with a letter/digit start, max 64 chars) — "
                f"got {name!r}"
            )
        # Everything from here to registration is atomic.  The model may emit
        # two spawn calls for one name in a single assistant message and the
        # loop runs tool calls concurrently (`asyncio.gather`), so without this
        # both would pass the reuse check and the cap check, both would start a
        # child, and the second would overwrite the first's registry entry —
        # leaving a live worker that nothing can list, send to, or stop.
        async with self._registry_lock:
            self._prune_dead()
            # Reuse a running worker BEFORE the cap check — spawn() is
            # idempotent (the spawned_running() contract), so re-invoking a
            # name that is already running at the cap must hand back the
            # worker, not raise.  The worker keeps the context it was started
            # with; callers report that, never what they asked for.
            if name in self._subagents and self._subagents[name].is_running:
                return name
            if self.count >= self._max:
                raise RuntimeError(f"Max {self._max} subagents reached")
            proc = SubagentProcess(
                name, self._config,
                context_source=context_source, context_messages=context_messages,
            )
            await proc.start()
            self._subagents[name] = proc
            return name

    async def send_task(self, agent_name: str, task: str, timeout: float | None = None) -> str:
        if (proc := self._subagents.get(agent_name)) is None:
            raise ValueError(f"Subagent '{agent_name}' not found")
        return await proc.send_task(task, timeout)

    async def send_task_async(
        self, agent_name: str, task: str, mode: str = "auto",
    ) -> str:
        """Send a task without waiting — returns *rpc_id* immediately.

        *mode* ``"auto"`` (default) auto-pushes the result when complete
        (the result stays retrievable via get_task_result); ``"poll"``
        suppresses the push (retrieve via get_task_result).
        """
        if (proc := self._subagents.get(agent_name)) is None:
            raise ValueError(f"Subagent '{agent_name}' not found")
        return await proc.send_task_async(task, mode=mode)

    def get_task_result(self, agent_name: str, rpc_id: str) -> tuple[str, str | None]:
        """Return ``(state, result)`` for a task — see
        :meth:`SubagentProcess.get_task_result`.

        ``("unknown", None)`` covers an unknown worker as well as an unknown
        task id: neither can be answered, and neither is "not ready yet".
        """
        if (proc := self._subagents.get(agent_name)) is None:
            return "unknown", None
        return proc.get_task_result(rpc_id)

    def list_tasks(
        self, agent_name: str | None = None, status: str | None = None,
        limit: int = 50,
    ) -> tuple[list[dict], int]:
        """List worker task records across all subagents (local store).

        Returns ``(records, total)`` — *total* is the pre-limit count, so a
        caller can say how much it is not showing instead of presenting a
        truncated list as the whole of it.

        Not an A2A listing — worker tasks are tracked locally in each
        :class:`SubagentProcess`, independent of the mesh task store.
        """
        records: list[dict] = []
        for aid, proc in self._subagents.items():
            if agent_name is not None and aid != agent_name:
                continue
            records.extend(proc.list_task_records())
        if status is not None:
            records = [r for r in records if r.get("status") == status]
        records.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return records[:limit], len(records)

    async def stop(self, agent_name: str) -> bool:
        async with self._registry_lock:
            proc = self._subagents.get(agent_name)
            if proc is None:
                return False
            await proc.stop()
            del self._subagents[agent_name]
            return True

    async def stop_all(self) -> None:
        """Stop every worker, concurrently.

        Each name goes through :meth:`stop`, so an entry leaves the registry
        only once its process is gone — the registry stays the list of workers
        that might still be alive, which is what the crash-path sweep
        (``kill_child_processes``) reads to find children to kill.
        """
        if not self._subagents:
            return
        await asyncio.gather(
            *(self.stop(name) for name in list(self._subagents))
        )

    async def broadcast(
        self, method: str, params: dict | None = None,
    ) -> None:
        """Send a notification to every live subagent worker (best-effort)."""
        if not self._subagents:
            return
        await asyncio.gather(
            *(
                proc.send_notification(method, params)
                for proc in list(self._subagents.values())
            )
        )

    def list(self) -> list[str]:
        return [n for n, p in self._subagents.items() if p.is_running]

    def get(self, agent_name: str) -> SubagentProcess | None:
        return self._subagents.get(agent_name)

    def is_busy(self, agent_name: str) -> bool:
        """True if *agent_name* has a task in flight (serially processed)."""
        proc = self._subagents.get(agent_name)
        return bool(proc and proc.is_busy)

    def queued_count(self, agent_name: str) -> int:
        """Return the number of in-flight/queued tasks for *agent_name*."""
        proc = self._subagents.get(agent_name)
        return proc.queued if proc else 0

    async def cancel_task(self, agent_name: str, task_id: str) -> bool:
        """Cancel a pending/queued worker task on *agent_name* (best-effort)."""
        proc = self._subagents.get(agent_name)
        if proc is None:
            return False
        return await proc.cancel_task(task_id)
