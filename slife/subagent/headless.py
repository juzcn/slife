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
from slife.logfmt import elapsed

logger = logging.getLogger("slife_subagent")

#: Set by ``run_headless`` — log path so callers can find it.
_log_path: Path | None = None

#: Cloned parent conversation (received via the stdin "context" message),
#: or None for a clean-context subagent.
_inherited_context: list[dict] | None = None


def _write(result=None, error=None, rpc_id=None) -> None:
    msg = {"jsonrpc": "2.0", "id": rpc_id}
    if error is not None:
        msg["error"] = {"code": error.get("code", -32000), "message": error.get("message", "")}
    else:
        msg["result"] = result or {}
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


async def run_headless() -> None:
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

    # Inherit config from the main agent via SLIFE_CONFIG env var.
    # Subagents never read the json5 file — they get the main agent's
    # in-memory config directly.
    _config_json = os.environ.get("SLIFE_CONFIG", "")
    if _config_json:
        import json as _json
        with elapsed("config_load", logger, level=logging.INFO, source="SLIFE_CONFIG"):
            config = Config.from_dict(_json.loads(_config_json))
    else:
        # Standalone mode: read config from file (fallback).
        import sys as _sys
        _config_path = next(
            (a for a in _sys.argv[1:] if not a.startswith("-")), "slife.json5",
        )
        with elapsed("config_load", logger, level=logging.INFO, path=_config_path):
            config = Config.from_json5(_config_path)

    logger.info(
        "config_loaded model=%s tools=%d memory=%s mcp=%s a2a=%s",
        config.active_model.ref,
        len(config.tools),
        "on" if config.memdb_config else "off",
        "on" if config.mcp_config else "off",
        "on" if config.a2a_config else "off",
    )

    service = AgentService(config, is_subagent=True)

    # Connect to the main agent's plugin servers via Streamable HTTP when
    # ports are provided.  Subagents share the main agent's plugins instead
    # of spawning their own — avoids duplicate processes and shared state.
    _mcp_port = os.environ.get("SLIFE_MCP_PORT", "")

    if _mcp_port and config.mcp_config:
        try:
            with elapsed("mcp_startup", logger, level=logging.INFO, port=_mcp_port):
                await service.connect_mcp_http(int(_mcp_port))
        except Exception as e:
            logger.warning("mcp_http_failed port=%s err=%s", _mcp_port, e)

    # Subagents share the main agent's memory and wechat servers too.
    _memdb_port = os.environ.get("SLIFE_MEMDB_PORT", "")
    if _memdb_port and config.memdb_config:
        try:
            with elapsed("memdb_connect", logger, level=logging.INFO, port=_memdb_port):
                await service.connect_memdb_http(int(_memdb_port))
        except Exception as e:
            logger.warning("memdb_http_failed port=%s err=%s", _memdb_port, e)

    _wechat_port = os.environ.get("SLIFE_WECHAT_PORT", "")
    if _wechat_port and config.wechat_config:
        try:
            with elapsed("wechat_connect", logger, level=logging.INFO, port=_wechat_port):
                await service.connect_wechat_http(int(_wechat_port))
        except Exception as e:
            logger.warning("wechat_http_failed port=%s err=%s", _wechat_port, e)

    # Reuse the parent agent's a2a plugin (thin client): register the a2a_*
    # tools so we can send as the parent, but never drain the inbound queue
    # (all replies and management belong to the parent agent).
    _a2a_port = os.environ.get("SLIFE_A2A_PORT", "")
    if _a2a_port:
        try:
            await service.connect_a2a_http(int(_a2a_port))
        except Exception as e:
            logger.warning("a2a_http_failed port=%s err=%s", _a2a_port, e)

    # Share the main agent's memfiles plugin (file cabinet + public URLs)
    # instead of spawning a second instance that would fight over the
    # single free-tier ngrok tunnel.
    _memfiles_port = os.environ.get("SLIFE_MEMFILES_PORT", "")
    if _memfiles_port:
        try:
            await service.connect_memfiles_http(int(_memfiles_port))
        except Exception as e:
            logger.warning("memfiles_http_failed port=%s err=%s", _memfiles_port, e)

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
    reader = asyncio.StreamReader()

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
    # Esc-equivalent cancel.  The only differences are no TUI handler and
    # no turn persistence (REVIEW C5).  worker/send → inbox.post; the
    # reader stays live while a task runs, so worker/cancel can preempt
    # the running loop via inbox.cancel_correlation (→ agent_loop.cancel).
    from slife.agent.conversation import Conversation
    from slife.agent.inbox import ConversationStore
    from slife.agent.system_prompt import build as build_system_prompt
    from slife.a2a.identity import AgentName, AgentMessage

    # Subagents never save turns to memory, even when sharing the main
    # agent's memdb plugin (which would make memdb_enabled True).
    service.inbox._on_turn_complete = None

    class _WorkerConversationStore(ConversationStore):
        """Fresh one-shot conversation per task, seeded with the cloned
        parent context (mirrors the main agent's remote-message model)."""

        def get_or_create(self, source: AgentName) -> Conversation:
            if _inherited_context:
                return Conversation.from_history(
                    self._system_prompt, _inherited_context,
                )
            return Conversation(system_prompt=self._system_prompt)

    service.inbox._conversations = _WorkerConversationStore(
        system_prompt=build_system_prompt(service.config, is_subagent=True),
    )
    await service.start_inbox()
    _source = AgentName(_name or "worker")

    request_count = 0
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                req = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue

            method = req.get("method", "")
            rpc_id = req.get("id")
            params = req.get("params", {})

            if method == "shutdown":
                logger.info("subagent_shutdown requested task_count=%d", request_count)
                break
            elif method == "context":
                # Cloned parent context, sent over stdin at spawn time.
                messages = params.get("messages")
                if isinstance(messages, list):
                    global _inherited_context
                    _inherited_context = messages
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
                    reply_text: str, cancelled: bool = False, rid=rpc_id,
                ) -> None:
                    # The parent may already have discarded this task (it
                    # cancelled it) — writing the late result is harmless.
                    _write(result=reply_text, rpc_id=rid)
                    _notify("worker/complete", {"task_id": str(rid)})

                await service.inbox.post(AgentMessage(
                    source=_source,
                    content=task_text,
                    correlation_id=str(rpc_id) if rpc_id else "",
                    on_reply=_reply,
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
        await service.stop_mcp()
        await service.stop_memdb()
        await service.stop_wechat()
        shutdown_server_logging()


def main(argv: list[str] | None = None) -> None:
    asyncio.run(run_headless())


if __name__ == "__main__":
    main(sys.argv[1:])
