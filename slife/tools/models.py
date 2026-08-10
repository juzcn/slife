"""LLM model management tools.

model_list            — list all configured models grouped by provider
model_add              — add or update a model (creates provider if new)
model_remove           — remove a model by ref; auto-switches if it was active
model_switch           — switch the active model (instant, no restart)
switch_to_nvidia_free  — switch to a free NVIDIA NIM model in-memory only
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, ClassVar

from slife.tools._config_io import _ConfigPathMixin, read_config, write_config
from slife.tools.base import Tool, make_params

if TYPE_CHECKING:
    from slife.config import Config

logger = logging.getLogger(__name__)

_MODELS_KEY = "models"
_ACTIVE_KEY = "active_model"

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


def _providers_section(raw: dict) -> dict:
    """Get or create the models.providers: section."""
    models = raw.setdefault(_MODELS_KEY, {})
    if not isinstance(models, dict):
        models = {}
        raw[_MODELS_KEY] = models
    providers = models.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = {}
        models["providers"] = providers
    return providers


def _find_first_ref(providers: dict) -> str | None:
    """Find the first model ref across all providers."""
    for pid, pcfg in providers.items():
        if isinstance(pcfg, dict):
            for m in pcfg.get("models", []):
                if isinstance(m, dict):
                    mid = m.get("model", m.get("id", ""))
                    if mid:
                        return f"{pid}/{mid}"
    return None


# ── List Models ──────────────────────────────────────────────────────


class ListModelsTool(_ConfigPathMixin, Tool):
    """List all configured LLM models grouped by provider."""

    name: ClassVar[str] = "model_list"
    category: ClassVar[str] = "Models"
    description: ClassVar[str] = (
        "List all configured LLM models grouped by provider. "
        "Shows each model's ref, display name, API type, context window, "
        "max tokens, thinking support, and vision support. "
        "The active model is marked with ★."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **_kwargs) -> str:
        if not self._config_path:
            return "Error: config path not available."
        raw = read_config(self._config_path)
        providers = raw.get(_MODELS_KEY, {}).get("providers", {})
        if not isinstance(providers, dict) or not providers:
            return "No models configured. Add a provider with models in slife.json5."

        active = raw.get(_ACTIVE_KEY, "")
        lines = []
        total = 0
        for pid, pcfg in providers.items():
            if not isinstance(pcfg, dict):
                continue
            api = pcfg.get("api", "openai-completions")
            base = pcfg.get("base_url", pcfg.get("baseUrl", ""))
            models = pcfg.get("models", [])
            if not isinstance(models, list):
                continue
            lines.append(f"\n## {pid}  (api: {api}, base: {base})")
            for m in models:
                if not isinstance(m, dict):
                    continue
                model_id = m.get("model", m.get("id", "?"))
                name = m.get("name", model_id)
                ref = f"{pid}/{model_id}"
                star = "★" if ref == active else " "
                thinking = "🧠" if m.get("reasoning", m.get("thinking_enabled")) else ""
                vision = "👁" if "image" in m.get("input", []) else ""
                ctx = m.get("context_window", m.get("contextWindow", "?"))
                max_tok = m.get("max_tokens", m.get("maxTokens", "?"))
                lines.append(
                    f"  {star} `{ref}` — {name}"
                    f"  ctx={ctx} max_tok={max_tok}"
                    f"  {thinking} {vision}".rstrip()
                )
                total += 1
        lines.insert(0, f"**{total} model(s)** configured. Active: `{active}`")
        return "\n".join(lines)


# ── Add Model ────────────────────────────────────────────────────────


class AddModelTool(_ConfigPathMixin, Tool):
    """Add a model to a provider in the config."""

    name: ClassVar[str] = "model_add"
    category: ClassVar[str] = "Models"
    description: ClassVar[str] = (
        "Add an LLM model to the configuration. If the provider doesn't exist, "
        "it will be created.  Supported api values: openai-completions, "
        "anthropic-messages, openai-responses."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "description": "Provider ID (e.g. deepseek, bailian, openai). Created if new.",
            },
            "model": {
                "type": "string",
                "description": "API model name (e.g. qwen3.8-max, gpt-4o).",
            },
            "name": {
                "type": "string",
                "description": "Display name for the model (e.g. 'Qwen3.8 Max').",
            },
            "api": {
                "type": "string",
                "description": "API type: openai-completions, anthropic-messages, or openai-responses. Default: openai-completions.",
                "enum": ["openai-completions", "anthropic-messages", "openai-responses"],
            },
            "base_url": {
                "type": "string",
                "description": "Base URL for the API endpoint. Required for new providers.",
            },
            "api_key": {
                "type": "string",
                "description": "API key as ${VAR} reference (e.g. ${BAILIAN_API_KEY}). Required for new providers.",
            },
            "reasoning": {
                "type": "boolean",
                "description": "Whether the model supports reasoning/thinking. Default: false.",
            },
            "input": {
                "type": "array",
                "description": 'Input modalities, e.g. ["text"] or ["text","image"]. Default: ["text"].',
                "items": {"type": "string"},
            },
            "context_window": {
                "type": "integer",
                "description": "Context window size in tokens. Default: 131072.",
            },
            "max_tokens": {
                "type": "integer",
                "description": "Max output tokens. Default: 4096.",
            },
        },
        "required": ["provider", "model", "name"],
    }

    async def execute(self, **kwargs) -> str:
        if not self._config_path:
            return "Error: config path not available."

        raw = read_config(self._config_path)
        providers = _providers_section(raw)

        pid = kwargs["provider"]
        model_id = kwargs["model"]
        name = kwargs["name"]

        # Provider: get or create
        if pid not in providers or not isinstance(providers[pid], dict):
            if "base_url" not in kwargs:
                return (
                    f"Error: provider '{pid}' does not exist. "
                    f"Provide base_url and api_key to create it."
                )
            providers[pid] = {}
        pcfg = providers[pid]
        if not isinstance(pcfg, dict):
            pcfg = {}
            providers[pid] = pcfg

        # Set provider-level defaults if provided
        if "base_url" in kwargs:
            pcfg["base_url"] = kwargs["base_url"]
        if "api_key" in kwargs:
            pcfg["api_key"] = kwargs["api_key"]
        if "api" in kwargs:
            pcfg["api"] = kwargs["api"]

        # Model entry
        model_entry: dict = {
            "model": model_id,
            "name": name,
        }
        if "reasoning" in kwargs:
            model_entry["reasoning"] = kwargs["reasoning"]
        if "input" in kwargs:
            model_entry["input"] = kwargs["input"]
        if "context_window" in kwargs:
            model_entry["context_window"] = kwargs["context_window"]
        if "max_tokens" in kwargs:
            model_entry["max_tokens"] = kwargs["max_tokens"]

        # UPSERT: replace if model with same id exists, otherwise append
        models = pcfg.setdefault("models", [])
        if not isinstance(models, list):
            models = []
            pcfg["models"] = models

        replaced = False
        for i, m in enumerate(models):
            if isinstance(m, dict) and m.get("model") == model_id:
                models[i] = model_entry
                replaced = True
                break
        if not replaced:
            models.append(model_entry)

        write_config(self._config_path, raw)
        ref = f"{pid}/{model_id}"
        action = "Updated" if replaced else "Added"
        logger.info("model_%s ref=%s", action.lower(), ref)
        return f"[OK] {action} model `{ref}` ({name})"


# ── Remove Model ─────────────────────────────────────────────────────


class RemoveModelTool(_ConfigPathMixin, Tool):
    """Remove a model from the config."""

    name: ClassVar[str] = "model_remove"
    category: ClassVar[str] = "Models"
    description: ClassVar[str] = (
        "Remove an LLM model from the configuration by its ref "
        "(e.g. 'bailian/qwen3.8-max').  If it's the active model, "
        "you will need to switch to another model afterwards."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "Model ref to remove (e.g. 'bailian/qwen3.8-max').",
            },
        },
        "required": ["ref"],
    }

    async def execute(self, **kwargs) -> str:
        if not self._config_path:
            return "Error: config path not available."

        ref = kwargs["ref"]
        if "/" not in ref:
            return f"Error: invalid ref '{ref}'. Use format: provider/model-name"

        pid, model_id = ref.split("/", 1)
        raw = read_config(self._config_path)
        providers = raw.get(_MODELS_KEY, {}).get("providers", {})
        if not isinstance(providers, dict):
            return f"Error: no providers configured."

        pcfg = providers.get(pid)
        if not isinstance(pcfg, dict):
            return f"Error: provider '{pid}' not found."

        models = pcfg.get("models", [])
        if not isinstance(models, list):
            return f"Error: no models in provider '{pid}'."

        removed = False
        for i, m in enumerate(models):
            if isinstance(m, dict) and (m.get("model") == model_id or m.get("id") == model_id):
                del models[i]
                removed = True
                break

        if not removed:
            return f"Error: model '{ref}' not found."

        # Clean up empty provider
        if not models:
            del providers[pid]

        # If removed model was active, warn
        active = raw.get(_ACTIVE_KEY, "")
        if active == ref:
            remaining = _find_first_ref(providers)
            if remaining:
                raw[_ACTIVE_KEY] = remaining
                write_config(self._config_path, raw)
                logger.info("model_removed_active ref=%s new_active=%s", ref, remaining)
                return (
                    f"[OK] Removed `{ref}` (was active). "
                    f"Switched active model to `{remaining}`."
                )
            else:
                raw[_ACTIVE_KEY] = ""
                write_config(self._config_path, raw)
                return (
                    f"[OK] Removed `{ref}` (was active). No models remaining. "
                    f"Add a model with model_add before continuing."
                )

        write_config(self._config_path, raw)
        logger.info("model_removed ref=%s", ref)
        return f"[OK] Removed `{ref}`."


# ── Switch Model ─────────────────────────────────────────────────────


class SwitchModelTool(_ConfigPathMixin, Tool):
    """Switch the active model.

    Updates the config file AND the in-memory Config object so the
    AgentService, LLM client, and TUI status bar all reflect the new
    model without a restart.
    """

    name: ClassVar[str] = "model_switch"
    category: ClassVar[str] = "Models"
    description: ClassVar[str] = (
        "Switch the active LLM model.  The new model takes effect on the "
        "next turn.  Use model_list to see available models and their refs."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "Model ref to activate (e.g. 'deepseek/deepseek-v4-flash').",
            },
        },
        "required": ["ref"],
    }

    def __init__(self, config_path=None, config=None):
        super().__init__(config_path=config_path)
        self._config: Config | None = config

    @classmethod
    def from_config(cls, cfg: dict, config: "Config | None", ctx=None):
        tool = cls(config_path=config._path if config else None, config=config)
        if ctx is not None:
            object.__setattr__(tool, "_ctx", ctx)
        return tool

    async def execute(self, **kwargs) -> str:
        if not self._config_path:
            return "Error: config path not available."

        ref = kwargs["ref"]
        raw = read_config(self._config_path)

        # Validate that the model exists
        providers = raw.get(_MODELS_KEY, {}).get("providers", {})
        if not isinstance(providers, dict):
            return f"Error: no providers configured."

        if "/" not in ref:
            return f"Error: invalid ref '{ref}'. Use format: provider/model-name"

        pid, model_id = ref.split("/", 1)
        pcfg = providers.get(pid)
        found = False
        display = model_id
        if isinstance(pcfg, dict):
            for m in pcfg.get("models", []):
                if isinstance(m, dict) and (
                    m.get("model") == model_id or m.get("id") == model_id
                ):
                    found = True
                    display = m.get("name", model_id)
                    break

        if not found:
            return f"Error: model '{ref}' not found in config. Use model_list."

        old = raw.get(_ACTIVE_KEY, "(none)")
        raw[_ACTIVE_KEY] = ref
        write_config(self._config_path, raw)
        logger.info("model_switched from=%s to=%s", old, ref)

        # ── Update in-memory config + notify runtime ──────────────
        if self._config is not None:
            self._config.active_model_ref = ref
        # Fire module-level callbacks so AgentService rebuilds the
        # LLM client and the TUI refreshes the status bar.
        try:
            from slife.agent.service import _on_model_switched
            for cb in _on_model_switched:
                try:
                    cb(ref)
                except Exception:
                    logger.debug("model_switch_callback_error", exc_info=True)
        except ImportError:
            pass

        return f"[OK] Switched active model from `{old}` to `{ref}` ({display})."


# ── Switch to NVIDIA Free ─────────────────────────────────────────────


class SwitchToNvidiaFreeTool(Tool):
    """Switch the active model to a free NVIDIA NIM model.

    Queries the nvidia-nim MCP server for currently available models,
    classifies them by type via nim_get_model_capabilities, and activates
    one **without writing to slife.json5**.  The switch takes effect on
    the next turn.
    """

    name: ClassVar[str] = "switch_to_nvidia_free"
    category: ClassVar[str] = "Models"
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
    def from_config(cls, cfg: dict, config: "Config | None", ctx=None):  # noqa: ARG003
        tool = cls(config=config)
        if ctx is not None:
            object.__setattr__(tool, "_ctx", ctx)
        return tool

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
        """Call nvidia-nim__nim_model_list."""
        ctx = getattr(self, "_ctx", None)
        registry = ctx.registry if ctx is not None else None
        if registry is None:
            return None
        tool = registry.get("nvidia-nim__nim_model_list")
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
        ctx = getattr(self, "_ctx", None)
        registry = ctx.registry if ctx is not None else None
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
