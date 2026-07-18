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
    """Verify credentials in the OS keyring with masked values.

    Never exposes the full secret — only first 4 + last 4 characters.
    """

    name = "credential_check"
    description = (
        "Check a credential in the OS keyring. "
        "Shows masked value (e.g. 'sk-a…B3f2') if stored. "
        "Shell env vars override the keyring. "
        "NEVER exposes the full secret."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Credential key, e.g. 'DEEPSEEK_API_KEY'.",
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


