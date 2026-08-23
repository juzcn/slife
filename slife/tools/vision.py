"""Vision tools — help the LLM feed images to multimodal models.

``include_image`` is a helper for building the image content blocks a
vision model sees.  It takes one or more image sources — data URIs,
local file paths, or HTTP(S) URLs — and injects the resulting blocks
into the active conversation (works exactly like the ``@`` syntax in
chat).  It feeds the vision API only — nothing is ever rendered in the
terminal.  To *show* a file to the user, hand them a path / URL (opened
with the OS) or publish it via ``memfiles__expose_file``.

This is an agent-loop concern (it mutates in-conversation state), so it
stays a native tool in the main process — unrelated to the memfiles
plugin, which only handles file storage and public URL sharing.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from slife.tools.base import Tool

logger = logging.getLogger(__name__)


class IncludeImageTool(Tool):
    """Include one or more images for the LLM to process with vision.

    Takes data URIs, local file paths, or HTTP(S) URLs and makes them
    visible to the vision model.  Works exactly like the ``@`` syntax
    in chat.  Pass a list via ``sources`` for multiple images in one
    call (a single one also works via ``source``).
    """

    name: ClassVar[str] = "include_image"
    category: ClassVar[str] = "Vision"
    _requires_vision: ClassVar[bool] = True
    description: ClassVar[str] = (
        "Include one or more images for vision processing. "
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

        conv = ctx.conversation if ctx is not None else None
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
                raise ValueError("include_image requires 'sources' or 'source'")
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
