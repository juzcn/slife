"""credstore — resolve the encrypted credential file path.

Priority:
  1. ``CREDSTORE_FILE`` env var
  2. ``~/.credstore/credentials.crypt`` (production) or
     ``./credentials.crypt`` (dev — when CWD contains slife's pyproject.toml)
"""

from __future__ import annotations

import os
from pathlib import Path

_tomllib = None
try:
    import tomllib as _tomllib
except ImportError:
    pass


def is_slife_dev() -> bool:
    """Check whether we're running from the slife source tree.

    Returns True when the CWD contains a ``pyproject.toml`` with
    ``project.name == "slife"``.
    """
    if _tomllib is None:
        return False
    try:
        data = _tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        return data.get("project", {}).get("name") == "slife"
    except Exception:
        return False


__all__ = ["is_slife_dev", "get_cryptfile_path"]


def get_cryptfile_path() -> str:
    """Resolve the cryptfile path.

    Priority:
      1. ``CREDSTORE_FILE`` env var
      2. ``~/.credstore/credentials.crypt`` (production) or
         ``./credentials.crypt`` (dev — when CWD contains slife's pyproject.toml)
    """
    env_path = os.environ.get("CREDSTORE_FILE")
    if env_path:
        return env_path

    if is_slife_dev():
        return str(Path("credentials.crypt"))
    return str(Path.home() / ".credstore" / "credentials.crypt")
