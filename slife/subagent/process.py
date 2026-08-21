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
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from slife.platform import terminate_process

# A subagent_name is rendered into the child's system prompt identity line and
# into its log filename — restrict it to a safe identifier so an injected
# parent agent can neither forge a multi-line identity nor traverse out of the
# log dir ("..\\..\\evil").
_SAFE_SUBAGENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# Caps on per-worker bookkeeping — a long-lived worker with many tasks must
# not grow these without bound.
_MAX_TASK_RECORDS = 500
_MAX_ASYNC_RESULTS = 200
_MAX_CANCELLED = 500

if TYPE_CHECKING:
    from slife.config import Config

logger = logging.getLogger(__name__)

# ── Module-level current-manager reference ───────────────────────────
# Set by AgentService.start_subagent() / stop_subagent() so that native
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
        self._async_results: dict[str, str] = {}
        self._ready = asyncio.Event()
        # Local worker task records — rpc_id → {task_id, agent_name, preview,
        # status, result}.  Kept separate from the A2A task store: worker
        # tasks are not mesh tasks.
        self._task_records: dict[str, dict] = {}
        # In-flight worker tasks (sent but not yet resolved).  The child
        # processes tasks serially, so this is both "busy" and "queued".
        self._inflight = 0
        # task_ids the parent has cancelled — the child skips them if still
        # queued; any late response is ignored.
        self._cancelled: set[str] = set()
        # Sync tasks that timed out but the child is still processing — their
        # late result is STORED for get_task_result, not discarded (the tool
        # promises the result remains retrievable).
        self._late_results: set[str] = set()

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
        return self._inflight > 0
    @property
    def queued(self) -> int:
        """Number of tasks sent but not yet resolved (in-flight + queued)."""
        return self._inflight
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
    @property
    def pending_async_ids(self) -> list[str]:
        """task_ids of async tasks sent but not yet completed."""
        return [
            r["task_id"] for r in self._task_records.values()
            if r.get("mode") == "async" and r.get("status") == "pending"
        ]

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
        self._process = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
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
        self._running = True
        # Start _read_stdout as the sole stdout reader — it will set
        # self._ready when it receives the "ready" signal.  Do NOT call
        # _read_one() concurrently: two readline() calls on the same
        # StreamReader cause "readuntil() called while another coroutine
        # is already waiting for incoming data".
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=30.0)
            logger.info("ready name=%s", self._name)
        except asyncio.TimeoutError:
            await self._stop_process()
            raise RuntimeError(f"Subagent '{self._name}' not ready within 30s")
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
        # Mark any in-flight worker tasks as failed and reset the counter.
        for rpc_id in list(self._task_records):
            if self._task_records[rpc_id].get("status") == "pending":
                self._record_update(rpc_id, "failed", "Error: worker stopped")
        self._inflight = 0
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

    async def send_task(self, task: str, timeout: float = 120.0) -> str:
        if not self.is_running or not self._process or not self._process.stdin:
            raise RuntimeError(f"Subagent '{self._name}' not running")
        if not self.is_ready:
            raise RuntimeError(f"Subagent '{self._name}' not ready")
        rpc_id = uuid.uuid4().hex[:12]

        self._record_send(rpc_id, task)
        self._inflight += 1
        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._pending[rpc_id] = future
        req = json.dumps({"jsonrpc":"2.0","method":"worker/send","params":{"task":task},"id":rpc_id}, ensure_ascii=False)
        async with self._stdin_lock:
            self._process.stdin.write((req + "\n").encode()); await self._process.stdin.drain()
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(rpc_id, None)
            # Release the in-flight slot and mark the record failed — otherwise
            # a worker whose task never resolves stays busy forever, every
            # later send auto-queues async, and records pile up.
            # Mark it late-arriving: the child keeps processing it serially,
            # and its eventual response is STORED for get_task_result rather
            # than discarded (the tool promises the result stays retrievable)
            # or mis-routed as a fresh async completion.
            self._late_results.add(rpc_id)
            if len(self._late_results) > _MAX_CANCELLED:
                self._late_results.pop()
            if self._inflight > 0:
                self._inflight -= 1
            self._record_update(rpc_id, "failed", "Error: timed out")
            # Preempt the abandoned task in the child — the worker processes
            # tasks serially, so without this a genuinely stuck task blocks
            # every subsequent one forever (no timeout-driven recovery).
            await self._send_child_cancel(rpc_id)
            raise TimeoutError(f"Task to '{self._name}' timed out after {timeout}s")
        except asyncio.CancelledError:
            self._pending.pop(rpc_id, None)
            self._cancelled.add(rpc_id)
            if len(self._cancelled) > _MAX_CANCELLED:
                self._cancelled.pop()
            if self._inflight > 0:
                self._inflight -= 1
            raise

    async def send_task_async(self, task: str, mode: str = "auto") -> str:
        """Send a task without waiting for the result — returns *rpc_id*.

        *mode* ``"auto"`` (default) auto-pushes the result to the parent
        when the worker completes; ``"poll"`` suppresses the push — the
        caller retrieves the result via :meth:`get_task_result`.
        """
        if not self.is_running or not self._process or not self._process.stdin:
            raise RuntimeError(f"Subagent '{self._name}' not running")
        if not self.is_ready:
            raise RuntimeError(f"Subagent '{self._name}' not ready")
        rpc_id = uuid.uuid4().hex[:12]

        self._record_send(
            rpc_id, task, mode="async-poll" if mode == "poll" else "async",
        )
        self._inflight += 1
        req = json.dumps(
            {"jsonrpc": "2.0", "method": "worker/send",
             "params": {"task": task}, "id": rpc_id},
            ensure_ascii=False,
        )
        async with self._stdin_lock:
            self._process.stdin.write((req + "\n").encode())
            await self._process.stdin.drain()
        logger.debug("subagent_async_send name=%s rpc_id=%s", self._name, rpc_id)
        return rpc_id

    def get_task_result(self, rpc_id: str) -> str | None:
        """Return the result of an async task, or ``None`` if not yet complete."""
        return self._async_results.pop(rpc_id, None)

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

        rec["status"] = "cancelled"
        rec["result"] = "Cancelled by parent"
        self._cancelled.add(task_id)
        if len(self._cancelled) > _MAX_CANCELLED:
            self._cancelled.pop()
        if self._inflight > 0:
            self._inflight -= 1

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

        *mode* is ``"sync"`` (a caller waits) or ``"async"`` (fire-and-forget,
        result auto-pushed).
        """
        self._task_records[rpc_id] = {
            "task_id": rpc_id,
            "agent_name": self._name,
            "preview": task[:200],
            "status": "pending",
            "mode": mode,
            "result": None,
            "created_at": asyncio.get_event_loop().time(),
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
        self._async_results[rpc_id] = result
        if len(self._async_results) > _MAX_ASYNC_RESULTS:
            # dict preserves insertion order — evict the oldest.
            self._async_results.pop(next(iter(self._async_results)))

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
        reader = self._process.stdout
        reader._limit = 10 * 1024 * 1024  # type: ignore[attr-defined]
        try:
            while self._running:
                line = await reader.readline()
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
            # its own timeout.  Also zero the in-flight count: a dead reader
            # means no task can ever resolve, so is_busy would otherwise stay
            # True forever and every later send would be auto-queued async
            # against a worker that can never reply.
            for rpc_id, f in list(self._pending.items()):
                if not f.done():
                    f.set_exception(RuntimeError(
                        f"Subagent '{self._name}' closed before task "
                        f"'{rpc_id}' was resolved"
                    ))
            self._pending.clear()
            self._inflight = 0

    def _dispatch_message(self, msg: dict) -> None:
        """Handle one decoded JSON-RPC line from the worker's stdout.

        Resolves sync waiters, stores async results, honours the ready
        signal, and drops late responses for cancelled tasks.  Raises on a
        structurally-bad message — :meth:`_read_stdout` catches it and keeps
        the reader alive.
        """
        rpc_id = msg.get("id")
        # Late response for a timed-out sync task — store it for retrieval via
        # get_task_result, but do NOT auto-push or flip the record: the caller
        # was already told it timed out.  _inflight was already decremented at
        # the timeout, so don't decrement again.
        if rpc_id and rpc_id in self._late_results:
            self._late_results.discard(rpc_id)
            if "error" in msg:
                self._store_async_result(
                    rpc_id, f"Error: {msg['error'].get('message', 'Unknown')}",
                )
            else:
                self._store_async_result(rpc_id, str(msg.get("result", "")))
            logger.debug(
                "subagent_late_result_stored task=%s", rpc_id,
            )
            return
        # Late response for a cancelled task — discard it (the local waiter
        # was already cleaned up by cancel_task).
        if rpc_id and rpc_id in self._cancelled:
            self._cancelled.discard(rpc_id)
            logger.debug(
                "subagent_cancelled_result_discarded task=%s", rpc_id,
            )
            return
        if rpc_id and rpc_id in self._pending:
            # Sync waiter — resolve the pending future
            f = self._pending.pop(rpc_id, None)
            if self._inflight > 0: self._inflight -= 1
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
            # No synchronous waiter — store for async retrieval
            if self._inflight > 0: self._inflight -= 1
            if "error" in msg:
                err = msg["error"].get("message", "Unknown")
                self._store_async_result(rpc_id, f"Error: {err}")
                self._record_update(rpc_id, "failed", f"Error: {err}")
            else:
                result_text = str(msg.get("result", ""))
                self._store_async_result(rpc_id, result_text)
                self._record_update(rpc_id, "completed", result_text)
            # Notify the manager so it can auto-push the result to the user,
            # unless the task was sent in "poll" mode — the caller retrieves
            # it via get_task_result instead (no redundant push).
            if self._task_records.get(rpc_id, {}).get("mode") != "async-poll":
                self._notify_manager_task_done(rpc_id)
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

    def _notify_manager_task_done(self, task_id: str) -> None:
        """Signal the manager that an async task has completed."""
        mgr = get_manager()
        if mgr is not None and mgr.on_task_complete is not None:
            result_text = self._async_results.get(task_id, "")
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # no event loop running
            loop.create_task(
                mgr.on_task_complete(self._name, task_id, result_text)
            )


class SubagentManager:
    """Manages a collection of SubagentProcess instances."""

    def __init__(self, config: "Config"):
        self._subagents: dict[str, SubagentProcess] = {}
        self._config = config
        sc = config.subagent_config or {}
        self._max = sc.get("max_subagents", 5)
        self._timeout = sc.get("task_timeout", 120)
        # Callback invoked when a subagent task completes:
        #   async def cb(agent_name: str, task_id: str, result: str) -> None
        self.on_task_complete: "Callable | None" = None

    @property
    def count(self) -> int: return sum(1 for p in self._subagents.values() if p.is_running)

    async def spawn(
        self, name: str | None = None,
        context_source: str = "clean", context_messages: list[dict] | None = None,
    ) -> str:
        if self.count >= self._max: raise RuntimeError(f"Max {self._max} subagents reached")
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
        if name in self._subagents and self._subagents[name].is_running: return name
        proc = SubagentProcess(
            name, self._config,
            context_source=context_source, context_messages=context_messages,
        )
        await proc.start(); self._subagents[name] = proc
        return name

    async def send_task(self, agent_name: str, task: str, timeout: float | None = None) -> str:
        if (proc := self._subagents.get(agent_name)) is None:
            raise ValueError(f"Subagent '{agent_name}' not found")
        return await proc.send_task(task, timeout or self._timeout)

    async def send_task_async(
        self, agent_name: str, task: str, mode: str = "auto",
    ) -> str:
        """Send a task without waiting — returns *rpc_id* immediately.

        *mode* ``"auto"`` (default) auto-pushes the result when complete;
        ``"poll"`` suppresses the push (retrieve via get_task_result).
        """
        if (proc := self._subagents.get(agent_name)) is None:
            raise ValueError(f"Subagent '{agent_name}' not found")
        return await proc.send_task_async(task, mode=mode)

    def get_task_result(self, agent_name: str, rpc_id: str) -> str | None:
        """Return the result of an async task, or ``None`` if not yet ready."""
        if (proc := self._subagents.get(agent_name)) is None:
            return None
        return proc.get_task_result(rpc_id)

    def list_tasks(
        self, agent_name: str | None = None, status: str | None = None,
    ) -> list[dict]:
        """List worker task records across all subagents (local store).

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
        return records[:50]

    async def stop(self, agent_name: str) -> bool:
        if (proc := self._subagents.get(agent_name)) is None: return False
        await proc.stop(); del self._subagents[agent_name]
        return True

    async def stop_all(self) -> None:
        if not self._subagents: return
        await asyncio.gather(*(s.stop() for s in list(self._subagents.values())))
        self._subagents.clear()

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
