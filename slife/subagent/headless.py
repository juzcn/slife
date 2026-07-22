"""Headless Slife — JSON-RPC 2.0 over stdin/stdout (A2A spec §9).

Protocol::

    ← {"jsonrpc":"2.0","result":{"ready":true},"id":null}
    → {"jsonrpc":"2.0","method":"tasks/send","params":{"task":"…"},"id":"x"}
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


async def _process(task_text: str, rpc_id, service) -> None:
    from slife.agent.conversation import Conversation
    from slife.agent.system_prompt import build as build_system_prompt
    from slife.agent.loop import MaxIterationsExceeded

    logger.info("task_start id=%s task=%.100s", rpc_id, task_text)
    conv = Conversation(
        system_prompt=build_system_prompt(
            agent_id=os.environ.get("SLIFE_AGENT_ID", "slife"),
            agent_name=os.environ.get("SLIFE_SUBAGENT_NAME", ""),
        ),
    )

    try:
        with elapsed("task_loop", logger, level=logging.INFO, rpc_id=rpc_id):
            result = await service.agent_loop.run(
                user_input=task_text, conversation=conv, handler=None,
            )
        _write(result=result.text, rpc_id=rpc_id)
        _notify("tasks/complete", {"task_id": str(rpc_id)})
        logger.info(
            "task_done id=%s tok_p=%s tok_c=%s tok_t=%s",
            rpc_id,
            result.usage.prompt_tokens,
            result.usage.completion_tokens,
            result.usage.total_tokens,
        )
    except MaxIterationsExceeded as e:
        logger.warning("task_loop_exceeded id=%s err=%s", rpc_id, e)
        _write(error={"code": -32000, "message": str(e)}, rpc_id=rpc_id)
        _notify("tasks/complete", {"task_id": str(rpc_id)})
    except Exception as e:
        logger.error("task_error id=%s err=%s", rpc_id, e)
        _write(error={"code": -32000, "message": str(e)}, rpc_id=rpc_id)
        _notify("tasks/complete", {"task_id": str(rpc_id)})


async def run_headless() -> None:
    global _log_path
    from slife.config import Config
    from slife.agent.service import AgentService

    _name = os.environ.get("SLIFE_SUBAGENT_NAME", "")
    _suffix = f"_{_name}" if _name else "_subagent"
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
        "on" if config.memory_config else "off",
        "on" if config.mcp_config else "off",
        "on" if config.a2a_config else "off",
    )

    service = AgentService(config, is_subagent=True)

    # Connect to the main agent's plugin servers via Streamable HTTP when
    # ports are provided.  Subagents share the main agent's plugins instead
    # of spawning their own — avoids duplicate processes and shared state.
    _mcp_port = os.environ.get("SLIFE_MCP_PORT", "")
    _mem_port = os.environ.get("SLIFE_MEMORY_PORT", "")
    _wc_port = os.environ.get("SLIFE_WECHAT_PORT", "")

    if _mcp_port and config.mcp_config:
        try:
            with elapsed("mcp_startup", logger, level=logging.INFO, port=_mcp_port):
                await service.connect_mcp_http(int(_mcp_port))
        except Exception as e:
            logger.warning("mcp_http_failed port=%s err=%s", _mcp_port, e)

    if _mem_port and config.memory_config:
        try:
            with elapsed("memory_startup", logger, level=logging.INFO, port=_mem_port):
                await service.connect_memory_http(int(_mem_port))
        except Exception as e:
            logger.warning("memory_http_failed port=%s err=%s", _mem_port, e)

    if _wc_port and config.wechat_config and config.wechat_config.enabled:
        try:
            with elapsed("wechat_startup", logger, level=logging.INFO, port=_wc_port):
                await service.connect_wechat_http(int(_wc_port))
        except Exception as e:
            logger.warning("wechat_http_failed port=%s err=%s", _wc_port, e)

    _write(result={"ready": True})
    logger.info("subagent_ready")

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
            elif method == "tasks/send":
                request_count += 1
                task_text = params.get("task", "")
                if not task_text:
                    _write(
                        error={"code": -32602, "message": "Invalid params: task required"},
                        rpc_id=rpc_id,
                    )
                    continue
                await _process(task_text, rpc_id, service)
            else:
                _write(
                    error={"code": -32601, "message": f"Method not found: {method}"},
                    rpc_id=rpc_id,
                )
    finally:
        logger.info(
            "subagent_stop task_count=%d tok_p=%s tok_c=%s tok_t=%s",
            request_count,
            service.session_usage.prompt_tokens,
            service.session_usage.completion_tokens,
            service.session_usage.total_tokens,
        )
        await service.stop_mcp()
        await service.stop_memory()
        await service.stop_wechat()
        shutdown_server_logging()


def main(argv: list[str] | None = None) -> None:
    asyncio.run(run_headless())


if __name__ == "__main__":
    main(sys.argv[1:])
