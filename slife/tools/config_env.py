"""Config environment variable tools.

- Secrets (API keys, tokens, passwords): NEVER set via these tools.
  Use ``credstore set <KEY>`` in the terminal instead.
- Non-secret env vars: can be set/get/removed here.

Resolution order: os.environ → credstore (keyring) → slife.json5
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from slife.tools._config_io import _ConfigPathMixin, read_config, write_config
from slife.tools.base import Tool

logger = logging.getLogger(__name__)

_PLACEHOLDER_PREFIX = "<YOUR_"

# Key name patterns that indicate a secret
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def _is_secret_key(key: str) -> bool:
    return any(hint in key.upper() for hint in _SECRET_HINTS)


def _env_section(raw: dict) -> dict:
    env = raw.setdefault("env", {})
    if not isinstance(env, dict):
        logger.warning("env_config_not_dict")
        env = {}
        raw["env"] = env
    return env


# ── config_env_set ──────────────────────────────────────────


class ConfigEnvSetTool(_ConfigPathMixin, Tool):
    """Set a non-secret environment variable in slife.json5.

    For API keys / tokens / passwords, use ``credstore set <KEY>``
    in the terminal instead — never pass secrets through this tool.
    """

    name = "config_env_set"
    description = (
        "Set a NON-SECRET environment variable in slife.json5. "
        "For API keys, tokens, or passwords, do NOT use this tool — "
        "tell the user to run 'credstore set <KEY>' in their terminal. "
        "Only use this for things like EDITOR, LANG, LOG_LEVEL, etc."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Env var name. For secrets (KEY/TOKEN/SECRET/PASSWORD in name), this writes a credstore reference — never a value.",
            },
            "value": {
                "type": "string",
                "description": "The value. ONLY for non-secret vars (EDITOR, LANG, etc). If the key looks like a secret, this value is REJECTED.",
            },
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs.get("key", "")
        value: str | None = kwargs.get("value")

        if _is_secret_key(key):
            # Secret: NEVER accept a value. Write ${VAR} reference, direct to CLI.
            if value and not str(value).startswith(("${", "<")):
                return (
                    f"[REJECTED] '{key}' looks like a secret (API key/token/password).\n"
                    f"Never pass secrets through this tool.\n"
                    f"Tell the user to run: credstore set {key}"
                )

            raw = read_config(self._config_path)
            env = _env_section(raw)
            env[key] = "${%s}" % key
            write_config(self._config_path, raw)
            logger.info("env_set_credstore_ref key=%s", key)
            return (
                f"[OK] Registered '{key}' in slife.json5.\n\n"
                f"To store the secret, user must run in terminal:\n"
                f"  credstore set {key}"
            )

        # Non-secret: write value or placeholder
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
            return (
                f"[OK] {key} placeholder written.\n"
                f"Edit slife.json5 → env: → {key} with the real value."
            )


# ── config_env_get ──────────────────────────────────────────


class ConfigEnvGetTool(_ConfigPathMixin, Tool):
    """Read env vars — shell first, then credstore, then config."""

    name = "config_env_get"
    description = (
        "Look up an environment variable. Resolution order: "
        "1) current os.environ (shell export), "
        "2) credstore keyring (for secrets), "
        "3) slife.json5 env: section (config file). "
        "Secret values (keys containing KEY/TOKEN/SECRET/PASSWORD) "
        "are always masked in the output. "
        "Omit the key to list all configured variables."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Env var name. Omit to list all.",
            },
        },
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs.get("key", "")
        raw = read_config(self._config_path)
        env = _env_section(raw)

        if key:
            return _lookup_one(key, env)

        if not env:
            return "No environment variables in slife.json5 env: section."

        lines = ["env:"]
        for k in sorted(env.keys()):
            lines.append(_format_one(k, env.get(k, "")))
        return "\n".join(lines)


# ── config_env_remove ───────────────────────────────────────


class ConfigEnvRemoveTool(_ConfigPathMixin, Tool):
    """Remove an env var from all sources."""

    name = "config_env_remove"
    description = (
        "Delete an environment variable from slife.json5, "
        "os.environ, and credstore (if stored there)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Env var name, e.g. TAVILY_API_KEY.",
            },
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]
        removed_from = []

        if os.environ.pop(key, None) is not None:
            removed_from.append("shell environment")

        try:
            from credstore import delete_credential
            if delete_credential(key):
                removed_from.append("credstore (keyring)")
        except Exception:
            pass

        raw = read_config(self._config_path)
        env = _env_section(raw)
        if key in env:
            del env[key]
            write_config(self._config_path, raw)
            removed_from.append("slife.json5")

        if not removed_from:
            return f"'{key}' was not set anywhere — nothing to remove."

        logger.info("env_removed key=%s sources=%s", key, removed_from)
        return f"[OK] {key} removed from: {', '.join(removed_from)}."


# ── helpers ─────────────────────────────────────────────────


def _lookup_one(key: str, env: dict) -> str:
    sources = []

    env_val = os.environ.get(key)
    if env_val:
        sources.append(("shell", env_val))

    from credstore import get_credential
    cred_val = get_credential(key)
    if cred_val:
        sources.append(("credstore", cred_val))

    config_val = env.get(key)
    if config_val and config_val not in (None, ""):
        sources.append(("slife.json5", str(config_val)))

    if not sources:
        return f"'{key}' is not set anywhere."

    lines = [f"{key}:"]
    for source_name, value in sources:
        masked = _mask_if_secret(key, value)
        marker = " ← active" if source_name == sources[0][0] else ""
        lines.append(f"  [{source_name}]{marker}: {masked}")

    return "\n".join(lines)


def _format_one(key: str, value: str) -> str:
    env_val = os.environ.get(key)
    if env_val:
        return f"  {key} = {_mask_if_secret(key, env_val)} [shell]"

    from credstore import get_credential
    cred_val = get_credential(key)
    if cred_val:
        return f"  {key} = {_mask_if_secret(key, cred_val)} [credstore]"

    is_placeholder = str(value).startswith(_PLACEHOLDER_PREFIX)
    note = " [PLACEHOLDER]" if is_placeholder else " [unset]"
    return f"  {key} = {value}{note}"


def _mask_if_secret(key: str, value: str) -> str:
    if _is_secret_key(key):
        if len(value) > 8:
            return f"{value[:4]}…{value[-4:]}"
        return "***"
    return value
