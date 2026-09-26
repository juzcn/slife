"""Headless Slife — worker-scoped JSON-RPC 2.0 over stdin/stdout.

A subagent is an *agent worker*: a local child process that runs a full
agent loop.  The control channel is a worker protocol (``worker/*``), not
A2A.  The subagent keeps no independent network identity — when it reaches
the mesh it sends as the parent via the shared a2a plugin.

Protocol::

    ← {"jsonrpc":"2.0","result":{"ready":true},"id":null}
    → {"jsonrpc":"2.0","method":"worker/send","params":{"task":"…"},"id":"x"}
    ← {"jsonrpc":"2.0","result":"…","id":"x"}
    ← {"jsonrpc":"2.0","error":{"code":-32000,"message":"…"},"id":"x"}
    → {"jsonrpc":"2.0","method":"shutdown","id":null}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from pathlib import Path

from slife.server_utils import setup_server_logging, shutdown_server_logging
from slife.logfmt import PROTOCOL_LINE_LIMIT, discard_overlong_line, elapsed
from slife.agent.roles import Role

logger = logging.getLogger("slife_subagent")


#: Set by ``run_headless`` — log path so callers can find it.
_log_path: Path | None = None


def _write(result=None, error=None, rpc_id=None) -> None:
    msg = {"jsonrpc": "2.0", "id": rpc_id}
    if error is not None:
        msg["error"] = {"code": error.get("code", -32000), "message": error.get("message", "")}
    elif result is not None:
        # Only set result when it's not None.  Coercing ``""`` to ``{}``
        # corrupted an empty (silent-success) turn into the literal string
        # "{}" on the parent side — an empty reply must stay empty.
        msg["result"] = result
    # Write UTF-8 bytes directly to stdout buffer.  On Windows, sys.stdout
    # defaults to GBK (or the system locale encoding) which cannot encode
    # emoji and many Unicode characters — json.dumps(ensure_ascii=False)
    # would then crash.  Writing raw UTF-8 bytes bypasses the text codec.
    sys.stdout.buffer.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _notify(method: str, params: dict | None = None) -> None:
    """Send a JSON-RPC notification (no ``id``) to the parent process."""
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    sys.stdout.buffer.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def cancelled_reply_text(reply_text: str, stop_reason: str = "") -> str:
    """Label a preempted task's reply as partial output.

    The loop can stop mid-task (the parent's ``worker/cancel``, or its own task
    bound), and what it produced is then an *interrupted* answer.  The parent
    usually discards such a reply — it cancelled the task itself — but for a
    task that timed out the parent stores it as that task's late result (§6.4),
    where an unlabelled truncated answer would read as the whole one.

    *stop_reason* is the loop's terminal state — one of ``message_history``'s
    short tokens (``esc``, ``parent``, ``max_iterations``, …) — and the label
    names it.  The reply is the only thing the parent ever sees of this task,
    so without the reason a task that hit its own ceiling is indistinguishable
    from one the caller pulled.
    """
    why = f" (reason: {stop_reason})" if stop_reason else ""
    if reply_text.strip():
        return (
            f"{reply_text}\n\n[interrupted — the task was cancelled before "
            f"completion{why}; the text above is partial]"
        )
    return f"Error: task cancelled before completion{why}"


async def run_headless(argv: list[str] | None = None) -> None:
    # ``argv`` carries the FULL command line (program name included) — the
    # shared CLI scanner (``parse_cli_config_path``) slices ``argv[1:]``
    # itself.  Callers MUST pass ``sys.argv`` as-is (a stripped argv would
    # double-strip and mis-read a positional config path).
    if argv is None:
        argv = sys.argv
    global _log_path
    from slife.config import Config
    from slife.agent.service import AgentService

    _name = os.environ.get("SLIFE_SUBAGENT_NAME", "")
    _suffix = f"subagent_{_name}" if _name else "subagent"
    _log_path = setup_server_logging(_suffix)
    logger.info(
        "subagent_start log=%s name=%s pid=%s",
        _log_path,
        os.environ.get("SLIFE_SUBAGENT_NAME", "?"),
        os.getpid(),
    )

    # Inherit config from the main agent via SLIFE_CONFIG_FILE (a 0600 temp
    # file).  Subagents never read the yaml file — they get the main agent's
    # in-memory config directly.  The handover is a file and *only* a file: the
    # config carries resolved plaintext api_keys, and the process environment is
    # readable through the process table (/proc/<pid>/environ), so there is
    # deliberately no env-var channel (DESIGN.md Appendix A 28).
    _config_json = ""
    _config_file = os.environ.get("SLIFE_CONFIG_FILE", "")
    if _config_file:
        try:
            _config_json = Path(_config_file).read_text(encoding="utf-8")
        except OSError:
            _config_json = ""
    if _config_json:
        import json as _json
        with elapsed("config_load", logger, level=logging.INFO, source="SLIFE_CONFIG_FILE"):
            config = Config.from_dict(_json.loads(_config_json))
        # The file is the parent handing its config over.  The report states
        # that provenance rather than a path: a worker has no yaml of its own.
        _config_source = "inherited from the main agent"
    else:
        # Standalone mode: read config from file (fallback).  The shared
        # CLI scanner skips flag values (--agent <id>, --lang <en|zh>), so
        # those can never be misread as a config path.
        from slife.config import parse_cli_config_path
        _config_path = parse_cli_config_path(argv) or "slife.yaml"
        with elapsed("config_load", logger, level=logging.INFO, path=_config_path):
            config = Config.from_yaml(_config_path)
        _config_source = str(_config_path)

    logger.info(
        "config_loaded model=%s tools=%d memory=%s a2a=%s",
        config.active_model.ref,
        len(config.tools),
        "on" if config.memdb_config else "off",
        "on" if config.a2a_config else "off",
    )

    # The host facts every process reports — the same call the TUI entry point
    # makes, so a worker's `system_health` lists the same components as its
    # parent's (config, model, and the external toolchain; without this the
    # worker reported 14 against the parent's 20, and the difference was
    # invisible to anyone reading either report).
    from slife.health import record_host_facts
    record_host_facts(config, source=_config_source)

    service = AgentService(config, role=Role.WORKER)

    # Every plugin the parent started, shared by port — the one manifest loop
    # (``AgentService.connect_shared_plugins``), so "which plugins does a
    # worker use" is answered in the service that owns plugins rather than in
    # this boot.
    await service.connect_shared_plugins()

    # Subagents can spawn their own descendants (recursion enabled).
    await service.start_subagent()

    _write(result={"ready": True})
    logger.info("subagent_ready pid=%s", os.getpid())

    # Read JSON-RPC lines from stdin.  On Windows, connect_read_pipe
    # fails with OSError [WinError 6] (句柄无效) when sys.stdin is a
    # pipe from a parent process — the IOCP registration in the
    # ProactorEventLoop rejects the pipe handle.  We use a dedicated
    # thread calling os.read() instead, which bypasses IOCP and works
    # reliably on pipe handles across all platforms.
    loop = asyncio.get_running_loop()
    # One stdin protocol line can be the whole cloned parent history (the
    # "context" message) — far beyond StreamReader's 64 KB default.  Raise
    # the limit so an honest context is never misread as over-long; an
    # over-long line beyond even the cap is discarded below, not fatal.
    reader = asyncio.StreamReader(limit=PROTOCOL_LINE_LIMIT)

    def _feed_stdin() -> None:
        fd = sys.stdin.fileno()
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            loop.call_soon_threadsafe(reader.feed_data, data)
        loop.call_soon_threadsafe(reader.feed_eof)

    threading.Thread(target=_feed_stdin, daemon=True).start()

    # ── Unified inbox (the same machinery as the main agent) ──────────
    # The subagent is a headless agent worker: identical loop, identical
    # Esc-equivalent cancel.  Its per-role differences — no turn persistence,
    # a one-shot history per task, no heartbeat/scheduler/host server, the
    # catalog queried rather than maintained — are the ROLE's and are wired by
    # ``AgentService`` itself (``slife/agent/roles.py``), not patched in here.
    # worker/send → inbox.post; the reader stays live while a task runs, so
    # worker/cancel can preempt the running loop via inbox.cancel_correlation
    # (→ agent_loop.cancel).
    from slife.a2a.identity import AgentName, AgentMessage, Channel

    await service.start_inbox()
    _source = AgentName(_name or "worker")

    request_count = 0
    try:
        while True:
            try:
                line = await reader.readline()
            except ValueError:
                # LimitOverrunError — a single line beyond the reader limit.
                # Discard its remainder and keep the worker alive; one
                # pathological line must never tear down the whole worker
                # (only JSONDecodeError was caught before — this path would
                # otherwise kill the child on a long context).
                dropped = await discard_overlong_line(reader)
                logger.warning(
                    "subagent_stdin_line_overlong_discarded min_bytes=%d",
                    dropped,
                )
                continue
            if not line:
                break
            try:
                req = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(req, dict):
                # Valid JSON but not a request object (42, [...]) — one such
                # line must never tear down the whole worker (only JSONDecode
                # was guarded before this; an AttributeError on req.get
                # escaped and killed every pending task).
                logger.warning(
                    "subagent_stdin_non_object_discarded type=%s",
                    type(req).__name__,
                )
                continue

            method = req.get("method", "")
            rpc_id = req.get("id")
            params = req.get("params")
            if not isinstance(params, dict):
                params = {}

            if method == "shutdown":
                logger.info("subagent_shutdown requested task_count=%d", request_count)
                break
            elif method == "context":
                # Cloned parent context, sent over stdin at spawn time.  It is
                # the service's field, not this module's: the worker's history
                # store reads it when a task creates its history.
                messages = params.get("messages")
                if isinstance(messages, list):
                    service.inherited_context = messages
                    logger.info(
                        "subagent_context_received messages=%d", len(messages),
                    )
                else:
                    logger.warning(
                        "subagent_context_bad_shape type=%s",
                        type(messages).__name__,
                    )
            elif method == "worker/cancel":
                # True cancellation: drop it if still queued, or preempt the
                # running loop (same Esc mechanism as the main agent).
                task_id = str(params.get("task_id", ""))
                if task_id:
                    logger.info("subagent_cancel_received task=%s", task_id)
                    service.inbox.cancel_correlation(task_id)
            elif method == "worker/plugin_restart":
                # The parent restarted a shared plugin on a new auto-assigned port.
                # Our client still points at the dead one — reconnect so the
                # plugin's tools keep working instead of erroring until the
                # worker exits.  Any shared plugin may restart (not just the
                # mcp wrapper), so the handler is generic.
                plugin = params.get("plugin", "")
                port = params.get("port", 0)
                if plugin and port:
                    logger.info("subagent_plugin_restart plugin=%s port=%s", plugin, port)
                    try:
                        await service.connect_plugin_http(str(plugin), int(port))
                        logger.info(
                            "subagent_plugin_reconnect_done plugin=%s port=%s",
                            plugin, port,
                        )
                    except Exception as e:
                        logger.warning(
                            "subagent_plugin_reconnect_failed plugin=%s port=%s err=%s",
                            plugin, port, e, exc_info=True,
                        )
                else:
                    logger.warning(
                        "subagent_plugin_restart_ignored plugin=%s port=%s",
                        plugin, port,
                    )
            elif method == "worker/send":
                request_count += 1
                task_text = params.get("task", "")
                if not task_text:
                    _write(
                        error={"code": -32602, "message": "Invalid params: task required"},
                        rpc_id=rpc_id,
                    )
                    continue

                async def _reply(
                    reply_text: str, cancelled: bool = False,
                    stop_reason: str = "", rid=rpc_id,
                ) -> None:
                    if cancelled:
                        # The loop was preempted mid-task — say so, and say
                        # why, rather than letting a partial answer pass for a
                        # whole one.
                        reply_text = cancelled_reply_text(reply_text, stop_reason)
                    # The parent may already have discarded this task (it
                    # cancelled it) — writing the late result is harmless.
                    _write(result=reply_text, rpc_id=rid)
                    _notify("worker/complete", {"task_id": str(rid)})

                # The task text is posted as-is: routing back to the parent is
                # by correlation_id and the _reply closure, never by the text,
                # and each task gets a fresh one-shot context (no cross-task
                # disambiguation needed).
                await service.inbox.post(AgentMessage(
                    source=_source,
                    content=task_text,
                    correlation_id=str(rpc_id) if rpc_id else "",
                    on_reply=_reply,
                    channel=Channel.subagent(str(_source)),
                ))
            else:
                _write(
                    error={"code": -32601, "message": f"Method not found: {method}"},
                    rpc_id=rpc_id,
                )
    finally:
        await service.stop_inbox()
        logger.info(
            "subagent_stop task_count=%d tok_p=%s tok_c=%s tok_t=%s",
            request_count,
            service.session_usage.prompt_tokens,
            service.session_usage.completion_tokens,
            service.session_usage.total_tokens,
        )
        # Worker teardown: disconnect every shared plugin client the worker
        # connected to above (the manifest loop) — a worker never owns a
        # child process, so this is a uniform client-disconnect over the
        # registry, mirroring the connect loop instead of a hard-coded three.
        await service.stop_all_plugins()
        shutdown_server_logging()


def main(argv: list[str] | None = None) -> None:
    args = list(argv) if argv is not None else sys.argv
    # `--help` answers BEFORE the worker loop starts: that loop reads stdin
    # for JSON-RPC, so `--headless --help` would otherwise sit waiting for a
    # parent that is never coming.
    from slife.config import CLI_USAGE, parse_cli_help

    if parse_cli_help(args):
        print(CLI_USAGE, end="")
        return
    asyncio.run(run_headless(args))


if __name__ == "__main__":
    # Full argv (program name included) — ``run_headless`` → the CLI scanner
    # expects it and slices ``argv[1:]`` itself.
    main(sys.argv)
