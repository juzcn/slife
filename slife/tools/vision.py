"""Vision tools — help the LLM feed images to multimodal models.

``include_image`` is a helper for building the image content block a
vision model sees.  It takes a local file path or HTTPS URL and injects
the resulting image block into the active conversation (works exactly
like the ``@`` syntax in chat).

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
    """Include an image for the LLM to process with vision.

    Takes a local file path or HTTPS URL and makes the image visible
    to the vision model.  Works exactly like the ``@`` syntax in chat.
    """

    name: ClassVar[str] = "include_image"
    category: ClassVar[str] = "Vision"
    _requires_vision: ClassVar[bool] = True
    description: ClassVar[str] = (
        "Include an image for vision processing. "
        "Pass a local file path or HTTPS URL. Works like @ syntax."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": (
                    "Local file path (e.g. 'D:\\Downloads\\photo.jpg') "
                    "or HTTPS URL (e.g. 'https://example.com/photo.jpg')."
                ),
            },
        },
        "required": ["source"],
    }

    async def execute(self, **kwargs) -> str:
        from slife.agent.multimodal import include_image_url
        source: str = kwargs["source"]
        block = include_image_url(source)
        if block is None:
            return f"Error: cannot read image — {source}"

        ctx = getattr(self, "_ctx", None)
        conv = ctx.conversation if ctx is not None else None
        if conv is not None:
            conv.inject_images_to_last_user([block])

        return f"Image included: {source}"
