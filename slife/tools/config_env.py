"""Config environment variable management tools.

Tools for reading, setting, and removing environment variables in slife.json5's
env: section. Changes are persisted to disk AND injected into os.environ immediately
— no restart needed.
"""

import json5
import logging
import os
from pathlib import Path

from slife.tools.base import Tool

logger = logging.getLogger(__name__)

_PLACEHOLDER_PREFIX = "<YOUR_"


def _read_config(path: Path) -> dict:
    """Read and parse slife.json5. Returns the raw dict (may be empty)."""
    try:
        return json5.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Config file not found: %s", path)
        return {}
    except (ValueError, OSError) as e:
        logger.error("Cannot parse config %s: %s", path, e)
        return {}


def _write_config(path: Path, raw: dict) -> None:
    """Write the raw dict back to slife.json5 with indent=2 formatting."""
    path.write_text(json5.dumps(raw, indent=2, trailing_commas=False), encoding="utf-8")


def _env_section(raw: dict) -> dict:
    """Get or create the env: section, ensuring it's a dict."""
    env = raw.setdefault("env", {})
    if not isinstance(env, dict):
        logger.warning("Config env: section is not a dict — resetting.")
        env = {}
        raw["env"] = env
    return env


class ConfigEnvSetTool(Tool):
    """Add or update an environment variable in slife.json5.

    Changes take effect immediately — injected into os.environ so the
    next MCP server or tool call sees the new value. If the user hasn't
    provided a real value yet, a placeholder is written to remind them
    to edit slife.json5 later.
    """

    name = "config_env_set"
    description = (
        "Persist an environment variable to config and inject into the "
        "current process immediately."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Env var name in UPPER_SNAKE_CASE, e.g. TAVILY_API_KEY.",
            },
            "value": {
                "type": "string",
                "description": (
                    "The value to persist. Omit to write a '<YOUR_KEY>' placeholder "
                    "that the user fills in manually."
                ),
            },
        },
        "required": ["key"],
    }

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or Path("slife.json5")

    async def execute(self, **kwargs) -> str:
        key: str = kwargs.get("key", "")
        value: str = kwargs.get("value", "")
        raw = _read_config(self._config_path)
        env = _env_section(raw)

        if value:
            env[key] = value
            os.environ[key] = str(value)
            _write_config(self._config_path, raw)
            logger.info("Env set: %s (persisted + active)", key)
            return f"[OK] {key} set and active immediately."
        else:
            placeholder = f"<YOUR_{key.upper().strip('<>')}>"
            env[key] = placeholder
            os.environ[key] = placeholder
            _write_config(self._config_path, raw)
            logger.info("Env set: %s = placeholder (persisted + active)", key)
            return (
                f"[OK] {key} set to placeholder '{placeholder}'.\n"
                f"  Edit slife.json5 → env: → {key} to fill in your real value."
            )


class ConfigEnvGetTool(Tool):
    """Read environment variables from slife.json5's env: section."""

    name = "config_env_get"
    description = "Read environment variables from config."
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Single env var name to look up. Omit to list all.",
            },
        },
        "required": [],
    }

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or Path("slife.json5")

    async def execute(self, **kwargs) -> str:
        key: str = kwargs.get("key", "")
        raw = _read_config(self._config_path)
        env = _env_section(raw)

        if key:
            value = env.get(key)
            if value is None:
                return f"'{key}' is not set in slife.json5 env: section."
            is_placeholder = str(value).startswith(_PLACEHOLDER_PREFIX)
            note = " [PLACEHOLDER] placeholder — needs real value" if is_placeholder else ""
            return f"{key} = {value}{note}"

        if not env:
            return "No environment variables configured in slife.json5 env: section."

        lines = []
        for k, v in env.items():
            marker = " [PLACEHOLDER]" if str(v).startswith(_PLACEHOLDER_PREFIX) else ""
            lines.append(f"  {k} = {v}{marker}")
        return "slife.json5 env:\n" + "\n".join(lines)


class ConfigEnvRemoveTool(Tool):
    """Remove an environment variable from slife.json5 and os.environ."""

    name = "config_env_remove"
    description = "Remove an environment variable from config."
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Env var name to delete, e.g. TAVILY_API_KEY.",
            },
        },
        "required": ["key"],
    }

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or Path("slife.json5")

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]
        raw = _read_config(self._config_path)
        env = _env_section(raw)

        if key not in env:
            return f"'{key}' was not set in slife.json5 — nothing to remove."

        del env[key]
        os.environ.pop(key, None)
        _write_config(self._config_path, raw)
        logger.info("Env removed: %s (from config + os.environ)", key)
        return f"[OK] {key} removed from slife.json5 and deactivated."
