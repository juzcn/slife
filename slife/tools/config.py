"""Config management tools.

config_env_set    — write env var to slife.json5
config_env_get    — read env var (shell → slife.json5)
config_env_remove — remove env var from slife.json5
native_tool_set   — enable/disable a built-in tool
"""

from __future__ import annotations

import logging
import os
from typing import ClassVar

from slife.tools._config_io import _ConfigPathMixin, read_config, write_config
from slife.tools.base import Tool

logger = logging.getLogger(__name__)

_PLACEHOLDER_PREFIX = "<YOUR_"


def _env_section(raw: dict) -> dict:
    env = raw.setdefault("env", {})
    if not isinstance(env, dict):
        env = {}
        raw["env"] = env
    return env


def _mcp_env_sections(raw: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    servers = raw.get("mcp", {}).get("servers", {})
    if isinstance(servers, dict):
        for name, cfg in servers.items():
            if isinstance(cfg, dict):
                server_env = cfg.get("env", {})
                if isinstance(server_env, dict) and server_env:
                    result[name] = dict(server_env)
    return result


def _lookup_one(key: str, env: dict, mcp_envs: dict[str, dict]) -> str:
    env_val = os.environ.get(key)
    if env_val:
        return f"{key} = {env_val} [shell]"
    sources = []
    config_val = env.get(key)
    if config_val and config_val not in (None, ""):
        sources.append(("slife.json5", str(config_val)))
    for server_name, server_env in sorted(mcp_envs.items()):
        val = server_env.get(key)
        if val and val not in (None, ""):
            sources.append((f"mcp/{server_name}", str(val)))
    if not sources:
        return f"'{key}' is not set."
    lines = [f"{key}:"]
    for source_name, value in sources:
        marker = " ← active" if source_name == sources[0][0] else ""
        lines.append(f"  [{source_name}]{marker}: {value}")
    return "\n".join(lines)


def _format_one(key: str, value: str) -> str:
    env_val = os.environ.get(key)
    if env_val:
        return f"  {key} = {env_val} [shell]"
    is_placeholder = str(value).startswith(_PLACEHOLDER_PREFIX)
    note = " [PLACEHOLDER]" if is_placeholder else " [unset]"
    return f"  {key} = {value}{note}"


def _toggle_native_enabled(raw: dict, name: str, enabled: bool) -> None:
    tools_override: list = raw.setdefault("tools", [])
    if not isinstance(tools_override, list):
        tools_override = []
        raw["tools"] = tools_override
    for entry in tools_override:
        if isinstance(entry, dict) and entry.get("name") == name:
            entry["enabled"] = enabled
            return
    tools_override.append({"name": name, "enabled": enabled})


# ═══════════════════════════════════════════════════════════════════════

class ConfigEnvSetTool(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    name = "config_env_set"
    category: ClassVar[str] = "Config"
    _subagent_skip = True
    description = "Write an env var to slife.json5. Use ${VAR} refs for secrets — never plaintext."
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Env var name, e.g. EDITOR, DEEPSEEK_API_KEY."},
            "value": {"type": "string", "description": "Value to set. Omit to write a placeholder."},
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs.get("key", "")
        value: str | None = kwargs.get("value")
        raw = read_config(self._config_path)
        env = _env_section(raw)
        if value:
            env[key] = value
            os.environ[key] = str(value)
            write_config(self._config_path, raw)
            logger.info("env_set key=%s", key)
            return f"[OK] {key} = {value}"
        else:
            placeholder = f"<YOUR_{key.upper().strip('<>')}>"
            env[key] = placeholder
            write_config(self._config_path, raw)
            logger.info("env_set_placeholder key=%s", key)
            return f"[OK] {key} placeholder written.\nEdit slife.json5 → env: → {key} with the real value."


class ConfigEnvGetTool(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    name = "config_env_get"
    category: ClassVar[str] = "Config"
    description = "Look up an env var: shell first, then slife.json5. ${VAR} refs shown as-is. Omit key to list all."
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Env var name. Omit to list all."},
        },
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs.get("key", "")
        raw = read_config(self._config_path)
        env = _env_section(raw)
        mcp_envs = _mcp_env_sections(raw)
        if key:
            return _lookup_one(key, env, mcp_envs)
        lines = []
        if env:
            lines.append("env:")
            for k in sorted(env.keys()):
                lines.append(_format_one(k, env.get(k, "")))
        else:
            lines.append("env: (empty)")
        for server_name, server_env in sorted(mcp_envs.items()):
            lines.append(f"mcp/{server_name}:")
            for k in sorted(server_env.keys()):
                lines.append(_format_one(k, server_env.get(k, "")))
        return "\n".join(lines)


class ConfigEnvRemoveTool(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    name = "config_env_remove"
    category: ClassVar[str] = "Config"
    _subagent_skip = True
    description = "Remove an env var from slife.json5. Does NOT touch the OS keyring."
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Env var name to remove."},
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]
        raw = read_config(self._config_path)
        env = _env_section(raw)
        if key not in env:
            return f"'{key}' is not in slife.json5 — nothing to remove."
        del env[key]
        write_config(self._config_path, raw)
        logger.info("env_removed key=%s", key)
        return f"[OK] Removed '{key}' from slife.json5."


class NativeToolSet(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    name = "native_tool_set"
    category: ClassVar[str] = "Config"
    _subagent_skip = True
    description = "Enable or disable a built-in tool. Takes effect after restart."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Tool name, from list_tools."},
            "enabled": {"type": "boolean", "description": "Enable or disable."},
        },
        "required": ["name", "enabled"],
    }

    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]
        enabled: bool = kwargs["enabled"]
        raw = read_config(self._config_path)
        _toggle_native_enabled(raw, name, enabled)
        write_config(self._config_path, raw)
        state = "enabled" if enabled else "disabled"
        logger.info("native_tool_set name=%s enabled=%s", name, enabled)
        return f"[OK] Native tool '{name}' {state}. Restart for the change to take effect."


import logging
from typing import TYPE_CHECKING, ClassVar

from slife.tools._config_io import _ConfigPathMixin, read_config, write_config
from slife.tools.base import Tool

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


# ── List Models ──────────────────────────────────────────────────────


class ListModelsTool(_ConfigPathMixin, Tool):
    """List all configured LLM models grouped by provider."""

    name: ClassVar[str] = "list_models"
    category: ClassVar[str] = "Config"
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

    name: ClassVar[str] = "add_model"
    category: ClassVar[str] = "Config"
    _subagent_skip: ClassVar[bool] = True
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

    name: ClassVar[str] = "remove_model"
    category: ClassVar[str] = "Config"
    _subagent_skip: ClassVar[bool] = True
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
                    f"Add a model with add_model before continuing."
                )

        write_config(self._config_path, raw)
        logger.info("model_removed ref=%s", ref)
        return f"[OK] Removed `{ref}`."


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


# ── Switch Model ─────────────────────────────────────────────────────


class SwitchModelTool(_ConfigPathMixin, Tool):
    """Switch the active model.

    Updates the config file AND the in-memory Config object so the
    AgentService, LLM client, and TUI status bar all reflect the new
    model without a restart.
    """

    name: ClassVar[str] = "switch_model"
    category: ClassVar[str] = "Config"
    _subagent_skip: ClassVar[bool] = True
    description: ClassVar[str] = (
        "Switch the active LLM model.  The new model takes effect on the "
        "next turn.  Use list_models to see available models and their refs."
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
    def from_config(cls, cfg: dict, config: "Config | None"):
        return cls(config_path=config._path if config else None, config=config)

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
            return f"Error: model '{ref}' not found in config. Use list_models."

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
