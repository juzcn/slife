"""USER.md read-merge-append — the plugin-internal store for standing user
preferences.

``USER.md`` is a plain markdown file in the per-agent File Cabinet directory,
hand-edited directly by the user and appended to by the LLM-visible
``add_user_pref`` native tool.  The plugin process is its only host, so every
writer — the main agent, subagents, and the user's own editor — serialises
through one read-modify-write; the main process never touches the file
directly.

The merge is deterministic and structure-preserving:

  - existing lines are kept byte-for-byte (a hand-edited file is never
    renumbered, reworded or reflowed);
  - the new item is inserted after the LAST existing item line, in the file's
    own style (``N.`` continues the numbering, otherwise a ``- `` bullet);
  - a normalized duplicate — or substantial containment — is a no-op append;
  - a file with no item lines gets the item appended after a blank separator.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The reserved cabinet filename holding the user's standing preferences.
USER_PREFS_FILENAME = "USER.md"

_NUMBERED_ITEM_RE = re.compile(r"^(\d+)\.\s+(.*)$")
_BULLET_ITEM_RE = re.compile(r"^[-\*]\s+(.*)$")


def user_prefs_path(memfiles_dir: Path) -> Path:
    """Resolve the USER.md path inside a *memfiles_dir*."""
    return memfiles_dir / USER_PREFS_FILENAME


def _normalize(text: str) -> str:
    """Comparable item form for dedupe: lowered, list marker and non-word
    punctuation stripped, single-spaced.  ``\\w`` keeps CJK letters."""
    s = re.sub(r"^\s*(?:\d+\.|[-\*])\s+", "", text).strip().lower()
    s = re.sub(r"[^\w\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _is_duplicate(norm_new: str, norm_existing: str) -> bool:
    """True when *norm_new* duplicates *norm_existing*.

    Containment only counts past a minimum length — short common phrases
    (e.g. "reply in English") would otherwise falsely absorb unrelated items.
    """
    if norm_new == norm_existing:
        return True
    if len(norm_new) >= 15 and norm_new in norm_existing:
        return True
    if len(norm_existing) >= 15 and norm_existing in norm_new:
        return True
    return False


def append_preference(current: str, preference: str) -> tuple[str, dict]:
    """Merge one *preference* into *current* USER.md text.

    Returns ``(new_text, info)``.  *info* carries ``appended`` (bool),
    ``duplicate`` (bool), ``error`` (str, when the preference is empty),
    ``item`` (the inserted line), ``items`` (the post-write item count) and
    ``chars`` (the post-write length).
    """
    preference = (preference or "").strip()
    if not preference:
        return current, {"appended": False, "duplicate": False,
                         "error": "preference is required"}

    lines = current.splitlines()
    items: list[tuple[int, int | None, str]] = []  # (line_idx, number_or_None, body)
    max_num = 0
    has_bullet = False
    for i, line in enumerate(lines):
        m = _NUMBERED_ITEM_RE.match(line)
        if m:
            num = int(m.group(1))
            max_num = max(max_num, num)
            items.append((i, num, m.group(2)))
            continue
        b = _BULLET_ITEM_RE.match(line)
        if b:
            has_bullet = True
            items.append((i, None, b.group(1)))

    norm_new = _normalize(preference)
    for _, _, body in items:
        norm_body = _normalize(body)
        if norm_body and _is_duplicate(norm_new, norm_body):
            return current, {
                "appended": False, "duplicate": True,
                "items": len(items), "chars": len(current),
                "preference": preference,
            }

    if max_num:
        new_line = f"{max_num + 1}. {preference}"
    elif has_bullet:
        new_line = f"- {preference}"
    else:
        new_line = f"1. {preference}"

    if not items:
        # Empty file, or only non-item content (title / prose) — the item
        # appends after a blank separator so it never glues to the last line.
        # A truly empty file simply starts with the item.
        out = lines.copy()
        while out and not out[-1].strip():
            out.pop()
        if out:
            out.append("")
        out.append(new_line)
    else:
        insert_at = items[-1][0] + 1
        out = lines[:insert_at] + [new_line] + lines[insert_at:]

    new_text = "\n".join(out)
    info = {
        "appended": True,
        "duplicate": False,
        "item": new_line,
        "items": len(items) + 1,
        "chars": len(new_text),
    }
    return new_text, info