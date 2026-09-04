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

import asyncio
import contextvars
import functools
import inspect
import json
import logging
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
    """
    name = getattr(fn, "__name__", "?")

    @functools.wraps(fn)
    async def _run(**kwargs):
        token = _current_client.set(client)
        try:
            if inspect.iscoroutinefunction(fn):
                result = await fn(**kwargs)
            else:
                # Sync job on a worker thread — to_thread propagates the
                # current context (incl. our llm client), loop stays free.
                result = await asyncio.to_thread(fn, **kwargs)
            return _to_text(result)
        except Exception as e:
            logger.warning("job_exec_failed name=%s err=%s", name, e)
            return f"Error: {type(e).__name__}: {e}"
        finally:
            _current_client.reset(token)

    return _run