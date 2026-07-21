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

# Patterns that indicate a value is likely a secret (API key, token, etc.)
import re as _re

_SECRET_KEY_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "AUTH")
_SECRET_VALUE_PATTERNS = [
    _re.compile(r"^sk-[A-Za-z0-9_-]{20,}"),     # OpenAI / Anthropic style
    _re.compile(r"^[A-Za-z0-9+/=]{32,}$"),      # base64-like blob
    _re.compile(r"^gh[psu]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    _re.compile(r"^ya29\.[A-Za-z0-9_-]{20,}"),  # Google OAuth
    _re.compile(r"^[A-Za-z0-9]{32,}$"),          # hex-ish tokens (deepseek, etc.)
]


def _looks_like_secret(key: str, value: str) -> bool:
    """Return True if *key* or *value* looks like it contains a secret.

    Checks both the variable name (for KEY / SECRET / TOKEN / PASSWORD /
    AUTH hints) and the value shape (API key prefixes, length, entropy).
    """
    # Variable name hints
    if any(hint in key.upper() for hint in _SECRET_KEY_HINTS):
        return True

    # Value shape: known prefixes
    for pat in _SECRET_VALUE_PATTERNS:
        if pat.match(value):
            return True

    # Value shape: long enough to be a key and looks structured
    if len(value) >= 40 and any(c.isupper() for c in value) and any(c.islower() for c in value):
        return True

    return False


def _env_section(raw: dict) -> dict:
    env = raw.setdefault("env", {})
    if not isinstance(env, dict):
        logger.warning("env_config_not_dict")
        env = {}
        raw["env"] = env
    return env


# ── config_env_set ──────────────────────────────────────────


class ConfigEnvSetTool(_ConfigPathMixin, Tool):
    """Set a NON-SECRET environment variable in slife.json5.

    REJECTS values that look like secrets (API key prefixes, token
    patterns, high-entropy strings, or key names containing KEY/
    SECRET/TOKEN/AUTH).  For secrets, use config_secret_register
    instead — it writes a ${VAR} placeholder and directs the user
    to store the real key via credstore.

    This tool is for env vars like EDITOR, LANG, LOG_LEVEL, etc.
    """

    name = "config_env_set"
    description = (
        "Set a NON-SECRET env var in slife.json5. "
        "REJECTS values that look like API keys (sk-*, ghp_*, ya29.*, "
        "base64 blobs) or key names with KEY/SECRET/TOKEN/AUTH. "
        "For secrets use config_secret_register — plaintext keys are "
        "rejected system-wide. "
        "Omit value to write a <YOUR_VAR> placeholder."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Env var name — NON-SECRET only. Examples: 'EDITOR', 'LANG', 'LOG_LEVEL'. "
                    "Names with KEY/SECRET/TOKEN/AUTH are REJECTED — use config_secret_register."
                ),
            },
            "value": {
                "type": "string",
                "description": (
                    "Value for the env var. REJECTED if it looks like an API key "
                    "(sk-*, ghp_*, ya29.*, long base64/hex strings). "
                    "Omit to write a <YOUR_VAR> placeholder instead."
                ),
            },
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs.get("key", "")
        value: str | None = kwargs.get("value")

        # ── Guard: reject values that look like API keys ──────────
        if value:
            if _looks_like_secret(key, value):
                return (
                    f"Cannot set '{key}' via config_env_set — the value looks like a secret "
                    f"(API key, token, or password).\n\n"
                    f"Use config_secret_register instead to register '{key}' as a "
                    f"${key} reference, then store the real value securely:\n"
                    f"  credstore set {key}"
                )

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
    """Register a secret env var (API key, token, password) in slife.json5.

    The ONLY safe path for secrets.  Writes a ${VAR} placeholder ONLY
    — the tool has NO value parameter, so the secret can never enter
    the LLM context.  Plaintext keys are rejected at startup and by
    config_env_set — this is the enforced single path.

    The user must run ``credstore set <KEY>`` in their own terminal
    to store the real secret in the OS keyring.  credstore requires
    direct TTY input — LLMs cannot invoke it.
    """

    name = "config_secret_register"
    description = (
        "Register a secret env var (API key, token, password) in slife.json5 — "
        "the ONLY safe path for secrets. "
        "Writes a ${VAR} placeholder — NEVER accepts or sees the secret value. "
        "Plaintext keys are rejected everywhere else (startup, config_env_set). "
        "The user must run 'credstore set <KEY>' in their terminal "
        "(credstore requires direct TTY — LLMs cannot invoke it)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Env var name for the secret, e.g. 'DEEPSEEK_API_KEY', "
                    "'GITHUB_TOKEN', 'SERPER_API_KEY'. "
                    "The user stores the real value via: credstore set <KEY>"
                ),
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
        env[key] = f"${{{key}}}"
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
    """Read non-secret env var configuration. Resolution: shell → slife.json5.

    Does NOT query the OS keyring — ${VAR} references are shown AS-IS
    (e.g. ``${DEEPSEEK_API_KEY}``), never resolved to their secret values.
    Use credential_check to verify secrets in the keyring.
    Use inject_credential to load a secret into the current process.
    """

    name = "config_env_get"
    description = (
        "Read non-secret env var configuration (shell → slife.json5 → MCP server envs). "
        "Does NOT query the OS keyring — ${VAR} references are shown as-is "
        "(e.g. '${DEEPSEEK_API_KEY}'), never resolved. "
        "Secret values from the shell environment are automatically masked "
        "(e.g. 'sk-a…B3f2 [shell]'). "
        "Use credential_check to verify secrets in the keyring. "
        "Omit key to list all configured vars across root env: and "
        "mcp.servers.<name>.env sections."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Env var name to look up. Shows shell value if set, "
                    "otherwise the slife.json5 entry (${VAR} refs shown as-is). "
                    "Omit to list all configured vars. "
                    "For secrets use credential_check instead."
                ),
            },
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

        # Root env: section
        if env:
            lines.append("env:")
            for k in sorted(env.keys()):
                lines.append(_format_one(k, env.get(k, "")))
        else:
            lines.append("env: (empty)")

        # MCP server env sections
        for server_name, server_env in sorted(mcp_envs.items()):
            lines.append(f"mcp/{server_name}:")
            for k in sorted(server_env.keys()):
                lines.append(_format_one(k, server_env.get(k, "")))

        return "\n".join(lines)


# ── config_env_remove ───────────────────────────────────────


class ConfigEnvRemoveTool(_ConfigPathMixin, Tool):
    """Remove an env var REFERENCE from slife.json5 only.

    Does NOT touch the OS keyring or shell environment — only removes
    what Slife itself configured (${VAR} placeholder or non-secret value).
    Secrets remain in the keyring and must be deleted via ``credstore delete``.
    """

    name = "config_env_remove"
    description = (
        "Remove an env var entry from slife.json5 only. "
        "Does NOT touch the OS keyring — secrets stored via credstore "
        "are unaffected. To delete a secret from the keyring the user "
        "must run 'credstore delete <KEY>' in their terminal. "
        "Only removes what Slife put in its config file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Env var name to remove from slife.json5. "
                    "Only removes the config entry (${VAR} ref or value) — "
                    "the keyring secret (if any) is NOT touched."
                ),
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


def _mcp_env_sections(raw: dict) -> dict[str, dict]:
    """Extract per-server env dicts from ``mcp.servers.<name>.env``."""
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
    # shell takes priority
    env_val = os.environ.get(key)
    if env_val:
        if _looks_like_secret(key, env_val):
            from slife.tools.credentials import _mask_value
            return f"{key} = {_mask_value(env_val)} [shell]"
        return f"{key} = {env_val} [shell]"

    sources = []

    # Root env:
    config_val = env.get(key)
    if config_val and config_val not in (None, ""):
        sources.append(("slife.json5", str(config_val)))

    # MCP server envs
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
        if _looks_like_secret(key, env_val):
            from slife.tools.credentials import _mask_value
            return f"  {key} = {_mask_value(env_val)} [shell]"
        return f"  {key} = {env_val} [shell]"

    is_placeholder = str(value).startswith(_PLACEHOLDER_PREFIX)
    note = " [PLACEHOLDER]" if is_placeholder else " [unset]"
    return f"  {key} = {value}{note}"
