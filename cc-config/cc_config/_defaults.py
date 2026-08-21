"""Default settings template for the generated ``~/.claude/settings.json``.

The values here match the stock Claude Code defaults (mirrored from the
user's existing settings.json) and are used for every model slot that is
not explicitly overridden.  ``activate --custom`` walks the override
keys in :data:`DEFAULT_OVERRIDE_KEYS`; plain ``activate`` uses these
values unchanged.

:data:`DEFAULT_ENV` is exported for tests and for building the
settings dict programmatically.
"""

from __future__ import annotations

# The main model (ANTHROPIC_MODEL) is chosen on the command line as
# ``provider/model`` and is intentionally NOT here — ``--custom`` must
# never change it.  These are the remaining env slots the user may tweak.
DEFAULT_OVERRIDE_KEYS = [
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_CLAUDE_CODE_SUBAGENT_MODEL",
    "ANTHROPIC_CLAUDE_CODE_EFFORT_LEVEL",
]

# Stock Claude Code env defaults.  A model that does not speak the
# effort-level dialect (e.g. a pure chat model) can override this per
# provider via the ``extra_env`` field stored in cc-config.json.
DEFAULT_ENV: dict[str, str] = {
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "",
    "ANTHROPIC_CLAUDE_CODE_SUBAGENT_MODEL": "",
    "ANTHROPIC_MODEL": "",
    "ANTHROPIC_CLAUDE_CODE_EFFORT_LEVEL": "medium",
}

# Frozen template for the settings.json body — never a credential here.
DEFAULT_SETTINGS = {
    "env": {},
    "autoUpdatesChannel": "latest",
}

# Static defaults used when ``activate`` runs without --custom.
DEFAULT_TEMPLATE = {
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "",
    "ANTHROPIC_CLAUDE_CODE_SUBAGENT_MODEL": "",
    "ANTHROPIC_MODEL": "",
    "ANTHROPIC_CLAUDE_CODE_EFFORT_LEVEL": "medium",
}


def list_default_override_keys() -> list[str]:
    """Return the env keys that ``activate --custom`` prompts for, in order."""
    return list(DEFAULT_OVERRIDE_KEYS)
