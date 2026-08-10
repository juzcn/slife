"""Config management tools.

config_env_set    — write env var to slife.json5
config_env_get    — read env var (shell → slife.json5)
config_env_remove — remove env var from slife.json5
native_tool_set   — enable/disable a built-in tool
"""

from __future__ import annotations

import logging
import os
from typing import ClassVar

from slife.tools._config_io import _ConfigPathMixin, read_config, write_config
from slife.tools.base import Tool

logger = logging.getLogger(__name__)

_PLACEHOLDER_PREFIX = "<YOUR_"


def _env_section(raw: dict) -> dict:
    env = raw.setdefault("env", {})
    if not isinstance(env, dict):
        env = {}
        raw["env"] = env
    return env


def _mcp_env_sections(raw: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    servers = raw.get("mcp", {}).get("servers", {})
    if isinstance(servers, dict):
        for name, cfg in servers.items():
            if isinstance(cfg, dict):
                server_env = cfg.get("env", {})
                if isinstance(server_env, dict) and server_env:
                    result[name] = dict(server_env)
    return result


def _lookup_one(key: str, env: dict, mcp_envs: dict[str, dict]) -> str:
    env_val = os.environ.get(key)
    if env_val:
        return f"{key} = {env_val} [shell]"
    sources = []
    config_val = env.get(key)
    if config_val and config_val not in (None, ""):
        sources.append(("slife.json5", str(config_val)))
    for server_name, server_env in sorted(mcp_envs.items()):
        val = server_env.get(key)
        if val and val not in (None, ""):
            sources.append((f"mcp/{server_name}", str(val)))
    if not sources:
        return f"'{key}' is not set."
    lines = [f"{key}:"]
    for source_name, value in sources:
        marker = " ← active" if source_name == sources[0][0] else ""
        lines.append(f"  [{source_name}]{marker}: {value}")
    return "\n".join(lines)


def _format_one(key: str, value: str) -> str:
    env_val = os.environ.get(key)
    if env_val:
        return f"  {key} = {env_val} [shell]"
    is_placeholder = str(value).startswith(_PLACEHOLDER_PREFIX)
    note = " [PLACEHOLDER]" if is_placeholder else " [unset]"
    return f"  {key} = {value}{note}"


def _toggle_native_enabled(raw: dict, name: str, enabled: bool) -> None:
    tools_override: list = raw.setdefault("tools", [])
    if not isinstance(tools_override, list):
        tools_override = []
        raw["tools"] = tools_override
    for entry in tools_override:
        if isinstance(entry, dict) and entry.get("name") == name:
            entry["enabled"] = enabled
            return
    tools_override.append({"name": name, "enabled": enabled})


# ── Config Env Set ───────────────────────────────────────────────────


class ConfigEnvSetTool(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    name = "config_env_set"
    category: ClassVar[str] = "Config"
    description = "Write an env var to slife.json5. Use ${VAR} refs for secrets — never plaintext."
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Env var name, e.g. EDITOR, DEEPSEEK_API_KEY."},
            "value": {"type": "string", "description": "Value to set. Omit to write a placeholder."},
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs.get("key", "")
        value: str | None = kwargs.get("value")
        raw = read_config(self._config_path)
        env = _env_section(raw)
        if value:
            env[key] = value
            os.environ[key] = str(value)
            write_config(self._config_path, raw)
            logger.info("env_set key=%s", key)
            return f"[OK] {key} = {value}"
        else:
            placeholder = f"<YOUR_{key.upper().strip('<>')}>"
            env[key] = placeholder
            write_config(self._config_path, raw)
            logger.info("env_set_placeholder key=%s", key)
            return f"[OK] {key} placeholder written.\nEdit slife.json5 → env: → {key} with the real value."


# ── Config Env Get ───────────────────────────────────────────────────


class ConfigEnvGetTool(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    name = "config_env_get"
    category: ClassVar[str] = "Config"
    description = "Look up an env var: shell first, then slife.json5. ${VAR} refs shown as-is. Omit key to list all."
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Env var name. Omit to list all."},
        },
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs.get("key", "")
        raw = read_config(self._config_path)
        env = _env_section(raw)
        mcp_envs = _mcp_env_sections(raw)
        if key:
            return _lookup_one(key, env, mcp_envs)
        lines = []
        if env:
            lines.append("env:")
            for k in sorted(env.keys()):
                lines.append(_format_one(k, env.get(k, "")))
        else:
            lines.append("env: (empty)")
        for server_name, server_env in sorted(mcp_envs.items()):
            lines.append(f"mcp/{server_name}:")
            for k in sorted(server_env.keys()):
                lines.append(_format_one(k, server_env.get(k, "")))
        return "\n".join(lines)


# ── Config Env Remove ────────────────────────────────────────────────


class ConfigEnvRemoveTool(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    name = "config_env_remove"
    category: ClassVar[str] = "Config"
    description = "Remove an env var from slife.json5. Does NOT touch the OS keyring."
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Env var name to remove."},
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]
        raw = read_config(self._config_path)
        env = _env_section(raw)
        if key not in env:
            return f"'{key}' is not in slife.json5 — nothing to remove."
        del env[key]
        write_config(self._config_path, raw)
        logger.info("env_removed key=%s", key)
        return f"[OK] Removed '{key}' from slife.json5."


# ── Native Tool Set ──────────────────────────────────────────────────


class NativeToolSet(_ConfigPathMixin, Tool):  # pyright: ignore[reportIncompatibleMethodOverride]
    name = "native_tool_set"
    category: ClassVar[str] = "Config"
    description = "Enable or disable a built-in tool. Takes effect after restart."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Tool name, from list_tools."},
            "enabled": {"type": "boolean", "description": "Enable or disable."},
        },
        "required": ["name", "enabled"],
    }

    async def execute(self, **kwargs) -> str:
        name: str = kwargs["name"]
        enabled: bool = kwargs["enabled"]
        raw = read_config(self._config_path)
        _toggle_native_enabled(raw, name, enabled)
        write_config(self._config_path, raw)
        state = "enabled" if enabled else "disabled"
        logger.info("native_tool_set name=%s enabled=%s", name, enabled)
        return f"[OK] Native tool '{name}' {state}. Restart for the change to take effect."
