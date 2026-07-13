"""Config environment variable management tools.

Tools for reading, setting, and removing environment variables in slife.json5's
env: section. Changes are persisted to disk AND injected into os.environ immediately
— no restart needed.
"""

import logging
import os
from pathlib import Path

from slife.tools._config_io import read_config, write_config
from slife.tools.base import Tool

logger = logging.getLogger(__name__)

_PLACEHOLDER_PREFIX = "<YOUR_"


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
        "Write an environment variable to slife.json5 and inject it "
        "into os.environ immediately. If value is omitted, writes a "
        "<YOUR_KEY> placeholder instead."
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
                    "The value to persist. Omit to write a "
                    "'<YOUR_KEY>' placeholder."
                ),
            },
        },
        "required": ["key"],
    }

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or Path("slife.json5")

    @classmethod
    def from_config(cls, cfg, config):
        path = config._path if config else None
        return cls(config_path=path)

    async def execute(self, **kwargs) -> str:
        key: str = kwargs.get("key", "")
        value: str = kwargs.get("value", "")
        raw = read_config(self._config_path)
        env = _env_section(raw)

        if value:
            env[key] = value
            os.environ[key] = str(value)
            write_config(self._config_path, raw)
            logger.info("Env set: %s (persisted + active)", key)
            return f"[OK] {key} set and active immediately."
        else:
            placeholder = f"<YOUR_{key.upper().strip('<>')}>"
            env[key] = placeholder
            os.environ[key] = placeholder
            write_config(self._config_path, raw)
            logger.info("Env set: %s = placeholder (persisted + active)", key)
            return (
                f"[OK] {key} set to placeholder '{placeholder}'.\n"
                f"  Edit slife.json5 → env: → {key} to fill in your real value."
            )


class ConfigEnvGetTool(Tool):
    """Read environment variables from slife.json5's env: section."""

    name = "config_env_get"
    description = (
        "Read environment variables from slife.json5. Returns a single "
        "value if key is provided, or lists all configured variables "
        "if omitted."
    )
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

    @classmethod
    def from_config(cls, cfg, config):
        path = config._path if config else None
        return cls(config_path=path)

    async def execute(self, **kwargs) -> str:
        key: str = kwargs.get("key", "")
        raw = read_config(self._config_path)
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
    description = (
        "Delete an environment variable from slife.json5 and "
        "remove it from os.environ."
    )
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

    @classmethod
    def from_config(cls, cfg, config):
        path = config._path if config else None
        return cls(config_path=path)

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]
        raw = read_config(self._config_path)
        env = _env_section(raw)

        if key not in env:
            return f"'{key}' was not set in slife.json5 — nothing to remove."

        del env[key]
        os.environ.pop(key, None)
        write_config(self._config_path, raw)
        logger.info("Env removed: %s (from config + os.environ)", key)
        return f"[OK] {key} removed from slife.json5 and deactivated."
