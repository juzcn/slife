"""NVIDIA NIM dynamic model switching — in-memory only, no config writes.

switch_to_nvidia_free:  query the nvidia-nim MCP server for currently
                        available models, classify them via the MCP's
                        own nim_get_model_capabilities tool, and activate
                        one in-memory.  Falls back to the statically
                        configured models on failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, ClassVar

from slife.tools.base import Tool, make_params
from slife.tools.registry import get_registry

if TYPE_CHECKING:
    from slife.config import Config

logger = logging.getLogger(__name__)

# Types that can serve as the active chat model
_SWITCHABLE_TYPES = frozenset({"chat", "vlm", "code"})

_TYPE_LABELS: dict[str, str] = {
    "chat":      "Chat / LLM",
    "vlm":       "Vision (VLM)",
    "code":      "Code",
    "image":     "Image Generation",
    "embed":     "Embeddings",
    "rerank":    "Reranking",
    "safety":    "Safety / Guard",
    "parse":     "Parse / OCR",
    "translate": "Translation",
    "audio":     "Audio / Speech",
}


class SwitchToNvidiaFreeTool(Tool):
    """Switch the active model to a free NVIDIA NIM model.

    Queries the nvidia-nim MCP server for currently available models,
    classifies them by type via nim_get_model_capabilities, and activates
    one **without writing to slife.json5**.  The switch takes effect on
    the next turn.
    """

    name: ClassVar[str] = "switch_to_nvidia_free"
    category: ClassVar[str] = "Config"
    description: ClassVar[str] = (
        "Switch to a free NVIDIA NIM model **in-memory only** "
        "(no config file changes).  Queries the nvidia-nim MCP server "
        "for currently available models, classifies them by type via "
        "nim_get_model_capabilities.  "
        "Use ``list_only=True`` to browse models grouped by type.  "
        "model_type: chat (default), vlm, code, image, embed, or all."
    )
    parameters: ClassVar[dict] = make_params(
        model_type={
            "type": "string",
            "description": (
                "Model type: chat, vlm (vision), code, image, embed, or all.  "
                "Default: chat."
            ),
            "enum": ["chat", "vlm", "code", "image", "embed", "all"],
            "default": "chat",
        },
        list_only={
            "type": "boolean",
            "description": "If True, only list models grouped by type without switching.",
            "default": False,
        },
        model_id={
            "type": "string",
            "description": (
                "Specific model ID to switch to (e.g. 'deepseek-ai/deepseek-v4-flash'). "
                "If omitted, the first model of the chosen type is selected."
            ),
        },
    )

    def __init__(self, config: "Config | None" = None):
        super().__init__()
        self._config: Config | None = config

    @classmethod
    def from_config(cls, cfg: dict, config: "Config | None"):  # noqa: ARG003
        return cls(config=config)

    async def execute(
        self,
        model_type: str = "chat",
        list_only: bool = False,
        model_id: str = "",
        **_kwargs,
    ) -> str:
        if self._config is None:
            return "Error: config not available."

        provider = self._find_nvidia_provider()
        if provider is None:
            return (
                "Error: no 'nvidia' provider found in config. "
                "Add an nvidia provider with NVAPI_KEY to slife.json5."
            )

        # 1. ── Fetch model IDs via MCP ──────────────────────────────
        all_ids = await self._list_nim_models()
        if all_ids is None:
            return (
                "Error: could not query nvidia-nim MCP. "
                "Ensure the nvidia-nim MCP server is enabled and NVAPI_KEY is set."
            )
        if not all_ids:
            return "Error: nvidia-nim MCP returned an empty model list."

        # 2. ── Fast path: specific model_id ─────────────────────────
        if model_id:
            return await self._switch_to_specific(
                model_id, provider,
            )

        # 3. ── Classify models via MCP ──────────────────────────────
        by_type = await self._classify_all(all_ids)
        if by_type is None:
            return "Error: failed to classify models via nim_get_model_capabilities."

        # 4. ── list_only → show grouped listing ─────────────────────
        if list_only:
            return self._format_listing(by_type, model_type)

        # 5. ── Pick & switch ────────────────────────────────────────
        if model_type == "all":
            return (
                "Error: model_type='all' is only for listing. "
                "Use list_only=True to browse, then switch with a specific type."
            )
        if model_type not in _SWITCHABLE_TYPES:
            return (
                f"Error: '{model_type}' models cannot be the active chat model. "
                f"Use the nvidia-nim MCP tools directly for {model_type} tasks."
            )

        pool = by_type.get(model_type, [])
        if not pool:
            available = ", ".join(
                f"{t} ({len(v)})" for t, v in sorted(by_type.items())
            )
            return f"No '{model_type}' models found. Available: {available}"

        mc = self._build_model_config(pool[0], model_type, provider)
        return self._activate_in_memory(mc)

    # ── MCP tool callers ────────────────────────────────────────────

    async def _list_nim_models(self) -> list[str] | None:
        """Call nvidia-nim__nim_list_models."""
        registry = get_registry()
        if registry is None:
            return None
        tool = registry.get("nvidia-nim__nim_list_models")
        if tool is None:
            return None
        try:
            result = await tool.execute()
        except Exception:
            logger.exception("nvidia_nim_list_failed")
            return None
        if not result or result.startswith("Error:"):
            return None
        return [l.strip() for l in result.split("\n") if l.strip()]

    async def _get_capabilities(self, model_id: str) -> dict | None:
        """Call nvidia-nim__nim_get_model_capabilities for one model.

        The MCP tool returns JSON like:
          {"type": "chat", "vision": false, "tools": true, "context": 131072, "notes": "..."}
        """
        registry = get_registry()
        if registry is None:
            return None
        tool = registry.get("nvidia-nim__nim_get_model_capabilities")
        if tool is None:
            return None
        try:
            result = await tool.execute(model=model_id)
        except Exception:
            logger.debug("nvidia_nim_caps_failed model=%s", model_id, exc_info=True)
            return None
        if not result or result.startswith("Error:"):
            return None
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            logger.debug("nvidia_nim_caps_parse model=%s raw=%.200s", model_id, result)
            return None

    async def _classify_all(self, ids: list[str]) -> dict[str, list[str]] | None:
        """Classify every model by calling nim_get_model_capabilities.

        Runs with limited concurrency so we don't flood the MCP server.
        """
        by_type: dict[str, list[str]] = {}
        sem = asyncio.Semaphore(8)  # at most 8 concurrent calls

        async def classify_one(mid: str):
            async with sem:
                caps = await self._get_capabilities(mid)
            return mid, caps

        # Gather all concurrently (bounded by semaphore)
        tasks = [classify_one(mid) for mid in ids]
        results = await asyncio.gather(*tasks)

        for mid, caps in results:
            t = "other"
            if isinstance(caps, dict):
                t = caps.get("type", "other")
            by_type.setdefault(t, []).append(mid)

        return by_type

    async def _switch_to_specific(self, model_id: str, provider: dict) -> str:
        """Verify a specific model_id and switch to it."""
        caps = await self._get_capabilities(model_id)
        if caps is None:
            return (
                f"Error: could not get capabilities for '{model_id}'. "
                f"The model may not exist or the MCP call failed."
            )

        model_type = caps.get("type", "chat")
        if model_type not in _SWITCHABLE_TYPES:
            return (
                f"Error: '{model_id}' is type '{model_type}', not a chat model. "
                f"Use the nvidia-nim MCP tools directly for {model_type} tasks."
            )

        mc = self._build_model_config(model_id, model_type, provider)
        # Use capabilities for better config when available
        if caps.get("vision"):
            mc.supports_vision = True
            mc.input_modalities = ("text", "image")
        ctx = caps.get("context")
        if isinstance(ctx, (int, float)) and ctx > 0:
            mc.context_window = int(ctx)
        mc.thinking_enabled = caps.get("tools", True)

        return self._activate_in_memory(mc)

    # ── helpers ─────────────────────────────────────────────────────

    def _find_nvidia_provider(self) -> dict | None:
        config = self._config
        assert config is not None  # execute() guards this before calling
        for m in config.models:
            if m.provider == "nvidia":
                return {
                    "api_key": m.api_key,
                    "base_url": m.base_url,
                    "api": m.api,
                }
        return None

    def _build_model_config(self, model_id: str, model_type: str, provider: dict):
        from slife.config import ModelConfig
        is_vision = model_type == "vlm"
        return ModelConfig(
            ref=f"nvidia/{model_id}",
            provider="nvidia",
            api_model=model_id,
            display_name=f"NVIDIA {model_id.split('/')[-1]}",
            api_key=provider["api_key"],
            base_url=provider["base_url"],
            api=provider.get("api", "openai-completions"),
            supports_vision=is_vision,
            input_modalities=("text", "image") if is_vision else ("text",),
            thinking_enabled=True,
            context_window=131072,
            max_tokens=32768,
        )

    def _activate_in_memory(self, model_config) -> str:
        config = self._config
        assert config is not None

        for i, existing in enumerate(config.models):
            if existing.ref == model_config.ref:
                config.models[i] = model_config
                break
        else:
            config.models.append(model_config)

        old_ref = config.active_model_ref
        config.active_model_ref = model_config.ref

        try:
            from slife.agent.service import _on_model_switched
            for cb in _on_model_switched:
                try:
                    cb(model_config.ref)
                except Exception:
                    logger.debug("nvidia_nim_switch_callback_error", exc_info=True)
        except ImportError:
            pass

        logger.info(
            "nvidia_nim_switched from=%s to=%s display=%s",
            old_ref, model_config.ref, model_config.display_name,
        )
        return (
            f"[OK] Switched to NVIDIA NIM model `{model_config.ref}` "
            f"({model_config.display_name}) — in-memory only."
        )

    def _format_listing(self, by_type: dict[str, list[str]], focus: str) -> str:
        total = sum(len(v) for v in by_type.values())
        lines = [f"**{total} NVIDIA NIM model(s)** (focus: {focus}):"]

        # Sort: focus type first, switchable next, rest last
        def sort_key(item: tuple[str, list[str]]) -> int:
            t = item[0]
            if t == focus:
                return 0
            if t in _SWITCHABLE_TYPES:
                return 1
            return 2

        ordered = sorted(by_type.items(), key=sort_key)

        for t, ids in ordered:
            label = _TYPE_LABELS.get(t, t or "other")
            switchable = "✓" if t in _SWITCHABLE_TYPES else " "
            lines.append(f"\n  {label} ({len(ids)}):")
            for mid in ids:
                marker = " ← auto-pick" if t == focus and mid == ids[0] else ""
                lines.append(f"    [{switchable}] `{mid}`{marker}")

        lines.append(f"\n✓ = available as active chat model")
        return "\n".join(lines)
