"""Credential management tools for the LLM agent.

All tools delegate to the credstore package.  Sensitive values are
NEVER exposed in full — credential_get always masks output,
credential_set never logs the secret value.

Tools:
  credential_set      — Store a secret in OS keyring
  credential_get      — Check if a credential exists (masked)
  credential_delete   — Remove a credential
  credential_list     — List stored credential keys
"""

from __future__ import annotations

import logging

from slife.tools._config_io import _ConfigPathMixin, read_config, write_config
from slife.tools.base import Tool

logger = logging.getLogger(__name__)


class CredentialSetTool(_ConfigPathMixin, Tool):
    """Store a secret in the OS keyring.

    After storing, optionally replaces the corresponding value in
    slife.json5 with a ``keyring:`` URI reference so the config file
    never contains the plaintext secret.
    """

    name = "credential_set"
    description = (
        "Guide for storing a credential: tells the user the exact "
        "credstore CLI command to run in their terminal. Secrets MUST "
        "be entered via the terminal (masked input), never through "
        "conversation or function arguments."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Credential identifier (env var name), e.g. 'DEEPSEEK_API_KEY'."
                ),
            },
            "replace_in_config": {
                "type": "string",
                "description": (
                    "Optional: dot-separated path in slife.json5 to remind the "
                    "user to replace with a keyring: reference, e.g. "
                    "'models.providers.deepseek.api_key'."
                ),
            },
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]
        config_path: str | None = kwargs.get("replace_in_config")

        lines = [
            f"To store the credential '{key}', run this in your terminal:",
            f"",
            f"  credstore set {key}",
            f"",
            f"This reads the secret via masked input (shows ***) —",
            f"never paste secrets into the chat.",
        ]

        if config_path:
            uri = f"keyring:{key}"
            lines.append(f"")
            lines.append(f"Then update slife.json5:")
            lines.append(f"  {config_path}: \"{uri}\"")

        return "\n".join(lines)


class CredentialGetTool(Tool):
    """Check if a credential exists (masked output)."""

    name = "credential_get"
    description = (
        "Check if a credential exists in the OS keyring. "
        "Returns a MASKED preview (first 4 + last 4 characters) — "
        "NEVER the full secret value."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Credential key to look up, e.g. 'DEEPSEEK_API_KEY'.",
            },
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]

        from credstore import get_credential

        value = get_credential(key)
        if value is None:
            return f"'{key}' is not stored in the keyring."

        from credstore._store import CredentialStore
        masked = CredentialStore.mask(value)
        return f"{key}: {masked}"


class CredentialDeleteTool(Tool):
    """Delete a credential from the OS keyring."""

    name = "credential_delete"
    description = "Delete a stored credential from the OS keyring."
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Credential key to delete, e.g. 'DEEPSEEK_API_KEY'.",
            },
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]

        from credstore import delete_credential

        existed = delete_credential(key)
        if existed:
            logger.info("credential_delete key=%s", key)
            return f"[OK] Deleted '{key}' from keyring."
        else:
            return f"'{key}' was not stored — nothing to delete."


class CredentialListTool(Tool):
    """List all stored credential keys (names only, no values)."""

    name = "credential_list"
    description = (
        "List all credential keys stored in the OS keyring. "
        "Returns key names only — NEVER secret values."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        from credstore import list_credentials

        keys = list_credentials()
        if not keys:
            return "No credentials stored in the keyring."

        lines = ["Credentials in keyring:"]
        for k in sorted(keys):
            lines.append(f"  - {k}")
        return "\n".join(lines)


# ── helpers ─────────────────────────────────────────────────────


def _set_nested(raw: dict, path: str, value: str) -> None:
    """Set a value at a dot-separated path in a nested dict.

    Example: _set_nested(raw, "mcp.servers.github.env.GH_TOKEN", "keyring:...")
    """
    parts = path.split(".")
    current = raw
    for part in parts[:-1]:
        if part not in current:
            current[part] = {}
        elif not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value
