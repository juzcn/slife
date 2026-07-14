"""Headless slife — JSON-RPC 2.0 over stdin/stdout (A2A spec §9).

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
from pathlib import Path

logger = logging.getLogger("slife.subagent")


_log_handler: logging.FileHandler | None = None
"""Module-level reference so we can close the handler on shutdown."""


def _setup_logging() -> Path:
    global _log_handler
    from slife.logfmt import init_session_id, SessionFormatter, FILE_LOG_FORMAT
    sid = init_session_id(); os.environ["SLIFE_SESSION_ID"] = sid
    (log_dir := Path("logs")).mkdir(exist_ok=True)
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = os.environ.get("SLIFE_SUBAGENT_NAME", "subagent")
    log_path = log_dir / f"slife_{name}_{ts}.log"
    root = logging.getLogger(); root.setLevel(logging.DEBUG)
    _log_handler = logging.FileHandler(log_path, encoding="utf-8")
    _log_handler.setLevel(logging.DEBUG)
    _log_handler.setFormatter(SessionFormatter(FILE_LOG_FORMAT))
    root.addHandler(_log_handler)
    for mod in ("openai._base_client", "httpcore", "httpx", "asyncio", "urllib3"):
        logging.getLogger(mod).setLevel(logging.WARNING)
    return log_path


def _shutdown_logging() -> None:
    """Close and remove the file handler, releasing the Windows file lock."""
    global _log_handler
    if _log_handler is not None:
        root = logging.getLogger()
        root.removeHandler(_log_handler)
        _log_handler.flush()
        _log_handler.close()
        _log_handler = None


def _write(result=None, error=None, rpc_id=None) -> None:
    msg = {"jsonrpc": "2.0", "id": rpc_id}
    if error is not None:
        msg["error"] = {"code": error.get("code", -32000), "message": error.get("message", "")}
    else:
        msg["result"] = result or {}
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def _process(task_text: str, rpc_id, service) -> None:
    from slife.agent.conversation import Conversation
    from slife.agent.system_prompt import build as build_system_prompt
    from slife.agent.loop import MaxIterationsExceeded

    logger.info("task_start id=%s task=%.100s", rpc_id, task_text)
    conv = Conversation(system_prompt=build_system_prompt())

    try:
        result = await service.agent_loop.run(user_input=task_text, conversation=conv, handler=None)
        _write(result=result.text, rpc_id=rpc_id)
        logger.info("task_done id=%s tok=%d", rpc_id, result.usage.total_tokens)
    except MaxIterationsExceeded as e:
        _write(error={"code": -32000, "message": str(e)}, rpc_id=rpc_id)
    except Exception as e:
        logger.error("task_error id=%s err=%s", rpc_id, e)
        _write(error={"code": -32000, "message": str(e)}, rpc_id=rpc_id)


async def run_headless(config_path: str = "slife.json5") -> None:
    from slife.config import Config
    from slife.agent.service import AgentService

    log_path = _setup_logging()
    logger.info("start config=%s log=%s", config_path, log_path)

    config = Config.from_json5(config_path)
    logger.info("model=%s tools=%d", config.active_model.ref, len(config.tools))

    service = AgentService(config)
    if config.mcp_config and config.mcp_config.enabled:
        try: await service.start_mcp()
        except Exception as e: logger.warning("mcp_failed err=%s", e)

    _write(result={"ready": True})
    logger.info("ready")

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    try:
        while True:
            line = await reader.readline()
            if not line: break
            try: req = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError: continue

            method = req.get("method", "")
            rpc_id = req.get("id")
            params = req.get("params", {})

            if method == "shutdown": break
            elif method == "tasks/send":
                task_text = params.get("task", "")
                if not task_text:
                    _write(error={"code": -32602, "message": "Invalid params: task required"}, rpc_id=rpc_id)
                    continue
                await _process(task_text, rpc_id, service)
            else:
                _write(error={"code": -32601, "message": f"Method not found: {method}"}, rpc_id=rpc_id)
    finally:
        logger.info("shutdown")
        await service.stop_mcp()
        _shutdown_logging()


def main(argv: list[str] | None = None) -> None:
    config_path = next((a for a in (argv or []) if not a.startswith("-")), "slife.json5")
    asyncio.run(run_headless(config_path))


if __name__ == "__main__":
    main(sys.argv[1:])
