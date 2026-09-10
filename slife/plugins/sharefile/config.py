"""sharefile config — load ``sharefile.json5``, pick the active tunnel provider.

Path precedence (mirrors mcp-plugin's resolver):
  1. ``$SHAREFILE_FILE`` — explicit override (a test/dev escape hatch only)
  2. slife data dir — ``<data_dir>/sharefile.json5`` via
     :func:`slife.paths.get_data_dir` (production ``~/.slife/sharefile.json5``,
     the checkout root in dev).  sharefile is a built-in slife plugin, so its
     config sits next to ``slife.json5``; the harness exports **no** per-file
     env var — the plugin child inherits ``$SLIFE_DATA_DIR`` and resolves the
     same directory itself (see ``slife/config.py`` on why MCP_PLUGIN_FILE /
     LOCAL_EMBED_FILE are deliberately not set either).

Config shape::

    {
      active_provider: "ngrok",
      providers: {
        ngrok: {},
        "localhost.run": { ssh: "ssh", host: "localhost.run", user: "nokey",
                           remote_port: 80 },
      },
    }

Exactly one provider is active; the rest stay configured and inert.  Provider
options support ``${VAR}`` / ``${VAR:-default}``, resolved through
``slife.env.resolve_env`` (shell env → credstore → literal).

A config that is missing, unparseable, or names an unknown provider degrades to
``ngrok`` rather than raising: a tunnel provider is a *subordinate* dependency
(PLUGIN_CONTRACT.md) — it must never keep the plugin from loading.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from slife.env import resolve_env
from slife.plugins.sharefile.providers import DEFAULT_PROVIDER, KNOWN_PROVIDERS
from slife.tools._config_io import ConfigParseError, read_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SharefileConfig:
    """The parsed config — the active provider's name plus every provider's options."""

    active_provider: str = DEFAULT_PROVIDER
    providers: dict[str, dict] = field(default_factory=dict)

    def options_for(self, name: str) -> dict:
        """Options for *name*.

        ``{}`` when the provider carries no entry — every provider must work
        from its own defaults, so an unconfigured active provider still works
        (that is what makes ``ngrok: {}`` a valid entry).
        """
        opts = self.providers.get(name)
        return dict(opts) if isinstance(opts, dict) else {}


def default_config_path() -> Path:
    """Default config path: ``<slife data dir>/sharefile.json5``.

    ``get_data_dir()`` honours ``$SLIFE_DATA_DIR``, which the host exports so
    plugin children resolve the same directory as the main process.
    """
    from slife.paths import get_data_dir

    return get_data_dir() / "sharefile.json5"


def resolve_config_path() -> Path:
    """Return the sharefile.json5 path for this process.

    ``$SHAREFILE_FILE`` (test/dev override) > slife data dir default.
    """
    env = os.environ.get("SHAREFILE_FILE")
    if env:
        return Path(env).expanduser()
    return default_config_path()


def load_sharefile_config(path: Path | None = None) -> SharefileConfig:
    """Read ``sharefile.json5`` and resolve the active provider.

    Degrades to :data:`DEFAULT_PROVIDER` — never raises — when the file is
    absent, cannot be parsed, or names a provider this build does not have.
    A single provider entry with an unresolvable ``${VAR}`` is dropped with a
    warning; the others still load (mirrors ``media/config.py``).
    """
    if path is None:
        path = resolve_config_path()

    try:
        raw = read_config(path)
    except ConfigParseError as e:
        # read_config already logged the parse failure; a broken config must
        # not take the plugin down with it.
        logger.warning(
            "sharefile_config_fallback reason=parse_error provider=%s err=%s",
            DEFAULT_PROVIDER, e,
        )
        return SharefileConfig()

    providers: dict[str, dict] = {}
    raw_providers = raw.get("providers")
    if isinstance(raw_providers, dict):
        for name, entry in raw_providers.items():
            if not isinstance(entry, dict):
                logger.warning("sharefile_provider_skip provider=%s not_a_dict", name)
                continue
            try:
                providers[str(name)] = dict(resolve_env(entry))
            except KeyError as e:
                # An unresolved ${VAR} in one provider must not sink the rest.
                logger.warning(
                    "sharefile_provider_skip provider=%s unresolved_env=%s", name, e,
                )
                continue

    active = raw.get("active_provider")
    active = str(active) if isinstance(active, str) and active.strip() else DEFAULT_PROVIDER
    if active not in KNOWN_PROVIDERS:
        logger.warning(
            "sharefile_active_provider_unknown provider=%s fallback=%s known=%s",
            active, DEFAULT_PROVIDER, sorted(KNOWN_PROVIDERS),
        )
        active = DEFAULT_PROVIDER

    logger.info(
        "sharefile_config_loaded active_provider=%s configured=%s",
        active, sorted(providers),
    )
    return SharefileConfig(active_provider=active, providers=providers)
