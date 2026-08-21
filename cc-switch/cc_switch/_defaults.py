"""Default settings template for the generated ``~/.claude/settings.json``.

The Claude Code convention: the other model slots
(``ANTHROPIC_DEFAULT_HAIKU_MODEL`` / ``_SONNET_`` / ``_OPUS_`` /
``CLAUDE_CODE_SUBAGENT_MODEL``) default to the **main model** picked on
the command line (``ANTHROPIC_MODEL``), and are never left empty.
``activate --custom`` starts each of those slots at the main model so the
user can change them individually.

Only the non-model slots have a static default
(``ANTHROPIC_CLAUDE_CODE_EFFORT_LEVEL: max``).
"""

from __future__ import annotations

# The model slots that default to the main model (ANTHROPIC_MODEL).
# ANTHROPIC_MODEL itself is NOT here — it is chosen on the command line.
MAIN_MODEL_SLOT_KEYS = [
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_CLAUDE_CODE_SUBAGENT_MODEL",
]

# Env keys that ``activate --custom`` prompts for, in order.
DEFAULT_OVERRIDE_KEYS = list(MAIN_MODEL_SLOT_KEYS) + [
    "ANTHROPIC_CLAUDE_CODE_EFFORT_LEVEL",
]

# Static default for the non-model slots.
DEFAULT_ENV: dict[str, str] = {
    "ANTHROPIC_CLAUDE_CODE_EFFORT_LEVEL": "max",
}

# Frozen template for the settings.json body — never a credential here.
DEFAULT_SETTINGS = {
    "env": {},
    "autoUpdatesChannel": "latest",
}


def default_value(key: str, main_model: str) -> str:
    """Return the default for an env key given the main model.

    Model slots inherit the main model; non-model slots use their static
    default.
    """
    if key in MAIN_MODEL_SLOT_KEYS:
        return main_model
    return DEFAULT_ENV[key]


def list_default_override_keys() -> list[str]:
    """Return the env keys that ``activate --custom`` prompts for, in order."""
    return list(DEFAULT_OVERRIDE_KEYS)