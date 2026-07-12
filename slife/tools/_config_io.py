"""Shared config file read/write helpers.

Used by config_env.py and cli.py to avoid duplicating the same
json5 read/write logic across tool modules.
"""

import json5
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def read_config(path: Path) -> dict:
    """Read and parse a JSON5 config file. Returns an empty dict on failure."""
    try:
        return json5.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Config file not found: %s", path)
        return {}
    except (ValueError, OSError) as e:
        logger.error("Cannot parse config %s: %s", path, e)
        return {}


def write_config(path: Path, raw: dict) -> None:
    """Write a dict to a JSON5 config file with indent=2 formatting."""
    path.write_text(json5.dumps(raw, indent=2, trailing_commas=False), encoding="utf-8")
