"""Credential management tools for the LLM agent.

Talk directly to the OS keyring via credstore.  Sensitive values are
NEVER exposed — credential_check only confirms existence (stored / not stored).
Use config_env_set to register env vars in
slife.json5 (it handles both secrets and non-secrets).
"""

from __future__ import annotations

import logging

from slife.tools.base import Tool

logger = logging.getLogger(__name__)


class CredentialCheckTool(Tool):
    """Check if a credential exists in the OS keyring."""

    name = "credential_check"
    description = (
        "Check if a credential exists in the OS keyring. "
        "Returns only 'stored' or 'not stored' — NEVER the value."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Credential key in the OS keyring, e.g. 'DEEPSEEK_API_KEY'.",
            },
        },
        "required": ["key"],
    }

    async def execute(self, **kwargs) -> str:
        key: str = kwargs["key"]

        from credstore import exists_credential

        if exists_credential(key):
            return f"'{key}' is stored in the keyring."
        else:
            return f"'{key}' is not stored in the keyring."


