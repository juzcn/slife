"""cc-config — generate ~/.claude/settings.json from saved provider/model configs.

A standalone CLI (mirroring the credstore pattern) that keeps the
non-secret *shape* of a Claude Code provider setup in
``~/.claude/cc-config.json`` and materialises it into
``~/.claude/settings.json``.  Secrets never touch settings.json — the
API key is read from credstore at activate time and injected only into
the system environment as ``ANTHROPIC_AUTH_TOKEN``.

Modules::

    cc_config      Package API (get/save/remove/list/activate)
    cc_config.cli  Command-line interface (entry point ``cc-config``)
"""

from cc_config._api import (
    CONFIG_PATH,
    add_provider,
    list_providers,
    remove_provider,
    load_config,
    save_config,
    update_provider,
)
from cc_config._defaults import (
    DEFAULT_ENV,
    DEFAULT_OVERRIDE_KEYS,
    DEFAULT_SETTINGS,
    MAIN_MODEL_SLOT_KEYS,
    default_value,
    list_default_override_keys,
)

__version__ = "0.1.0"

__all__ = [
    "CONFIG_PATH",
    "DEFAULT_ENV",
    "DEFAULT_OVERRIDE_KEYS",
    "DEFAULT_SETTINGS",
    "MAIN_MODEL_SLOT_KEYS",
    "add_provider",
    "default_value",
    "list_default_override_keys",
    "list_providers",
    "load_config",
    "remove_provider",
    "save_config",
    "update_provider",
]