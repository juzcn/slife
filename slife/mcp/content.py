"""One reader for an MCP ``CallToolResult``: the SDK's blocks → a string.

The two transports — :class:`~slife.plugins.mcp_gateway.client.MCPClient`
(plugin children and the wrapper) and
:class:`~slife.plugins.mcp_gateway.connection.MCPServerConnection` (pooled
external servers) — each used to spell this walk out for themselves.  They had
already drifted: one read ``ImageContent.data`` as raw bytes when the SDK types
it as base64 text, so its image branch could never fire, and only the other
recognised an unknown block type richly.  A tool result is a tool result
whichever transport carried it, so the walk lives here once.

The one thing that legitimately differs is what to do with binary content, and
that is the caller's policy (`save_image`), not a second copy of the walk.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def format_content_blocks(
    result: Any,
    *,
    save_image: Callable[[str | bytes], str | None] | None = None,
    dump_on_empty: bool = False,
) -> str:
    """Flatten *result*'s content into the string a slife tool returns.

    ``save_image`` materializes a binary block to a file and returns its path;
    when it is absent, or declines the payload, the block is described in text
    instead (the payload is what the model cannot read, but its type and size
    still say what came back).  ``dump_on_empty`` returns the whole result as
    JSON when there is no content at all, which is how a server that answers
    with only ``structuredContent`` still reaches the model.

    An ``is_error`` result becomes ``Error: …`` — the prefix every slife
    consumer judges failure by.
    """
    if getattr(result, "is_error", False):
        return "Error: " + "\n".join(
            str(block.text) for block in result.content if hasattr(block, "text")
        )

    parts: list[str] = []
    for block in result.content:
        if hasattr(block, "text"):
            parts.append(str(block.text))
        elif hasattr(block, "data"):
            parts.append(_binary_block(block, save_image))
        else:
            parts.append(_unknown_block(block))
    if parts:
        return "\n".join(parts)
    return json.dumps(result.model_dump()) if dump_on_empty else ""


def _binary_block(block: Any, save_image) -> str:
    """A binary block as a file path when it can be materialized, else as text."""
    if save_image is not None:
        path = save_image(block.data)
        if path is not None:
            return str(path)
    size = len(block.data)
    mime = getattr(block, "mime_type", "")
    # A non-string ``mime_type`` is not a mime type (a mock, or a malformed
    # block) — describe the payload by its size alone rather than print it.
    if isinstance(mime, str) and mime:
        return f"[image: {mime} {size} bytes]"
    return f"[binary data: {size} bytes]"


def _unknown_block(block: Any) -> str:
    """A block type this walk does not know — keep its structure, not its repr."""
    try:
        return block.model_dump_json()
    except Exception:
        return str(block)
