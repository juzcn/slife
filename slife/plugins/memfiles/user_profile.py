"""The USER.md store — where the user's standing profile lives.

``USER.md`` is a plain markdown file in the per-agent File Cabinet directory,
hand-edited directly by the user and rewritten by the LLM-visible
``profile_edit`` builtin tool.  It is a DOCUMENT, not a record store: no
keying, no per-item id, no merge contract.  A line is a line someone wrote,
and changing one is an edit to the text that was read back — which is why
nothing here parses or merges the file.

The memfiles plugin is the file's only host, so every writer — the main agent,
subagents, and the user's own editor — serialises through one lock; the main
process never touches the file directly.
"""

from __future__ import annotations

from pathlib import Path

#: The reserved cabinet filename holding the user's standing profile.
USER_PROFILE_FILENAME = "USER.md"


def user_profile_path(memfiles_dir: Path) -> Path:
    """Resolve the USER.md path inside a *memfiles_dir*."""
    return memfiles_dir / USER_PROFILE_FILENAME
