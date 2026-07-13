"""Slash-command registry and completion logic.

Defines available slash commands and provides matching/completion
for both command names and file paths (for /file).
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SlashCommand:
    """A slash command available in the input."""

    name: str         # "/file"
    description: str  # "Attach an image for vision models"
    usage: str = ""   # "/file <path>" — shown as hint


# ── Command registry ──────────────────────────────────────────────────

COMMANDS: list[SlashCommand] = [
    SlashCommand(
        name="/file",
        description="Attach an image for vision models",
        usage="/file <path>",
    ),
    SlashCommand(
        name="/exit",
        description="Exit slife",
        usage="/exit",
    ),
]


def match_commands(prefix: str) -> list[SlashCommand]:
    """Return commands whose name starts with prefix (case-insensitive).

    Args:
        prefix: Current input value (e.g. "/f", "/file", "/nonexist").

    Returns:
        Matching commands, or all commands if prefix is just "/".
    """
    if not prefix.startswith("/"):
        return []
    if prefix == "/":
        return list(COMMANDS)
    lower = prefix.lower()
    return [c for c in COMMANDS if c.name.lower().startswith(lower)]


# ── File path completion (for /file) ──────────────────────────────────

def _glob_paths(pattern: str) -> list[str]:
    """Return files/dirs matching pattern, sorted: dirs first, then files."""
    try:
        base = Path(pattern)
        if not pattern:
            matches = list(Path(".").iterdir())
        elif base.is_absolute():
            parent = base.parent if not pattern.endswith(("*", "?")) else base
            glob_pattern = str(base) if pattern.endswith(("*", "?")) else f"{pattern}*"
            matches = list(Path(".").glob(glob_pattern))
        else:
            # Relative path — glob from cwd
            if pattern.endswith("/"):
                matches = list(Path(pattern).glob("*")) if Path(pattern).is_dir() else []
            else:
                matches = list(Path(".").glob(f"{pattern}*"))

        # Sort: directories first, then files, alphabetical within each
        dirs = sorted([p for p in matches if p.is_dir()], key=lambda p: p.name.lower())
        files = sorted([p for p in matches if p.is_file()], key=lambda p: p.name.lower())
        return [str(p) + ("/" if p.is_dir() else "") for p in dirs + files]
    except Exception:
        return []


def complete_file_path(partial: str) -> list[str]:
    """Return file path completions for a partial path after /file.

    Args:
        partial: The path text after "/file " (may be empty).

    Returns:
        List of matching paths, limited to 20 entries.
    """
    partial = partial.strip()
    return _glob_paths(partial)[:20]
