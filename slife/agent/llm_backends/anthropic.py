"""Anthropic Messages API backend.

Takes OpenAI-format messages (slife's internal format) and internally
adapts them for the Anthropic Messages API.  No public conversion layer —
the adaptation is a private implementation detail of chat/chat_stream.
"""

from __future__ import annotations

import json
import logging
import time as _time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic

from slife.agent.llm_client import StreamChunk, TokenUsage, _chat_response
from slife.config import ModelConfig

if TYPE_CHECKING:
    import asyncio

logger = logging.getLogger(__name__)


class AnthropicBackend:
    """Anthropic Messages API backend."""

    def __init__(self, model: ModelConfig):
        self.model_config = model
        self.client = AsyncAnthropic(
            api_key=model.api_key,
            base_url=model.base_url or "https://api.anthropic.com",
        )
        logger.debug(
            "anthropic_init model=%s provider=%s thinking=%s",
            model.api_model, model.provider, model.thinking_enabled,
        )

    # ── Internal adaptation (private) ─────────────────────────────────

    @staticmethod
    def _oa_msgs_to_anthropic(
        messages: list[dict],
        cache_control: bool = True,
    ) -> tuple[list[dict] | None, list[dict]]:
        """Adapt OpenAI-format messages → Anthropic Messages format.

        Returns ``(system_blocks, anthropic_messages)``.

        *system_blocks* is a list of Anthropic system content blocks
        (``{"type": "text", "text": ...}``), one per OpenAI ``system``
        message in order, or ``None`` when there are none.  When
        *cache_control* is true the **last** block carries
        ``cache_control: {"type": "ephemeral"}`` so the stable system
        prompt acts as a prompt-cache breakpoint (everything before it is
        cached across turns).

        *anthropic_messages* strictly alternates ``user`` / ``assistant``
        The Anthropic API requires this: an OpenAI-format
        batch emits one ``tool`` message per tool call, and each must not
        become its own ``user`` block (consecutive ``user`` messages 400 on
        strict-alternation endpoints such as Bedrock / Bailian/Qwen).  All
        tool results of one batch are coalesced into a single ``user``
        message, and a user text message directly after tool results is
        merged into that same block.
        """
        system_parts: list[str] = []
        converted: list[dict] = []
        pending_tool_results: list[dict] = []

        def _flush_tool_results() -> None:
            if pending_tool_results:
                # Copy — the list is stored in the message, then cleared so
                # the next batch starts fresh.
                converted.append({"role": "user", "content": list(pending_tool_results)})
                pending_tool_results.clear()

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                system_parts.append(str(content))
            elif role == "user":
                if isinstance(content, list):
                    blocks = []
                    for part in content:
                        if part.get("type") == "image_url":
                            url = part.get("image_url", {}).get("url", "")
                            if url.startswith("data:"):
                                # data:image/jpeg;base64,<data>
                                header, b64 = url.split(",", 1)
                                mime = header.split(":")[1].split(";")[0]
                                blocks.append({
                                    "type": "image",
                                    "source": {"type": "base64", "media_type": mime, "data": b64},
                                })
                            else:
                                blocks.append({
                                    "type": "image",
                                    "source": {"type": "url", "url": url},
                                })
                        else:
                            blocks.append(part)
                    user_content = blocks
                else:
                    user_content = str(content)
                if pending_tool_results:
                    # Tool results + this user text form ONE user message —
                    # a user block may carry both tool_result and text
                    # content, and separate blocks would break alternation.
                    if not isinstance(user_content, list):
                        user_content = [{"type": "text", "text": user_content}]
                    converted.append({
                        "role": "user",
                        "content": pending_tool_results + user_content,
                    })
                    pending_tool_results.clear()
                else:
                    converted.append({"role": "user", "content": user_content})
            elif role == "assistant":
                _flush_tool_results()
                blocks: list[dict] = []
                if content and str(content).strip():
                    blocks.append({"type": "text", "text": str(content)})
                for tc in (msg.get("tool_calls") or []):
                    fn = tc.get("function", {})
                    try:
                        inp = json.loads(fn.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        inp = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": fn.get("name", ""),
                        "input": inp,
                    })
                if not blocks:
                    # An assistant turn with no text and no tool_calls (e.g. a
                    # reasoning-only response cut by max_tokens) would emit an
                    # empty content array, which the Messages API rejects with a
                    # 400.  Emit a single empty text block instead — it keeps the
                    # role alternating and carries the (empty) turn.
                    blocks.append({"type": "text", "text": ""})
                converted.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": str(content),
                })
        _flush_tool_results()

        system_blocks: list[dict] | None = None
        if system_parts:
            system_blocks = [{"type": "text", "text": p} for p in system_parts]
            if cache_control:
                system_blocks[-1]["cache_control"] = {"type": "ephemeral"}
        return system_blocks, converted

    @staticmethod
    def _oa_tools_to_anthropic(tools: list[dict]) -> list[dict]:
        """Adapt OpenAI function defs → Anthropic tool format."""
        result = []
        for t in tools:
            fn = t.get("function", {})
            schema = dict(fn.get("parameters", {}))
            schema.setdefault("type", "object")
            # Keep harness meta-params (_timeout/_async/_approve): the three
            # backends expose the same tool schemas to the model, so the LLM
            # can drive timeouts/async/approval uniformly on every API.
            result.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": schema,
            })
        return result

    def _use_system_cache_control(self) -> bool:
        """Whether to attach ``cache_control`` to the system blocks.

        On by default for the official Anthropic endpoint; off for
        Anthropic-compatible providers (Bailian/Qwen, …) that may reject
        the field.  Override per model with ``compat.cacheControl``
        (bool).
        """
        compat = self.model_config.compat or {}
        if "cacheControl" in compat:
            return bool(compat["cacheControl"])
        base = (self.model_config.base_url or "").rstrip("/")
        return "api.anthropic.com" in base

    def _is_reasoning_default(self) -> bool:
        """Whether the model reasons by default when ``thinking`` is absent.

        DeepSeek/Qwen reasoning models default to thinking ON on their
        native APIs — and on gateways serving them, such as
        bailian_personal's Anthropic-compatible endpoint — so an explicit
        ``{"type": "disabled"}`` must be sent when thinking is off.  Native
        Anthropic models default to OFF and may simply omit the field.

        The model id is matched in addition to provider/host because
        gateways serve deepseek/qwen under their own provider and host
        names (bailian_personal, token-plan.cn-beijing.maas.aliyuncs.com).
        """
        p = self.model_config.provider.lower()
        u = self.model_config.base_url.lower()
        m = self.model_config.api_model.lower()
        return (
            "deepseek" in p or "deepseek" in u
            or m.startswith("deepseek") or m.startswith("qwen")
        )

    # ── Build kwargs ──────────────────────────────────────────────────

    def _build_kwargs(
        self, messages: list[dict], tools: list[dict] | None
    ) -> dict:
        system, msgs = self._oa_msgs_to_anthropic(
            messages, cache_control=self._use_system_cache_control(),
        )
        kwargs: dict = {
            "model": self.model_config.api_model,
            "messages": msgs,
            "max_tokens": self.model_config.max_tokens,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._oa_tools_to_anthropic(tools)
        # Sampling params (temperature/top_p/top_k) are no longer typed
        # kwargs on create/stream since SDK 1.x (the httpx2 release) — they
        # remain valid request-body fields, sent via extra_body per the
        # official MIGRATION.md.  extra_body is accepted the same way by
        # older SDKs, so this path is version-agnostic.
        body: dict = {}
        if self.model_config.temperature:
            body["temperature"] = self.model_config.temperature
        if self.model_config.top_p:
            body["top_p"] = self.model_config.top_p
        if body:
            kwargs["extra_body"] = body
        # Per-model compat escape hatch: ``compat.thinking`` explicitly
        # controls the ``thinking`` parameter, matching the openai
        # backend.  Values: "enabled" / "disabled" force the exact
        # shape; "omit" (or anything else) sends no thinking field.
        compat = self.model_config.compat or {}
        thinking_override = compat.get("thinking")
        if thinking_override is not None and thinking_override != "enabled":
            # "disabled" -> explicit off; "omit"/other -> no thinking field.
            if thinking_override == "disabled":
                kwargs["thinking"] = {"type": "disabled"}
            return kwargs

        if self.model_config.thinking_enabled:
            # Bailian / qwen models with thinkingFormat "openai" always
            # think — no explicit Anthropic thinking param is needed (and
            # sending one may cause errors).
            if compat.get("thinkingFormat") != "openai":
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": max(self.model_config.max_tokens // 2, 1024),
                }
        elif self._is_reasoning_default():
            # DeepSeek/Qwen reason by default — an explicit "disabled" is
            # required when thinking is off.  Native Anthropic models
            # default to thinking off and simply omit the field.
            kwargs["thinking"] = {"type": "disabled"}
        return kwargs

    # ── Chat ──────────────────────────────────────────────────────────

    async def chat(
        self, messages: list[dict], tools: list[dict] | None = None,
    ) -> tuple:
        kwargs = self._build_kwargs(messages, tools)
        response = await self.client.messages.create(**kwargs)
        text = "".join(
            b.text for b in response.content
            if hasattr(b, "type") and b.type == "text"
        )
        usage = TokenUsage(
            prompt_tokens=response.usage.input_tokens or 0,
            completion_tokens=response.usage.output_tokens or 0,
            total_tokens=(response.usage.input_tokens or 0) + (response.usage.output_tokens or 0),
        )
        return _chat_response(text, usage)

    # ── Streaming ─────────────────────────────────────────────────────

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[StreamChunk]:
        kwargs = self._build_kwargs(messages, tools)
        t0 = _time.monotonic()
        tool_accum: dict[int, dict] = {}
        usage = TokenUsage()

        logger.debug(
            "anthropic_stream_start model=%s msgs=%d tools=%d",
            self.model_config.api_model, len(messages),
            len(tools) if tools else 0,
        )

        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if cancel_event is not None and cancel_event.is_set():
                    logger.debug("anthropic_stream_cancelled")
                    break
                etype = getattr(event, "type", None)

                if etype == "message_start":
                    msg = getattr(event, "message", None)
                    if msg and msg.usage:
                        usage.prompt_tokens = msg.usage.input_tokens or 0

                elif etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if block and getattr(block, "type", None) == "tool_use":
                        idx = getattr(event, "index", 0)
                        tool_accum[idx] = {
                            "id": getattr(block, "id", ""),
                            "name": getattr(block, "name", ""),
                            "arguments": "",
                        }

                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if delta is None:
                        continue
                    idx = getattr(event, "index", 0)
                    dtype = getattr(delta, "type", None)

                    if dtype == "text_delta":
                        yield StreamChunk(content=delta.text)
                    elif dtype == "thinking_delta":
                        yield StreamChunk(thinking=delta.thinking)
                    elif dtype == "input_json_delta":
                        if idx in tool_accum:
                            tool_accum[idx]["arguments"] += delta.partial_json
                        acc = tool_accum.get(idx, {})
                        yield StreamChunk(tool_deltas=[{
                            "index": idx,
                            "id": acc.get("id", ""),
                            "function": {
                                "name": acc.get("name", ""),
                                "arguments": delta.partial_json,
                            },
                        }])

                elif etype == "message_delta":
                    u = getattr(event, "usage", None)
                    if u:
                        usage.completion_tokens = u.output_tokens or 0
                        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
                        elapsed = (_time.monotonic() - t0) * 1000
                        logger.debug(
                            "anthropic_stream_done prompt=%d completion=%d total=%d took_ms=%.0f",
                            usage.prompt_tokens, usage.completion_tokens,
                            usage.total_tokens, elapsed,
                        )
                        yield StreamChunk(usage=usage)
