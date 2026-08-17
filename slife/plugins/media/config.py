"""Media plugin config — parses the ``media:`` section of slife.json5.

The media plugin owns its config section entirely (the main ``Config``
parser ignores unknown top-level sections).  Shape::

    media: {
      defaults: { image: "provider/model", video: "provider/model", ... },
      providers: {
        <provider_id>: {
          api: "dashscope-aigc",          # wire adapter (see adapters/)
          base_url: "https://.../api/v1",
          api_key: "${ENV_VAR}",
          models: [
            { model: "wan2.7-image", kind: "image" },
            { model: "happyhorse-1.1-t2v", kind: "video",
              params: { resolution: "720P", ratio: "16:9", duration: 5 } },
            { model: "qwen-audio-3.0-tts-plus", kind: "tts", voice: "..." },
            { model: "qwen-audio-3.0-asr-flash", kind: "asr" },
          ],
        },
      },
    }

``kind`` is the capability (image / video / tts / asr); ``api`` is the
wire adapter.  ``${VAR}`` references are resolved via ``slife.env``
(shell env → credstore → default).  A provider whose api_key cannot be
resolved is skipped with a warning, not fatal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from slife.env import resolve_env
from slife.paths import get_config_path
from slife.tools._config_io import ConfigParseError, read_config

logger = logging.getLogger(__name__)

#: Capability kinds the plugin understands.
KNOWN_KINDS = frozenset({"image", "video", "tts", "asr"})


class MediaConfigError(Exception):
    """Config parsing / model-resolution failure (surfaced to the LLM)."""


@dataclass(frozen=True)
class ModelEntry:
    model: str
    kind: str
    params: dict = field(default_factory=dict)
    voice: str = ""


@dataclass(frozen=True)
class ProviderConfig:
    api: str
    base_url: str
    api_key: str
    models: list[ModelEntry] = field(default_factory=list)


@dataclass(frozen=True)
class MediaConfig:
    defaults: dict[str, str] = field(default_factory=dict)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.providers

    def kinds_available(self) -> set[str]:
        return {m.kind for p in self.providers.values() for m in p.models}

    def resolve_model(
        self, kind: str, model_ref: str | None = None,
    ) -> tuple[str, ProviderConfig, ModelEntry]:
        """Resolve a capability model.

        Args:
            kind: Capability kind (image / video / tts / asr).
            model_ref: Optional "provider/model" ref or bare model name.
                When omitted, falls back to ``defaults[kind]``, then to the
                first configured model of that kind.

        Returns:
            ``(provider_id, provider_config, model_entry)``.

        Raises:
            MediaConfigError: Nothing configured for the kind, or the ref
                does not match any configured model.
        """
        if model_ref:
            pid, entry = self._find_ref(model_ref)
            if entry.kind != kind:
                raise MediaConfigError(
                    f"Model '{model_ref}' is kind '{entry.kind}', "
                    f"not '{kind}'."
                )
            return pid, self.providers[pid], entry

        ref = self.defaults.get(kind)
        if ref:
            pid, entry = self._find_ref(ref)
            if entry.kind == kind:
                return pid, self.providers[pid], entry
            logger.warning(
                "media_default_kind_mismatch kind=%s ref=%s entry_kind=%s",
                kind, ref, entry.kind,
            )

        for pid, provider in self.providers.items():
            for entry in provider.models:
                if entry.kind == kind:
                    return pid, provider, entry

        raise MediaConfigError(
            f"No {kind} model configured. Add a model with kind: "
            f"\"{kind}\" to the media: section of slife.json5."
        )

    def _find_ref(self, ref: str) -> tuple[str, ModelEntry]:
        if "/" in ref:
            pid, _, model_name = ref.partition("/")
            provider = self.providers.get(pid)
            if provider is None:
                raise MediaConfigError(
                    f"Unknown media provider '{pid}'. Configured: "
                    f"{', '.join(sorted(self.providers)) or '(none)'}."
                )
            for entry in provider.models:
                if entry.model == model_name:
                    return pid, entry
            raise MediaConfigError(
                f"Unknown model '{ref}'. Provider '{pid}' has: "
                f"{', '.join(m.model for m in provider.models)}."
            )
        # Bare model name — first match across providers.
        for pid, provider in self.providers.items():
            for entry in provider.models:
                if entry.model == ref:
                    return pid, entry
        raise MediaConfigError(
            f"Unknown model '{ref}'. Use 'provider/model' or a configured "
            f"model name."
        )


def _parse_models(raw_models: object) -> list[ModelEntry]:
    entries: list[ModelEntry] = []
    if not isinstance(raw_models, list):
        return entries
    for m in raw_models:
        if not isinstance(m, dict) or not m.get("model") or not m.get("kind"):
            logger.warning("media_model_skip entry=%r (needs model + kind)", m)
            continue
        kind = str(m["kind"])
        if kind not in KNOWN_KINDS:
            logger.warning(
                "media_model_skip model=%s unknown kind=%s", m["model"], kind,
            )
            continue
        params = m.get("params")
        entries.append(ModelEntry(
            model=str(m["model"]),
            kind=kind,
            params=dict(params) if isinstance(params, dict) else {},
            voice=str(m.get("voice") or ""),
        ))
    return entries


def load_media_config() -> MediaConfig:
    """Read the ``media:`` section from slife.json5.

    Missing section / parse failure / unresolved env vars all degrade to
    an empty config (logged) — the tools then report a config error to
    the LLM instead of crashing the plugin.
    """
    try:
        raw = read_config(get_config_path())
    except ConfigParseError as e:
        logger.warning("media_config_parse_error err=%s", e)
        return MediaConfig()

    media_raw = raw.get("media")
    if not isinstance(media_raw, dict) or not media_raw:
        return MediaConfig()

    defaults_raw = media_raw.get("defaults")
    defaults = {
        str(k): str(v)
        for k, v in (defaults_raw or {}).items()
        if isinstance(v, str)
    } if isinstance(defaults_raw, dict) else {}

    providers: dict[str, ProviderConfig] = {}
    providers_raw = media_raw.get("providers")
    if isinstance(providers_raw, dict):
        for pid, praw in providers_raw.items():
            if not isinstance(praw, dict):
                logger.warning("media_provider_skip provider=%s (not a dict)", pid)
                continue
            try:
                resolved = resolve_env(praw)
            except KeyError as e:
                logger.warning(
                    "media_provider_skip provider=%s unresolved_env=%s", pid, e,
                )
                continue
            models = _parse_models(resolved.get("models"))
            if not models:
                logger.warning("media_provider_skip provider=%s (no valid models)", pid)
                continue
            providers[str(pid)] = ProviderConfig(
                api=str(resolved.get("api") or ""),
                base_url=str(resolved.get("base_url") or "").rstrip("/"),
                api_key=str(resolved.get("api_key") or ""),
                models=models,
            )

    cfg = MediaConfig(defaults=defaults, providers=providers)
    logger.info(
        "media_config_loaded providers=%d kinds=%s",
        len(providers), sorted(cfg.kinds_available()),
    )
    return cfg
