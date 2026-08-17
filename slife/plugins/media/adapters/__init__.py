"""Adapter registry — maps the config ``api`` value to an adapter class.

``api`` is the wire protocol; ``kind`` (on each model entry) is the
capability.  Adding a provider family = one adapter class + one entry
here; nothing else in the plugin changes.
"""

from slife.plugins.media.adapters.base import (
    ArtifactSaver,
    MediaAdapter,
    MediaAdapterError,
)
from slife.plugins.media.adapters.dashscope_aigc import DashScopeAIGCAdapter
from slife.plugins.media.adapters.openai_compat import OpenAICompatAdapter
from slife.plugins.media.config import MediaConfigError, ProviderConfig

__all__ = [
    "ADAPTER_REGISTRY",
    "create_adapter",
    "ArtifactSaver",
    "MediaAdapter",
    "MediaAdapterError",
    "DashScopeAIGCAdapter",
    "OpenAICompatAdapter",
    "MediaConfigError",
    "ProviderConfig",
]

ADAPTER_REGISTRY: dict[str, type] = {
    "dashscope-aigc": DashScopeAIGCAdapter,
    "openai-images": OpenAICompatAdapter,
}


def create_adapter(provider: ProviderConfig) -> MediaAdapter:
    """Instantiate the adapter for a provider config.

    Raises:
        MediaConfigError: Unknown ``api`` value.
    """
    cls = ADAPTER_REGISTRY.get(provider.api)
    if cls is None:
        raise MediaConfigError(
            f"Unknown media api '{provider.api}'. Known adapters: "
            f"{', '.join(sorted(ADAPTER_REGISTRY))}."
        )
    return cls(provider)