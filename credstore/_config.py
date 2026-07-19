"""credstore config file support.

Looks for ``credstore.json5`` in:
  1. Current directory (``./credstore.json5``)
  2. Home directory (``~/.credstore/config.json5``)

Format::

    {
      // Path to the encrypted credential file
      cryptfile_path: "credentials.crypt",
    }
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("credstore")

_DEFAULT_CONFIG_FILES = [
    Path("credstore.json5"),
    Path.home() / ".credstore" / "config.json5",
]


def load_config() -> dict:
    """Load credstore config from the first found config file.

    Returns a dict with config keys, or empty dict if none found.
    """
    for path in _DEFAULT_CONFIG_FILES:
        if path.exists():
            try:
                import json5
                raw = json5.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    logger.debug("config_loaded path=%s", path)
                    return raw
            except Exception as exc:
                logger.debug("config_parse_failed path=%s err=%s", path, exc)
    return {}


def _is_slife_dev() -> bool:
    """Check whether we're running from the slife source tree.

    Reads ``pyproject.toml`` in CWD and checks that ``[project] name``
    equals ``"slife"``.
    """
    try:
        import tomllib
        data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        return data.get("project", {}).get("name") == "slife"
    except Exception:
        return False


def get_cryptfile_path() -> str:
    """Resolve the cryptfile path.

    Priority:
      1. ``CREDSTORE_FILE`` env var
      2. ``cryptfile_path`` in credstore.json5 config
      3. ``~/.credstore/credentials.crypt`` (production) or
         ``./credentials.crypt`` (dev — when CWD contains slife's pyproject.toml)
    """
    # 1. Env var
    env_path = os.environ.get("CREDSTORE_FILE")
    if env_path:
        return env_path

    # 2. Config file
    cfg = load_config()
    cfg_path = cfg.get("cryptfile_path")
    if cfg_path:
        return str(Path(cfg_path).expanduser())

    # 3. Default
    if _is_slife_dev():
        return str(Path("credentials.crypt"))
    return str(Path.home() / ".credstore" / "credentials.crypt")
