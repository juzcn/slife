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
    """Register an environment variable in slife.json5.

    The SINGLE tool for all env var registration — automatically
    handles secrets vs non-secrets based on the key name:

    • Secret key (contains KEY/TOKEN/SECRET/PASSWORD):
      Writes ${VAR} reference, tells user to run credstore set <KEY>.
      The secret VALUE is REJECTED if passed — never stored in config.

    • Non-secret key (EDITOR, LANG, LOG_LEVEL, etc):
      Writes the value directly, or a <YOUR_VAR> placeholder if no
      value given.

    There is no separate credential_set tool — this covers both.
    """

    name = "config_env_set"
    description = (
        "Register an env var in slife.json5. "
        "Secret key (KEY/TOKEN/SECRET/PASSWORD) → writes ${VAR} ref, "
        "value is REJECTED. "
        "Non-secret key → writes value or <YOUR_VAR> placeholder."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Env var name, e.g. 'DEEPSEEK_API_KEY' or 'EDITOR'. "
                    "Secret keys (containing KEY/TOKEN/SECRET/PASSWORD) "
                    "get a ${VAR} reference + CLI instruction. "
                    "Non-secret keys get a value or placeholder."
                ),
            },
            "value": {
                "type": "string",
                "description": (
                    "ONLY for non-secret vars (EDITOR, LANG, LOG_LEVEL, etc). "
                    "If the key looks like a secret, this value is REJECTED — "
                    "secrets MUST go through 'credstore set <KEY>' in the terminal."
                ),
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
                    f"NEVER paste secrets into the chat — chat history is plaintext.\n"
                    f"Instead, tell the user to run in their terminal:\n"
                    f"  credstore set {key}"
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
    """Resolve an env var across all three sources at runtime.

    Resolution order: shell environment → credstore keyring → slife.json5.
    Secret values are always masked in output.  Omit key to list all.
    """

    name = "config_env_get"
    description = (
        "Resolve an env var across shell → keyring → config. "
        "Secret values are always masked. "
        "Omit key to list all configured variables."
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
    """Remove an env var from slife.json5 only."""

    name = "config_env_remove"
    description = (
        "Remove an env var from slife.json5. "
        "Does NOT touch the OS keyring or shell environment — "
        "only removes what Slife itself configured."
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

        raw = read_config(self._config_path)
        env = _env_section(raw)
        if key not in env:
            return f"'{key}' is not in slife.json5 — nothing to remove."

        del env[key]
        write_config(self._config_path, raw)
        logger.info("env_removed key=%s", key)
        return f"[OK] Removed '{key}' from slife.json5."


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
