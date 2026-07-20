"""Credential management tools for the LLM agent.

Talk directly to the OS keyring via credstore.  credential_check returns
masked values (e.g. ``sk-a…B3f2``) so the LLM can verify a credential
is configured correctly without seeing the full secret.
Use config_secret_register to register secret env vars in slife.json5
(writes ${VAR} placeholder — user stores the real value via credstore CLI).
Use config_env_set / config_env_get for non-secret env vars.
"""

from __future__ import annotations

import logging
import os

from slife.tools.base import Tool

logger = logging.getLogger(__name__)


def _mask_value(value: str) -> str:
    """Mask a credential value — first 4 + last 4."""
    if len(value) > 8:
        return f"{value[:4]}…{value[-4:]}"
    return "***"


class CredentialCheckTool(Tool):
    """Verify a credential in the OS keyring — masked, LLM-safe.

    NEVER exposes the full secret — returns only first 4 + last 4
    characters (e.g. ``sk-a…B3f2``).  Shell env vars take priority
    over the keyring.  Use this to check whether a credential is
    configured before attempting operations that need it.
    """

    requires_a2a = False

    name = "credential_check"
    description = (
        "Verify a credential in the OS keyring — LLM-safe, never "
        "exposes the full secret. "
        "Returns masked value (e.g. 'sk-a…B3f2') if stored, or "
        "'not stored' if missing. Shell env vars override keyring. "
        "Use before running operations that need an API key — "
        "check first, then use inject_credential to load it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Credential key to check, e.g. 'DEEPSEEK_API_KEY', "
                    "'GITHUB_TOKEN'. Returns masked status only — "
                    "NEVER the full secret."
                ),
            },
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]

        from credstore import get_credential

        # shell takes priority
        env_val = os.environ.get(key)
        if env_val:
            return f"{key} = {_mask_value(env_val)} [shell]"

        # Check keyring
        cred_val = get_credential(key)
        if cred_val:
            return f"{key} = {_mask_value(cred_val)} [credstore]"

        return f"'{key}' is not stored in the keyring."


class InjectCredentialTool(Tool):
    """Load a secret from the OS keyring into the current process environment.

    Reads the secret from the keyring and sets it directly in os.environ
    — temporary, this process only, gone when the process exits.  The
    secret NEVER appears in the return value — the LLM only sees a
    confirmation.  Use credential_check first to verify the key exists.
    Use uninject_credential to clean up when done.
    """

    requires_a2a = False

    name = "inject_credential"
    description = (
        "Load a secret from the OS keyring into the current process "
        "environment (os.environ) — temporary, gone on exit. "
        "LLM-safe: return value is a confirmation ONLY, never the secret. "
        "The key becomes available to subprocesses spawned after injection. "
        "Use credential_check first to verify the key exists. "
        "Use uninject_credential to remove from the environment when done."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Credential key to load from keyring, e.g. 'DEEPSEEK_API_KEY', "
                    "'GITHUB_TOKEN'. The secret is placed in os.environ — "
                    "NEVER returned to the LLM."
                ),
            },
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]

        from credstore import get_credential

        value = get_credential(key)
        if value is None:
            return f"Error: '{key}' not found in the OS keyring."

        os.environ[key] = value
        del value
        return f"Set {key} from keyring (temporary, this process only)."


class UninjectCredentialTool(Tool):
    """Remove an env var from the current process — keyring is untouched.

    Only clears os.environ for this process.  Does NOT touch the OS
    keyring — the credential remains safely stored.  Use this to clean
    up after inject_credential when the secret is no longer needed.
    """

    requires_a2a = False

    name = "uninject_credential"
    description = (
        "Remove an env var from the current process environment only. "
        "Does NOT touch the OS keyring — the secret remains stored. "
        "Use to clean up after inject_credential when the key is no "
        "longer needed by subprocesses in this session."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Env var to remove from os.environ, e.g. 'DEEPSEEK_API_KEY'. "
                    "Only clears current process — keyring secret is unaffected."
                ),
            },
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]
        existed = key in os.environ
        os.environ.pop(key, None)
        if existed:
            return f"Removed {key} from environment."
        return f"{key} was not set in the environment."


