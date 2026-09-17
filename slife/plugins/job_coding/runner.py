"""Job execution runtime — the ``llm`` handle job modules import.

A job is a plain Python function in the jobs directory; running it is
deterministic code execution with exactly the declared arguments.  The ONLY
concessions to the LLM are the explicit ``llm.chat(...)`` calls a job author
writes — each a single, narrow ``LLMClient`` batch chat on the job model
(``job_coding_model`` in slife.json5, or the active model as a fallback).
No system prompt, no conversation history, no agent loop ever reaches the
job — messages are constructed solely by job code.

Job files do::

    from slife.plugins.job_coding import llm

    async def translate(text: str, lang: str = "zh") -> str:
        \"\"\"Translate text into target language.\"\"\"
        return await llm.chat(
            system="You are a professional translator. Output only the translation.",
            user=f"Translate the following into {lang}:\\n{text}",
        )

Jobs that call the LLM are ``async def``; pure-computation jobs can be
plain ``def`` functions — the runner handles both.

The tool schema (name, description, params) is derived by FastMCP from the
function's ``__name__``/docstring/annotations — standard MCP tool norms.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import logging
import os
from typing import Any

from slife.paths import get_config_path
from slife.tools._config_io import read_config

logger = logging.getLogger(__name__)


# ── Model resolution ───────────────────────────────────────────────────


def resolve_job_model() -> Any:
    """Resolve the job LLM ``ModelConfig``.

    1. ``job_coding_model`` top-level key of slife.json5: a ``provider/model``
       ref reusing the main ``models.providers`` (independent of the
       conversation's active model — usually a cheap/fast model).
    2. Fallback: the main config's active model.

    Returns ``None`` when neither can be resolved; jobs that never call
    ``llm`` still work, and ``llm.chat`` then raises a clear error.
    """
    # Short-circuit the cached Config when possible: read the ref from the
    # raw file, then resolve it through the same model list.
    try:
        raw = read_config(get_config_path())
    except Exception as e:
        logger.warning("job_config_unreadable err=%s", e)
        raw = {}
    if isinstance(raw, dict):
        ref = raw.get("job_coding_model")
        if isinstance(ref, str) and ref.strip():
            try:
                return resolve_model_ref(ref.strip())
            except Exception as e:
                logger.warning("job_coding_model_resolve_failed ref=%s err=%s", ref, e)

    try:
        return _get_config().active_model
    except Exception as e:
        logger.warning("job_active_model_fallback_failed err=%s", e)
    return None


# ── Execution context ──────────────────────────────────────────────────

#: The LLMClient bound to the currently-executing job's model.  The
#: ``llm`` proxy reads it, so ``from slife.plugins.job_coding import llm`` works
#: at import time while ``llm.chat`` binds to the running job at call time.
_current_client: contextvars.ContextVar = contextvars.ContextVar(
    "slife_job_llm_client", default=None
)


class _LLMClientRef:
    """A lazily-resolved LLM client bound to a job tool.

    Job tools are registered in the lifespan so the harness's first
    ``tools/list`` already lists them — and the lifespan must stay
    connect-fast.  Building the real ``LLMClient`` imports the provider
    SDK (a multi-second cold import that, on a slow machine, pushed
    job-coding past the spawn guard).  The client is therefore deferred
    until the job actually calls ``llm.chat``: :meth:`get` builds it once
    (from the zero-arg *factory*) and caches it; a ``None`` factory result
    (no model configured) is a valid cached value.
    """

    def __init__(self, factory):
        self._factory = factory
        self._client: Any = None
        self._resolved = False

    def get(self) -> Any:
        if not self._resolved:
            self._client = self._factory()
            self._resolved = True
        return self._client

#: Cached main Config for per-call model lookups (``llm.chat(model=...)``).
_config: Any = None


def _get_config() -> Any:
    """Lazily load and cache the main slife Config (for model resolution)."""
    global _config
    if _config is None:
        from slife.config import Config
        _config = Config.from_json5(get_config_path())
    return _config


def resolve_model_ref(model_ref: str) -> Any:
    """Resolve a model ref to a ``ModelConfig`` from the main config.

    Accepts ``"provider/model"`` or a bare model id (first match across
    providers).  Raises ``ValueError`` with the available set when unknown.
    """
    cfg = _get_config()
    ref = model_ref.strip()
    for model in cfg.models:
        if ref in (model.ref, f"{model.provider}/{model.api_model}", model.api_model):
            return model
    available = ", ".join(sorted(m.ref for m in cfg.models)) or "(none)"
    raise ValueError(f"Unknown model '{model_ref}'. Available: {available}")


class _LLMProxy:
    """``llm`` handle available inside job functions.

    Every call performs exactly one batch chat.  ``model`` selects a model
    explicitly (``"provider/model"`` or a bare model id from the main
    config); when omitted the call uses the job's configured model
    (``job_coding_model`` in slife.json5, or the active model).  Messages
    are built from the job author's arguments — structural guarantee that
    no conversation context ever reaches the model.
    """

    async def chat(
        self,
        *,
        system: str | None = None,
        user: str | None = None,
        messages: list[dict] | None = None,
        model: str | None = None,
    ) -> str:
        client = _current_client.get()
        if model:
            from slife.agent.llm_client import LLMClient
            client = LLMClient(resolve_model_ref(model))
        elif client is None:
            raise RuntimeError(
                "llm.chat() called outside a running job "
                "(job_coding requires a configured job_coding_model or active model)"
            )
        msgs = list(messages) if messages else []
        if system:
            # Prepend/replace the system message — never duplicate.
            msgs = [m for m in msgs if m.get("role") != "system"]
            msgs.insert(0, {"role": "system", "content": system})
        if user is not None:
            msgs.append({"role": "user", "content": user})
        if not msgs:
            raise ValueError("llm.chat() requires user= or messages= (or both)")
        # STREAM, never batch: Anthropic-messages proxies (bailian) and the
        # Anthropic SDK reject non-streaming requests for long operations
        # ("Streaming is required for operations that may take longer than
        # 10 minutes").  Accumulate the assistant text from the stream — one
        # narrow one-shot call either way, just transport-robust.
        parts: list[str] = []
        async for chunk in client.chat_stream(msgs):
            if chunk.content:
                parts.append(chunk.content)
        return "".join(parts)


llm = _LLMProxy()


# ── MCP gateway access (bare MCP) ───────────────────────────────────────

# Jobs may call external MCP tools through the mcp-gateway plugin's
# persistent connection pool — bare MCP, on demand.  The gateway's port is
# pushed by the host on every gateway connect/reconnect
# (``__set_mcp_gateway_port``), with the spawn-time env var
# ``SLIFE_MCP_GATEWAY_PORT`` as a fallback; the port itself is a readable
# fact at any time — the harness's health probe resolves it without
# connecting.  The gateway's INTERNAL tool
# ``__mcp_call_tool(server, tool_name, arguments)`` reaches any tool
# on any connected external server, including tools never loaded into the
# main agent's tool registry (only ``auto_load`` servers are; unloaded tool
# names are probed at authoring time via the host's ``mcp_list_tools``).
#
# NO CLIENT IS HELD between calls.  A 2026-07-28 (modern) MCP connection is
# stateless — no session id, no handshake, per-request ``_meta``, and the
# transport opens no standalone channel without a session id — so a session
# object buys one ``server/discover`` and nothing else.  Holding one cost a
# failure mode instead: a session that died under a live-looking
# ``is_connected`` failed every later call for the plugin's lifetime.  One
# ``mcp.call`` is one short-lived client, which is also why no reconnect
# bookkeeping lives here.

#: Host env var the generic spawn publishes for a plugin's port
#: (via ``plugin_port_env("mcp-gateway")`` — uppercase, dashes→underscores).
def _gateway_port_env() -> str:
    from slife.agent.plugins import plugin_port_env

    return plugin_port_env("mcp-gateway")

#: Connect attempts for one ``mcp.call`` — a count, so it stays local.  The
#: default startup window (~10 s) exists to outlast a plugin still coming up;
#: this caller is one-shot against a gateway that has been serving for a
#: while, and its NEXT call retries anyway, so it waits only long enough to
#: ride out a watchdog restart.
_CALL_CONNECT_ATTEMPTS = 3

_gateway_port: str | None = None     # last known port ("push" or "env")
_gateway_port_source: str = ""       # "push" | "env" | "" — diagnostic only


class _GatewayProxy:
    """``mcp`` handle available inside job functions — bare MCP access.

    Every call forwards ONE tool invocation to the mcp-gateway plugin's
    persistent connection pool (``__mcp_call_tool``), so a job can use any
    tool on any connected server from ``tools.json5`` — including
    tools never loaded into the main agent's registry.  Always returns a
    string (never raises); an unreachable gateway / disconnected or
    disabled server / unknown tool all surface as a clear error the job can
    branch on — the same determinism contract as ``llm``.
    """

    def _resolve_port(self) -> tuple[str | None, str]:
        """The port a call would use, resolved WITHOUT connecting.

        Host push wins; ``SLIFE_MCP_GATEWAY_PORT`` is the fallback — a
        job-coding child spawned after the gateway inherits it, and the
        host's push only reaches a client that is already up.  Shared by
        :attr:`port` / :attr:`port_source` (the health facts) and
        :meth:`call`, so what a probe reports is exactly what a call uses.
        """
        if _gateway_port:
            return _gateway_port, _gateway_port_source
        env_port = (os.environ.get(_gateway_port_env()) or "").strip()
        if env_port:
            return env_port, "env"
        return None, ""

    @property
    def port(self) -> str | None:
        """The gateway port a call would use (push or env), or None."""
        return self._resolve_port()[0]

    @property
    def port_source(self) -> str:
        """Where the port came from: ``"push"``, ``"env"``, or ``""``."""
        return self._resolve_port()[1]

    async def set_port(self, port: int | None) -> None:
        """Record the gateway port pushed by the host (gateway (re)connect).

        Nothing to re-point or tear down: the next call resolves the port
        afresh, so a gateway restarted on a new port is followed by
        construction.  ``None`` clears the pushed port.
        """
        global _gateway_port, _gateway_port_source
        _gateway_port = str(port) if port else None
        _gateway_port_source = "push" if port else ""

    async def call(
        self,
        server: str,
        tool: str,
        args: "dict | None" = None,
    ) -> str:
        """Call *tool* on external MCP server *server* (bare MCP, one-shot).

        Forwards one invocation to the gateway's persistent connection pool
        (``__mcp_call_tool``) — the same call shape the host's
        ``{server}__{tool}`` proxies use.  Returns the tool's text output,
        or a clear ``Error: ...`` string when the gateway is unreachable /
        the server is disconnected or disabled / the tool is unknown —
        never raises.  Jobs branch on the ``"Error"`` prefix.

        One call, one client, torn down again: the protocol is stateless, so
        nothing outlives the call and nothing can go stale between them.
        """
        port, _ = self._resolve_port()
        if not port:
            logger.warning("job_mcp_no_gateway_port")
            return (
                "Error: mcp.call — no connection to the mcp-gateway plugin "
                "(port unknown). It is published on gateway start; check that "
                "mcp-gateway is running."
            )
        from slife.plugins.mcp_gateway.client import MCPClient

        client = MCPClient()
        try:
            await client.connect(
                f"http://127.0.0.1:{port}/mcp", attempts=_CALL_CONNECT_ATTEMPTS,
            )
        except Exception as e:
            logger.warning("job_mcp_connect_failed port=%s err=%s", port, e)
            return (
                f"Error: mcp.call('{server}', '{tool}') failed: "
                f"{type(e).__name__}: {e}"
            )
        try:
            return await client.call_tool(
                "__mcp_call_tool",
                {
                    "server": server,
                    "tool_name": tool,
                    "arguments": json.dumps(args, ensure_ascii=False) if args else "{}",
                },
            )
        except Exception as e:  # call_tool returns its own errors; belt and braces
            logger.warning(
                "job_mcp_call_failed server=%s tool=%s err=%s",
                server, tool, e,
            )
            return (
                f"Error: mcp.call('{server}', '{tool}') failed: "
                f"{type(e).__name__}: {e}"
            )
        finally:
            try:
                await client.disconnect()
            except Exception:
                logger.debug("job_mcp_client_disconnect_error", exc_info=True)


mcp = _GatewayProxy()


# ── Tool wrapper ───────────────────────────────────────────────────────


def _to_text(result: Any) -> str:
    """Normalize a job's return value to a tool result string."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False, indent=2)
    return str(result)


def wrap(fn, client) -> Any:
    """Return the async tool function FastMCP registers for *fn*.

    ``functools.wraps`` copies ``__name__``/``__doc__``/``__annotations__``
    and sets ``__wrapped__``, so FastMCP's schema derivation sees the
    ORIGINAL job function's signature and docstring.  Execution binds the
    job's LLM client in the context variable, runs *fn* (async fns awaited
    on the loop, sync fns in a worker thread so the loop stays free), and
    normalizes the result.  Errors become ``"Error: …"`` tool results —
    deterministic, never a plugin crash.

    *client* may be the LLMClient itself or a lazy ``_LLMClientRef`` (the
    server registers jobs with a ref so the heavy provider-SDK import is
    deferred to the job's first ``llm.chat``, never paid at registration).
    """
    name = getattr(fn, "__name__", "?")

    @functools.wraps(fn)
    async def _run(**kwargs):
        # A lazy ref defers LLMClient construction (heavy provider-SDK
        # import) to the job's first llm.chat — registration stays fast.
        resolved = client.get() if isinstance(client, _LLMClientRef) else client
        token = _current_client.set(resolved)
        try:
            if inspect.iscoroutinefunction(fn):
                result = await fn(**kwargs)
            else:
                # Sync job on a daemon worker thread — run_daemon, NOT
                # asyncio.to_thread: the default executor's non-daemon worker
                # threads are joined (wait=True) at interpreter exit, so a
                # hung blocking job wedges the whole plugin shutdown.
                # to_thread propagates the contextvar via its internals;
                # run_daemon does not, so capture the context and run the job
                # inside it — the llm client stays visible to sync jobs.
                from slife.threads import run_daemon

                ctx = contextvars.copy_context()

                def _run_sync():
                    return ctx.run(fn, **kwargs)

                result = await run_daemon(_run_sync, name=f"job-{name}")
            return _to_text(result)
        except Exception as e:
            logger.warning("job_exec_failed name=%s err=%s", name, e)
            return f"Error: {type(e).__name__}: {e}"
        finally:
            _current_client.reset(token)

    return _run