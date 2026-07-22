"""Subagent process management — JSON-RPC 2.0 over stdin/stdout.

Follows ``MCPWrapperProcess`` pattern: asyncio subprocess + pipe bridging.
Protocol is JSON-RPC 2.0 per A2A specification §9.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from slife.platform import terminate_process

if TYPE_CHECKING:
    from slife.config import Config

logger = logging.getLogger(__name__)

# ── Module-level current-manager reference ───────────────────────────
# Set by AgentService.start_subagent() / stop_subagent() so that native
# tools (Slife.tools.a2a) can look up the live SubagentManager.
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

    def __init__(self, name: str, config: "Config"):
        import json as _json

        self._name = name
        self._config_json = _json.dumps(config.to_dict(), ensure_ascii=False)
        self._process: asyncio.subprocess.Process | None = None
        self._running = False
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stdin_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._async_results: dict[str, str] = {}
        self._ready = asyncio.Event()
        self._push_futures: dict[str, asyncio.Future[dict]] = {}
        """task_id → Future: resolved on next progress/result for that task."""

    @property
    def name(self) -> str: return self._name
    @property
    def pid(self) -> int | None: return self._process.pid if self._process else None
    @property
    def is_running(self) -> bool: return self._running and self._process is not None and self._process.returncode is None
    @property
    def is_ready(self) -> bool: return self._ready.is_set()

    async def start(self) -> None:
        if self._running: return
        cmd = [sys.executable, "-m", "slife.subagent.headless"]
        logger.info("spawn name=%s", self._name)
        env = dict(os.environ)
        env["SLIFE_SUBAGENT_NAME"] = self._name
        env["SLIFE_CONFIG"] = self._config_json
        # Subagents share the main agent's MCP tools but don't need
        # their own memory or wechat connections.
        env.pop("SLIFE_MEMORY_PORT", None)
        env.pop("SLIFE_WECHAT_PORT", None)
        self._process = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
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

    async def send_task(self, task: str, timeout: float = 120.0) -> str:
        if not self.is_running or not self._process or not self._process.stdin:
            raise RuntimeError(f"Subagent '{self._name}' not running")
        if not self.is_ready:
            raise RuntimeError(f"Subagent '{self._name}' not ready")
        rpc_id = uuid.uuid4().hex[:12]

        from slife.a2a.task_store import get_store
        get_store().record_send(rpc_id, self._name, task, "subagent")

        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._pending[rpc_id] = future
        req = json.dumps({"jsonrpc":"2.0","method":"tasks/send","params":{"task":task},"id":rpc_id}, ensure_ascii=False)
        async with self._stdin_lock:
            self._process.stdin.write((req + "\n").encode()); await self._process.stdin.drain()
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            get_store().record_result(rpc_id, result)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(rpc_id, None)
            get_store().record_error(rpc_id, "timeout")
            raise TimeoutError(f"Task to '{self._name}' timed out after {timeout}s")

    async def send_task_async(self, task: str) -> str:
        """Send a task without waiting for the result — returns *rpc_id*.

        The result can be retrieved later via :meth:`get_task_result`.
        """
        if not self.is_running or not self._process or not self._process.stdin:
            raise RuntimeError(f"Subagent '{self._name}' not running")
        if not self.is_ready:
            raise RuntimeError(f"Subagent '{self._name}' not ready")
        rpc_id = uuid.uuid4().hex[:12]

        from slife.a2a.task_store import get_store
        get_store().record_send(rpc_id, self._name, task, "subagent")

        req = json.dumps(
            {"jsonrpc": "2.0", "method": "tasks/send",
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

    def wait_for_task(self, task_id: str) -> asyncio.Future[dict]:
        """Register a Future that resolves on the next update for *task_id*.

        The Future receives the raw JSON-RPC message (result, error, or
        notification) so the caller can decide how to handle it.
        """
        fut: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._push_futures[task_id] = fut
        return fut

    def _resolve_push(self, task_id: str | None, msg: dict) -> None:
        """Resolve any registered push future for *task_id*."""
        if task_id and task_id in self._push_futures:
            fut = self._push_futures.pop(task_id)
            if not fut.done():
                fut.set_result(msg)

    async def _read_stdout(self) -> None:
        if not self._process or not self._process.stdout: return
        reader = self._process.stdout; reader._limit = 10 * 1024 * 1024
        try:
            while self._running:
                line = await reader.readline()
                if not line: break
                try: msg = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError: continue
                rpc_id = msg.get("id")
                from slife.a2a.task_store import get_store
                if rpc_id and rpc_id in self._pending:
                    # Sync waiter — resolve the pending future
                    f = self._pending.pop(rpc_id, None)
                    if not f or f.done(): continue
                    if "error" in msg:
                        f.set_exception(RuntimeError(msg["error"].get("message","Unknown")))
                        get_store().record_error(rpc_id, msg["error"].get("message","Unknown"))
                    else:
                        result_text = str(msg.get("result", ""))
                        f.set_result(result_text)
                        get_store().record_result(rpc_id, result_text)
                    self._resolve_push(rpc_id, msg)
                elif rpc_id:
                    # No synchronous waiter — store for async retrieval
                    if "error" in msg:
                        self._async_results[rpc_id] = f"Error: {msg['error'].get('message', 'Unknown')}"
                        get_store().record_error(rpc_id, msg["error"].get("message","Unknown"))
                    else:
                        result_text = str(msg.get("result", ""))
                        self._async_results[rpc_id] = result_text
                        get_store().record_result(rpc_id, result_text)
                    self._resolve_push(rpc_id, msg)
                    # Notify the manager so it can push the result to the user.
                    self._notify_manager_task_done(rpc_id)
                elif rpc_id is None:
                    # JSON-RPC notification or ready signal (no id)
                    if "result" in msg and msg["result"].get("ready"):
                        self._ready.set()
                    elif "method" in msg:
                        method = msg["method"]
                        params = msg.get("params", {})
                        task_id = params.get("task_id", "")
                        if method == "tasks/complete":
                            get_store().record_result(
                                task_id, str(params.get("result", "")),
                            )
                        elif method == "tasks/progress":
                            logger.debug(
                                "subagent_progress name=%s task=%s pct=%s",
                                self._name, task_id,
                                params.get("pct", "?"),
                            )
                        self._resolve_push(task_id, msg)
                        if method == "tasks/complete" and task_id:
                            self._notify_manager_task_done(task_id)
        except Exception:
            logger.debug("stdout_read_error name=%s", self._name, exc_info=True)

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
            try:
                mgr.on_task_complete(self._name, task_id, result_text)
            except Exception:
                logger.debug(
                    "task_complete_callback_error name=%s task=%s",
                    self._name, task_id, exc_info=True,
                )


class SubagentManager:
    """Manages a collection of SubagentProcess instances."""

    def __init__(self, config: "Config"):
        self._subagents: dict[str, SubagentProcess] = {}
        self._counter = 0
        self._config = config
        sc = config.subagent_config
        self._max = sc["max_subagents"]
        self._timeout = sc["task_timeout"]
        # Callback invoked when a subagent task completes:
        #   async def cb(agent_id: str, task_id: str, result: str) -> None
        self.on_task_complete: "Callable | None" = None

    @property
    def count(self) -> int: return sum(1 for p in self._subagents.values() if p.is_running)

    async def spawn(self, name: str | None = None) -> str:
        if self.count >= self._max: raise RuntimeError(f"Max {self._max} subagents reached")
        if name is None: self._counter += 1; name = f"sub-{self._counter}"
        if name in self._subagents and self._subagents[name].is_running: return name
        proc = SubagentProcess(name, self._config)
        await proc.start(); self._subagents[name] = proc
        return name

    async def send_task(self, agent_id: str, task: str, timeout: float | None = None) -> str:
        if (proc := self._subagents.get(agent_id)) is None:
            raise ValueError(f"Subagent '{agent_id}' not found")
        return await proc.send_task(task, timeout or self._timeout)

    async def send_task_async(self, agent_id: str, task: str) -> str:
        """Send a task without waiting — returns *rpc_id* immediately."""
        if (proc := self._subagents.get(agent_id)) is None:
            raise ValueError(f"Subagent '{agent_id}' not found")
        return await proc.send_task_async(task)

    def get_task_result(self, agent_id: str, rpc_id: str) -> str | None:
        """Return the result of an async task, or ``None`` if not yet ready."""
        if (proc := self._subagents.get(agent_id)) is None:
            return None
        return proc.get_task_result(rpc_id)

    async def broadcast(self, task: str) -> list[str]:
        """Send *task* to every running subagent (fire-and-forget).

        Returns a list of ``"agent_id:rpc_id"`` entries.
        """
        ids: list[str] = []
        for aid in self.list():
            try:
                rpc_id = await self.send_task_async(aid, task)
                ids.append(f"{aid}:{rpc_id}")
            except Exception as e:
                logger.warning("subagent_broadcast_skip agent=%s err=%s", aid, e)
        logger.info("subagent_broadcast agents=%d task=%.80s", len(ids), task)
        return ids

    def list_tasks(
        self, agent_id: str | None = None, status: str | None = None,
    ) -> list:
        """List subagent tasks from the shared :class:`TaskStore`."""
        from slife.a2a.task_store import get_store
        return get_store().list_tasks(
            agent_id=agent_id, status=status, transport="subagent",
        )

    async def subscribe_task(
        self, agent_id: str, task_id: str, timeout: float = 120.0,
    ) -> str | None:
        """Wait for a subagent task to complete.

        If a push future was registered (via :meth:`set_push_notification`),
        awaits it event-driven.  Otherwise falls back to polling
        :meth:`get_task_result`.
        """
        import asyncio as _asyncio, time as _t

        proc = self._subagents.get(agent_id)
        if proc is None:
            raise ValueError(f"Subagent '{agent_id}' not found")

        # Check if result is already available
        result = proc.get_task_result(task_id)
        if result is not None:
            return result

        # If a push future was registered, await it (event-driven)
        if task_id in proc._push_futures:
            fut = proc._push_futures[task_id]
            try:
                msg = await _asyncio.wait_for(fut, timeout=timeout)
            except _asyncio.TimeoutError:
                from slife.a2a.task_store import get_store
                get_store().record_error(task_id, "timeout")
                raise TimeoutError(
                    f"Subscribe to task '{task_id}' on '{agent_id}' "
                    f"timed out after {timeout}s"
                )
            # Extract result from the raw message
            if "error" in msg:
                err = msg["error"].get("message", "Unknown")
                return f"Error: {err}"
            if "result" in msg:
                return str(msg["result"])
            # Progress notification — check store for final result
            from slife.a2a.task_store import get_store
            rec = get_store().get(task_id)
            if rec is not None and rec.result is not None:
                return rec.result
            return None

        # Fallback: poll get_task_result
        deadline = _t.monotonic() + timeout
        while _t.monotonic() < deadline:
            result = proc.get_task_result(task_id)
            if result is not None:
                return result
            await _asyncio.sleep(0.5)

        from slife.a2a.task_store import get_store
        get_store().record_error(task_id, "timeout")
        raise TimeoutError(
            f"Subscribe to task '{task_id}' on '{agent_id}' "
            f"timed out after {timeout}s"
        )

    async def set_push_notification(
        self, agent_id: str, task_id: str, notify_topic: str,
    ) -> bool:
        """Register event-driven push for *task_id* on *agent_id*.

        Creates a Future that resolves on the next message (progress or
        result) from the subagent, so :meth:`subscribe_task` can wait
        without polling.

        If the MQTT client is active, also bridges progress/results to
        *notify_topic* so remote callers can subscribe.
        """
        proc = self._subagents.get(agent_id)
        if proc is None:
            return False

        # Verify the task exists
        from slife.a2a.task_store import get_store
        rec = get_store().get(task_id)
        if rec is None:
            return False

        # Register a push future for event-driven subscribe_task
        proc.wait_for_task(task_id)

        # Bridge to MQTT if available (allows remote callers to subscribe)
        from slife.a2a.client import get_client
        client = get_client()
        if client is not None:
            try:
                import json as _json
                await client._adapter.subscribe(notify_topic)
                logger.info(
                    "subagent_push_mqtt_bridge task=%s agent=%s topic=%s",
                    task_id, agent_id, notify_topic,
                )
                # Publish a setup message so the parent's own notification
                # machinery knows to forward subagent results
                setup = _json.dumps({
                    "correlation_id": task_id,
                    "source": agent_id,
                    "action": "set_push_notification",
                    "notify_topic": notify_topic,
                })
                await client._adapter.publish(
                    f"Slife/{agent_id}/tasks/inbox", setup, qos=1,
                )
            except Exception as e:
                logger.debug("subagent_push_mqtt_bridge_failed err=%s", e)

        logger.info(
            "subagent_push_notification_set task=%s agent=%s topic=%s",
            task_id, agent_id, notify_topic,
        )
        return True

    async def stop(self, agent_id: str) -> bool:
        if (proc := self._subagents.get(agent_id)) is None: return False
        await proc.stop(); del self._subagents[agent_id]
        return True

    async def stop_all(self) -> None:
        if not self._subagents: return
        await asyncio.gather(*(s.stop() for s in list(self._subagents.values())))
        self._subagents.clear()

    def list(self) -> list[str]:
        return [n for n, p in self._subagents.items() if p.is_running]

    def get(self, agent_id: str) -> SubagentProcess | None:
        return self._subagents.get(agent_id)
