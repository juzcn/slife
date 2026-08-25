"""LLM model management tools.

model_list            — list all configured models grouped by provider
model_set              — add or update a model (creates provider if new)
model_remove           — remove a model by ref (cannot remove the active model)
model_switch           — switch the active model (instant, no restart)
attach_image           — feed images to a vision-capable model in the current turn
_sys_note              — current context status (auto-invoked once per turn)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from slife.tools._config_io import _ConfigPathMixin, read_config, write_config
from slife.tools.base import Tool, make_params

if TYPE_CHECKING:
    from slife.config import Config

logger = logging.getLogger(__name__)

_MODELS_KEY = "models"
_ACTIVE_KEY = "active_model"


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


def _sync_in_memory_models(config, raw: dict) -> None:
    """Rebuild the live Config's model registry from *raw* — as if the
    config file were re-read.

    ``model_set`` / ``model_remove`` persist to disk; this keeps the
    in-memory ``config.models`` in sync so additions/removals take effect
    in the running session without a restart.  No-op when there is no live
    Config (headless, or tools constructed directly in tests).
    """
    if config is None:
        return
    from slife.config import Config
    config.models, _ = Config._parse_models_section(raw.get(_MODELS_KEY, {}))


class _ModelConfigTool(_ConfigPathMixin, Tool):
    """Shared ``__init__``/``from_config`` for the model-mutation tools.

    All three (set/remove/switch) need the live :class:`Config` to keep the
    in-memory model registry in sync with the on-disk JSON5.

    Not a real tool — placeholder class attrs only to pass
    ``Tool.__init_subclass__`` validation; excluded from auto-discovery.
    """

    name = "_model_config_tool"
    description = "model config tool base (placeholder)"
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}
    _skip_auto_register: ClassVar[bool] = True

    def __init__(self, config_path=None, config=None):
        super().__init__(config_path=config_path)
        self._config: "Config | None" = config

    @classmethod
    def from_config(cls, cfg: dict, config: "Config | None", ctx=None):  # noqa: ARG003
        tool = cls(config_path=config._path if config else None, config=config)
        if ctx is not None:
            object.__setattr__(tool, "_ctx", ctx)
        return tool


# ── List Models ──────────────────────────────────────────────────────


class ListModelsTool(_ConfigPathMixin, Tool):
    """List all configured LLM models grouped by provider."""

    name: ClassVar[str] = "model_list"
    category: ClassVar[str] = "Models"
    description: ClassVar[str] = (
        "List configured LLM models grouped by provider (ref, name, API, context, "
        "max tokens, thinking/vision support). Active model marked ★."
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
                compat = m.get("compat")
                compat_tag = f"  compat={compat}" if isinstance(compat, dict) and compat else ""
                lines.append(
                    f"  {star} `{ref}` — {name}"
                    f"  ctx={ctx} max_tok={max_tok}"
                    f"  {thinking} {vision}{compat_tag}".rstrip()
                )
                total += 1
        lines.insert(0, f"**{total} model(s)** configured. Active: `{active}`")
        return "\n".join(lines)


# ── Add Model ────────────────────────────────────────────────────────


class SetModelTool(_ModelConfigTool):
    """Add or update a model on a provider in the config."""

    name: ClassVar[str] = "model_set"
    category: ClassVar[str] = "Models"
    description: ClassVar[str] = (
        "Add/update an LLM model in the configuration (upsert — add + update in "
        "one call); creates the provider if new. Takes effect immediately — the "
        "running session's model registry is synced."
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
            "compat": {
                "type": "object",
                "description": (
                    "Provider-specific compatibility overrides, e.g. "
                    "{thinkingFormat: 'openai'} for Bailian/Qwen anthropic models, "
                    "or {thinking: 'omit'} to skip the thinking parameter entirely "
                    "(MiniMax-style openai-compat gateways)."
                ),
                "additionalProperties": True,
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

        # Model entry.  On update, MERGE into the existing entry instead of
        # replacing it wholesale — an earlier model_set that only changed e.g.
        # max_tokens must not silently drop reasoning/input/compat, which
        # previously broke thinking-enabled models (a model would lose its
        # reasoning flag and stop sending the thinking parameter).
        models = pcfg.setdefault("models", [])
        if not isinstance(models, list):
            models = []
            pcfg["models"] = models

        replaced = False
        for i, m in enumerate(models):
            if isinstance(m, dict) and m.get("model") == model_id:
                # Merge: keep fields the caller didn't pass (reasoning,
                # input, compat, context_window, ...) so a partial update
                # can't strip them.
                model_entry = {**m, "model": model_id, "name": name}
                if "reasoning" in kwargs:
                    model_entry["reasoning"] = kwargs["reasoning"]
                if "input" in kwargs:
                    model_entry["input"] = kwargs["input"]
                if "context_window" in kwargs:
                    model_entry["context_window"] = kwargs["context_window"]
                if "max_tokens" in kwargs:
                    model_entry["max_tokens"] = kwargs["max_tokens"]
                if "compat" in kwargs:
                    model_entry["compat"] = kwargs["compat"]
                models[i] = model_entry
                replaced = True
                break

        if not replaced:
            model_entry = {
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
            if "compat" in kwargs:
                model_entry["compat"] = kwargs["compat"]
            models.append(model_entry)

        write_config(self._config_path, raw)
        ref = f"{pid}/{model_id}"
        action = "Updated" if replaced else "Added"
        # Keep the running session's model registry in sync (as-if re-read),
        # so the new/updated model is immediately available to model_switch.
        _sync_in_memory_models(self._config, raw)
        logger.info("model_%s ref=%s", action.lower(), ref)
        return f"[OK] {action} model `{ref}` ({name})"


# ── Remove Model ─────────────────────────────────────────────────────


class RemoveModelTool(_ModelConfigTool):
    """Remove a model from the config."""

    name: ClassVar[str] = "model_remove"
    category: ClassVar[str] = "Models"
    description: ClassVar[str] = (
        "Remove an LLM model from the configuration by its ref "
        "(e.g. 'bailian/qwen3.8-max').  Takes effect immediately — the running "
        "session's model registry is synced.  Cannot remove the active model; "
        "switch to another model first (model_switch)."
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

        # The active model cannot be removed — switch away first.
        if raw.get(_ACTIVE_KEY, "") == ref:
            return (
                f"Error: cannot remove the active model `{ref}`. "
                f"Switch to another model first with model_switch, then remove it."
            )

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

        write_config(self._config_path, raw)
        # Keep the running session's registry in sync (as-if re-read).
        _sync_in_memory_models(self._config, raw)
        logger.info("model_removed ref=%s", ref)
        return f"[OK] Removed `{ref}`."


# ── Switch Model ─────────────────────────────────────────────────────


class SwitchModelTool(_ModelConfigTool):
    """Switch the active model.

    Updates the config file AND the in-memory Config object so the
    AgentService, LLM client, and TUI status bar all reflect the new
    model without a restart.
    """

    name: ClassVar[str] = "model_switch"
    category: ClassVar[str] = "Models"
    description: ClassVar[str] = (
        "Switch the active LLM model (takes effect next turn). ref from model_list."
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


# ── Attach Image ─────────────────────────────────────────────────────


class AttachImageTool(Tool):
    """Attach one or more images for the LLM to process with vision.

    Takes data URIs, local file paths, or HTTP(S) URLs and makes them
    visible to the vision model.  Works exactly like the ``@`` syntax
    in chat.  Pass a list via ``sources`` for multiple images in one
    call (a single one also works via ``source``).
    """

    name: ClassVar[str] = "attach_image"
    category: ClassVar[str] = "Models"
    _requires_vision: ClassVar[bool] = True
    description: ClassVar[str] = (
        "Attach one or more images for vision processing. "
        "Pass a list via 'sources' (data URI, local file path, or "
        "HTTP(S) URL each); a single image also works via 'source'. "
        "Works like @ syntax."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "sources": {
                "type": "array",
                "items": {
                    "type": "string",
                    "description": (
                        "Data URI (e.g. 'data:image/png;base64,<b64>'), "
                        "local file path (e.g. 'D:\\Downloads\\photo.jpg'), "
                        "or HTTP(S) URL (e.g. 'https://example.com/photo.jpg')."
                    ),
                },
                "description": (
                    "One or more image sources to attach in this call."
                ),
            },
            "source": {
                "type": "string",
                "description": (
                    "A single image source (data URI, local file path, or "
                    "HTTP(S) URL).  Alias for 'sources' with one element — "
                    "prefer 'sources' when attaching multiple images."
                ),
            },
        },
        "oneOf": [
            {"required": ["sources"]},
            {"required": ["source"]},
        ],
    }

    async def execute(self, **kwargs) -> str:
        from slife.agent.multimodal import include_image_urls
        sources = self._resolve_sources(kwargs)

        # Guard against a non-vision model — this is the runtime gate that
        # replaces the build-time filter in tools/factory.py (removed: the
        # tool is always registered).  The tool can exist in the registry
        # when the config is loaded (vision enabled) and the user then
        # switches to a non-vision model: only this per-call check can stop
        # the image from being fed to a model that can't see it.
        active = None
        ctx = getattr(self, "_ctx", None)
        if ctx is not None and ctx.config is not None:
            try:
                active = ctx.config.active_model
            except KeyError:
                active = None
        if active is not None and not active.supports_vision:
            return (
                "Error: the current model does not support image input "
                "(vision=false). Switch to a vision-capable model, then retry."
            )

        blocks, failed = include_image_urls(sources)

        conv = ctx.message_history if ctx is not None else None
        if conv is not None and blocks:
            conv.inject_images_to_last_user(blocks)

        if not blocks:
            return f"Error: cannot read image(s) — {', '.join(failed)}"

        parts = []
        if blocks:
            parts.append(f"Image included: {', '.join(sources)}")
        if failed:
            parts.append(f"Could not read: {', '.join(failed)}")
        return " | ".join(parts)

    @staticmethod
    def _resolve_sources(kwargs: dict) -> list[str]:
        """Return the ordered, deduplicated list of sources from ``sources``
        (list) or ``source`` (single), validating the argument shape.

        Exact duplicates are dropped (order-preserving): attaching the same
        source twice would inject two identical base64 blocks — wasted
        context with no information gain.  Dedup happens here so the
        reported message and the injected blocks both reflect it.
        """
        raw = kwargs.get("sources")
        if raw is None:
            single = kwargs.get("source")
            if single is None:
                raise ValueError("attach_image requires 'sources' or 'source'")
            if isinstance(single, str):
                sources = [single]
            else:
                sources = list(single)
        elif isinstance(raw, (list, tuple)):
            sources = [str(s) for s in raw]
        else:
            # LLM occasionally sends a single string in the array slot — treat
            # it as one source rather than failing.
            sources = [str(raw)]

        seen: set[str] = set()
        deduped: list[str] = []
        for s in sources:
            if s not in seen:
                seen.add(s)
                deduped.append(s)
        return deduped


# ── Context Status (harness auto-invoke) ───────────────────────────────


#: Optional render kwargs the loop passes to ``_sys_note`` each turn.
#: All optional — the tool degrades to a default status if called bare.
_SYS_NOTE_PARAMS = make_params(
    context_window={"type": "integer", "default": 0,
                    "description": "Context window size."},
    last_context_tokens={"type": "integer", "default": 0,
                         "description": "Context token count from the previous turn."},
    model_name={"type": "string", "default": "",
                "description": "Current model display name."},
    input_modalities={"type": "string", "default": "",
                      "description": "Model input modalities."},
    cwd={"type": "string", "default": "",
         "description": "Current working directory."},
    shell={"type": "string", "default": "",
           "description": "Current shell."},
    context_time_start={"type": "string", "default": "",
                        "description": "Context coverage start time."},
    presence_events={"type": "array", "default": [],
                     "description": "Peer online/offline events since the last poll."},
)


class SysNoteTool(Tool):
    """Report the current context status (time, usage %, tokens, peers)."""

    name = "_sys_note"
    category = "Models"
    description = "Current context status: time, context usage %, token usage, peer online/offline events."
    parameters = _SYS_NOTE_PARAMS

    async def execute(self, **kwargs) -> str:
        # Strip harness meta params (_timeout/_async/_approve) the schema
        # injects on every tool — they are not build_context_status params.
        clean = {k: v for k, v in kwargs.items() if k not in ("_timeout", "_async", "_approve")}
        from slife.agent.system_prompt import build_context_status
        return build_context_status(**clean)
