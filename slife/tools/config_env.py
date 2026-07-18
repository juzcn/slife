"""Config environment variable tools.

- Non-secret env vars: config_env_set / config_env_get / config_env_remove.
  Resolution: os.environ → slife.json5.
- Secrets (API keys, tokens, passwords): config_secret_register (register)
  + credential_check (verify in keyring).  LLMs cannot invoke credstore CLI.

credstore is an interactive-only CLI tool — LLMs cannot invoke it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from slife.tools._config_io import _ConfigPathMixin, read_config, write_config
from slife.tools.base import Tool

logger = logging.getLogger(__name__)

_PLACEHOLDER_PREFIX = "<YOUR_"


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

    For API keys, tokens, and passwords, use config_secret_register instead
    — it writes a ${VAR} placeholder and directs the user to the terminal.
    This tool is for regular env vars like EDITOR, LANG, LOG_LEVEL, etc.
    """

    name = "config_env_set"
    description = (
        "Set a non-secret env var in slife.json5. "
        "Writes the value directly (or a <YOUR_VAR> placeholder if omitted). "
        "For secrets (API keys, tokens, passwords) use config_secret_register."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Env var name for a NON-SECRET setting, e.g. 'EDITOR' or 'LOG_LEVEL'. "
                    "Do NOT use for API keys or tokens — use config_secret_register for those."
                ),
            },
            "value": {
                "type": "string",
                "description": "Value for the env var. Omit to write a <YOUR_VAR> placeholder.",
            },
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
            return (
                f"[OK] {key} placeholder written.\n"
                f"Edit slife.json5 → env: → {key} with the real value."
            )


# ── config_secret_register ──────────────────────────────────


class ConfigSecretRegisterTool(_ConfigPathMixin, Tool):
    """Register a secret env var in slife.json5.

    Writes a ${VAR} placeholder ONLY — the tool has NO value parameter,
    so the secret can never enter the LLM context.

    The user must run the interactive CLI ``credstore set <KEY>`` in
    their own terminal to store the real secret in the OS keyring.
    LLMs cannot invoke credstore — it requires direct TTY input.
    """

    name = "config_secret_register"
    description = (
        "Register a secret env var (API key, token, password) in slife.json5. "
        "Writes a ${VAR} placeholder — NEVER accepts the secret value. "
        "The user must store the real value via 'credstore set <KEY>' in "
        "their own terminal (credstore is interactive-only)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Env var name for the secret, e.g. 'DEEPSEEK_API_KEY'.",
            },
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]

        from credstore import exists_credential

        already_stored = exists_credential(key)

        raw = read_config(self._config_path)
        env = _env_section(raw)
        env[key] = "${%s}" % key
        write_config(self._config_path, raw)
        logger.info("secret_registered key=%s already_stored=%s", key, already_stored)

        status = "already stored in keyring" if already_stored else "not yet stored"
        return (
            f"[OK] Registered '{key}' in slife.json5 ({status}).\n\n"
            f"To store the secret, user must run in terminal:\n"
            f"  credstore set {key}\n\n"
            f"(credstore requires interactive terminal — LLMs cannot invoke it.)"
        )


# ── config_env_get ──────────────────────────────────────────


class ConfigEnvGetTool(_ConfigPathMixin, Tool):
    """Read non-secret env vars. Resolution: shell → slife.json5.

    Does NOT query the keyring — use credential_check for API keys and tokens.
    """

    name = "config_env_get"
    description = (
        "Read a non-secret env var (shell → slife.json5). "
        "Does NOT check the keyring — use credential_check for secrets. "
        "Omit key to list all configured non-secret variables."
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
    # shell takes priority
    env_val = os.environ.get(key)
    if env_val:
        return f"{key} = {env_val} [shell]"

    # Fallback: slife.json5
    config_val = env.get(key)
    if config_val and config_val not in (None, ""):
        return f"{key} = {config_val} [slife.json5]"

    return f"'{key}' is not set."


def _format_one(key: str, value: str) -> str:
    env_val = os.environ.get(key)
    if env_val:
        return f"  {key} = {env_val} [shell]"

    is_placeholder = str(value).startswith(_PLACEHOLDER_PREFIX)
    note = " [PLACEHOLDER]" if is_placeholder else " [unset]"
    return f"  {key} = {value}{note}"
