"""Configuration for slife agent — OpenClaw-compatible JSON5 format.

Two-level model hierarchy:
  providers:
    <provider-id>:           # connection config (shared)
      base_url, api_key, api
      models:
        - model: "<api-name>"  # API model name, doubles as local id
          name: "<display>"    # human-readable label
          reasoning, input, context_window, max_tokens, ...

Model refs: "provider-id/model-name"
"""

import json5
import logging
from dataclasses import dataclass
from pathlib import Path

from slife.env import resolve_env

logger = logging.getLogger(__name__)


@dataclass
class ModelConfig:
    """Configuration for a single LLM model."""

    ref: str                       # "deepseek/deepseek-v4-flash"
    provider: str                  # "deepseek"
    api_model: str                 # "deepseek-v4-flash" (sent to API)
    display_name: str              # "DeepSeek V4 Flash"
    api_key: str
    base_url: str = "https://api.deepseek.com"
    api: str = "openai-completions"
    supports_vision: bool = False
    max_tokens: int = 4096
    context_window: int = 131072
    temperature: float = 0.7
    top_p: float = 1.0
    thinking_enabled: bool = False
    reasoning_effort: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "ModelConfig":
        """Parse a model entry (OpenClaw field names → internal).

        model: API model name, doubles as local id (e.g. "deepseek-v4-flash")
        name: display label (e.g. "DeepSeek V4 Flash")
        reasoning: true → thinking_enabled
        input: ["text","image"] → supports_vision
        """
        api_model = data["model"]

        # model may contain provider prefix: "deepseek/deepseek-v4-flash"
        if "/" in api_model:
            provider, local_id = api_model.split("/", 1)
        else:
            provider = data.get("provider", "unknown")
            local_id = api_model

        ref = f"{provider}/{local_id}"
        display_name = data.get("name", api_model)
        thinking = data.get("reasoning", data.get("thinking_enabled", False))
        model_input = data.get("input", [])
        supports_vision = "image" in model_input if model_input else data.get(
            "supports_vision", False
        )

        return cls(
            ref=ref,
            provider=provider,
            api_model=api_model,
            display_name=display_name,
            api_key=data["api_key"],
            base_url=data.get("base_url", "https://api.deepseek.com"),
            api=data.get("api", "openai-completions"),
            supports_vision=supports_vision,
            max_tokens=data.get("max_tokens", 4096),
            context_window=data.get("context_window", 131072),
            temperature=data.get("temperature", 0.7),
            top_p=data.get("top_p", 1.0),
            thinking_enabled=bool(thinking),
            reasoning_effort=data.get("reasoning_effort"),
        )


@dataclass
class Config:
    """Top-level configuration for slife."""

    models: list[ModelConfig]
    active_model_ref: str
    tools: list[dict]
    max_iterations: int = 10
    system_prompt: str = (
        "You are slife, a helpful AI assistant with access to tools. "
        "Use web_search to find current information from the web. "
        "Use execute_shell to run shell commands on the user's system. "
        "Think step by step and use tools when needed. "
        "When you have enough information, answer the user directly."
    )

    @property
    def active_model(self) -> ModelConfig:
        """Return the currently active model configuration."""
        for m in self.models:
            if m.ref == self.active_model_ref:
                return m
        raise KeyError(
            f"Active model '{self.active_model_ref}' not found. "
            f"Available: {[m.ref for m in self.models]}"
        )

    @classmethod
    def from_json5(cls, path: str | Path = "slife.json5") -> "Config":
        """Load from JSON5 file with provider→model hierarchy."""
        path = Path(path)
        logger.info("Loading config from %s", path)
        if not path.exists():
            raise FileNotFoundError(
                f"Config file not found: {path}\n"
                f"Copy slife.json5.example → slife.json5 and edit it."
            )

        raw = json5.loads(path.read_text(encoding="utf-8"))
        models_section = raw.get("models", {})

        all_models: list[ModelConfig] = []
        providers: dict = {}

        if isinstance(models_section, dict):
            providers = models_section.get("providers", {})

            for provider_id, provider_cfg in providers.items():
                provider_cfg = resolve_env(provider_cfg)
                base_url = provider_cfg.get("base_url", "")
                api_key = provider_cfg.get("api_key", "")
                api = provider_cfg.get("api", "openai-completions")

                seen_ids: set[str] = set()

                for m in provider_cfg.get("models", []):
                    m = resolve_env(m)
                    m.setdefault("api_key", api_key)
                    m.setdefault("base_url", base_url)
                    m.setdefault("api", api)
                    m.setdefault("provider", provider_id)

                    # The 'model' field is both the id and the API model name
                    model_name = m["model"]
                    local_id = model_name.split("/", 1)[-1]

                    if local_id in seen_ids:
                        raise ValueError(
                            f"Duplicate model '{local_id}' in provider "
                            f"'{provider_id}'. Model names must be unique "
                            f"within a provider."
                        )
                    seen_ids.add(local_id)

                    all_models.append(ModelConfig.from_dict(m))

        elif isinstance(models_section, list):
            for m in models_section:
                m = resolve_env(m)
                all_models.append(ModelConfig.from_dict(m))

        if not all_models:
            raise ValueError(
                "No models defined. Add models.providers.<id>.models[]."
            )

        logger.info(
            "Parsed %d models across %d providers",
            len(all_models),
            len(providers) if isinstance(models_section, dict) else 0,
        )

        agent = raw.get("agent", {})
        tools = resolve_env(raw.get("tools", []))
        logger.info("Tool entries in config: %d", len(tools))

        return cls(
            models=all_models,
            active_model_ref=raw.get("active_model", all_models[0].ref),
            tools=tools,
            max_iterations=agent.get("max_iterations", 10),
            system_prompt=agent.get(
                "system_prompt", cls.system_prompt
            ).strip(),
        )
